#!/usr/bin/env python3
"""Export pinned, portable Codex plugins into an auditable Docker build context.

Run on the Windows host. Output stays under this repository's ignored .runtime.
Only explicit cache roots and top-level payload directories are copied.
"""

from hashlib import sha256
import argparse
import json
from pathlib import Path
import re
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from patch_agent_plugins import adapt as adapt_plugins
from patch_agent_train_driver import adapt as adapt_train_driver
from patch_agent_standalone import adapt as adapt_standalone
CACHE = Path.home() / ".codex" / "plugins" / "cache"
SKILLS = Path.home() / ".codex" / "skills"
OUTPUT = ROOT / ".runtime" / "agent-export" / "context"

PLUGINS = (
    ("ponytail", "ponytail", "ponytail", "5.3.2"),
    ("ai-marketplace", "jira", "jira", "0.4.0"),
    ("mattpocock", "mattpocock-skills", "mattpocock-skills", "1.3.1"),
    ("ai-marketplace", "task-folder-workflow", "task-folder-workflow", "0.6.2"),
    ("ai-marketplace", "task-thread-reflection", "task-thread-reflection", "0.1.1"),
    ("ai-marketplace", "decomposer", "decomposer", "0.1.3"),
    ("ai-marketplace", "plugin-skill-development", "plugin-skill-development", "0.1.0"),
    ("ai-marketplace", "train-driver", "train-driver", "0.6.9"),
    ("ai-marketplace", "task-tracker-mcp", "task-tracker-mcp", "0.2.2"),
    ("ai-marketplace", "prompt-engineer", "prompt-engineer", "0.1.0"),
)

STANDALONE = (
    "cdc-check-and-update", "etl-task-solver", "lmd-db-state-check",
    "postgre-sql-code-style", "project-context-reload",
    "skill-plugin-marketplace-workflow", "work-tools", "xlsx-workflow",
)


def market_name(provider):
    return provider

TOP_DIRS = frozenset((".codex-plugin", "skills", "scripts", "assets", "hooks", "lib"))
TOP_FILES = frozenset((".mcp.json", ".app.json", "package.json", "package-lock.json",
                       "plugin.json", "README.md", "LICENSE", "CHANGELOG.md", "GLOSSARY.md"))
BLOCKED_DIRS = frozenset((".git", "node_modules", "tests", "benchmarks", "examples", "__pycache__",
                          ".venv", ".runtime", "sessions", "auth"))
BLOCKED_FILES = frozenset(("auth.json", "credentials.json", "config.toml", ".env", ".env.local"))


def allowed(relative):
    parts = relative.parts
    if any(part.lower() in BLOCKED_DIRS for part in parts[:-1]):
        return False
    name = parts[-1].lower()
    if name in BLOCKED_FILES or name.startswith(".env.") or re.search(r"(^|[._-])(secret|credential)s?([._-]|$)", name):
        return False
    return True


def copy_tree(source, target, manifest, label):
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"Pinned source missing or linked: {source}")
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if not allowed(relative):
            continue
        if path.is_symlink():
            raise ValueError(f"Linked bundle source: {path}")
        if not path.is_file():
            continue
        if path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError(f"Unexpected large bundle file: {path}")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        payload = destination.read_bytes()
        manifest.append({"source": label, "path": destination.relative_to(OUTPUT).as_posix(),
                         "sha256": sha256(payload).hexdigest(), "bytes": len(payload)})


def check_plain_source(path, root):
    path, root = Path(path), Path(root)
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        raise ValueError("Bundle source is outside selected root")
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Linked bundle source: {current}")


def export(cache=CACHE, skills=SKILLS, output=OUTPUT, plugins=PLUGINS, standalone=STANDALONE):
    output = Path(output).resolve()
    runtime = (ROOT / ".runtime").resolve()
    if output != runtime / "agent-export" / "context" or runtime not in output.parents:
        raise ValueError("Export destination must be the repository's ignored .runtime/agent-export/context")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    manifest = []
    for provider, cache_name, plugin_name, version in plugins:
        source = Path(cache) / provider / cache_name / version
        check_plain_source(source, cache)
        if source.is_symlink() or not source.is_dir():
            raise ValueError(f"Pinned plugin root missing or linked: {source}")
        target = output / "agent_bundle" / "marketplaces" / market_name(provider) / "plugins" / plugin_name
        target.mkdir(parents=True)
        for item in source.iterdir():
            if item.is_symlink():
                raise ValueError(f"Linked plugin source: {item}")
            if item.name in TOP_DIRS and item.is_dir():
                if plugin_name == "ponytail" and item.name == "assets":
                    for name in ("codex-icon.png", "logo.png"):
                        asset = item / name
                        if asset.is_symlink() or not asset.is_file():
                            raise ValueError(f"Pinned plugin asset missing or linked: {asset}")
                        destination = target / "assets" / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(asset, destination)
                        payload = destination.read_bytes()
                        manifest.append({"source": f"{plugin_name}@{version}",
                                         "path": destination.relative_to(output).as_posix(),
                                         "sha256": sha256(payload).hexdigest(), "bytes": len(payload)})
                    continue
                copy_tree(item, target / item.name, manifest, f"{plugin_name}@{version}")
            elif item.name in TOP_FILES and item.is_file() and allowed(Path(item.name)):
                if item.is_symlink():
                    raise ValueError(f"Linked plugin file: {item}")
                destination = target / item.name
                shutil.copyfile(item, destination)
                payload = destination.read_bytes()
                manifest.append({"source": f"{plugin_name}@{version}",
                                 "path": destination.relative_to(output).as_posix(),
                                 "sha256": sha256(payload).hexdigest(), "bytes": len(payload)})
        plugin_manifest = target / ".codex-plugin" / "plugin.json"
        if not plugin_manifest.is_file():
            if plugin_name != "mattpocock-skills":
                raise ValueError(f"Codex plugin manifest missing: {plugin_name}")
            plugin_manifest.parent.mkdir(parents=True)
            plugin_manifest.write_text(json.dumps({"name": plugin_name, "version": version,
                "description": "Pinned skill-only bundle", "skills": "./skills/"}, indent=2) + "\n")
    ponytail_hook = ROOT / "agent_bundle" / "ponytail" / "hooks" / "hooks.json"
    target_hook = output / "agent_bundle" / "marketplaces" / "ponytail" / "plugins" / "ponytail" / "hooks" / "hooks.json"
    shutil.copyfile(ponytail_hook, target_hook)
    manifest.append({"source": "local Codex hook adapter", "path": target_hook.relative_to(output).as_posix(),
                     "sha256": sha256(target_hook.read_bytes()).hexdigest(), "bytes": target_hook.stat().st_size})
    ponytail_manifest = target_hook.parents[1] / ".codex-plugin" / "plugin.json"
    ponytail_settings = json.loads(ponytail_manifest.read_text())
    ponytail_settings["hooks"] = "./hooks/hooks.json"
    ponytail_manifest.write_text(json.dumps(ponytail_settings, indent=2) + "\n")
    for row in manifest:
        if row["path"] == ponytail_manifest.relative_to(output).as_posix():
            row.update(source="Ponytail Codex manifest adapter",
                       sha256=sha256(ponytail_manifest.read_bytes()).hexdigest(),
                       bytes=ponytail_manifest.stat().st_size)
            break
    for name in standalone:
        source = Path(skills) / name
        check_plain_source(source, skills)
        target = output / "agent_bundle" / "standalone-skills" / name
        copy_tree(source, target, manifest, f"standalone:{name}")
        if not (target / "SKILL.md").is_file():
            raise ValueError(f"Standalone skill missing SKILL.md: {name}")
    for target in adapt_standalone(output, ROOT / "agent_bundle" / "standalone-linux-runtime-check.sh"):
        relative = target.relative_to(output).as_posix()
        row = next((item for item in manifest if item["path"] == relative), None)
        if row is None:
            row = {"path": relative}
            manifest.append(row)
        row.update(source="local Linux standalone adapter", sha256=sha256(target.read_bytes()).hexdigest(),
                   bytes=target.stat().st_size)
    adapt_plugins(output)
    for target in adapt_train_driver(output, ROOT):
        relative = target.relative_to(output).as_posix()
        row = next((item for item in manifest if item["path"] == relative), None)
        if row is None:
            row = {"path": relative}
            manifest.append(row)
        row.update(source="local pinned Train Driver Linux adapter",
                   sha256=sha256(target.read_bytes()).hexdigest(), bytes=target.stat().st_size)
    for row in manifest:
        if row["path"].endswith(("scripts/task-folder-mcp.js", "scripts/task-tracker-mcp.js",
                                 "lib/tracker-snapshot-client.js")):
            adapted = output / row["path"]
            row.update(source=row["source"] + " + scoped Linux adapter",
                       sha256=sha256(adapted.read_bytes()).hexdigest(), bytes=adapted.stat().st_size)
    copy_tree(ROOT / "agent_bundle" / "ponytail", output / "agent_bundle" / "ponytail",
              manifest, "local Ponytail hook adapter")
    providers = sorted({market_name(item[0]) for item in plugins})
    for provider in providers:
        entries = [{"name": name, "source": {"source": "local", "path": f"./plugins/{name}"},
                    "policy": {"installation": "AVAILABLE", "authentication": "ON_USE"},
                    "category": "development"}
                   for group, _, name, _ in plugins if market_name(group) == provider]
        market = output / "agent_bundle" / "marketplaces" / provider / ".agents" / "plugins"
        market.mkdir(parents=True, exist_ok=True)
        (market / "marketplace.json").write_text(json.dumps({"name": provider, "plugins": entries}, indent=2) + "\n")
    config = []
    for provider in providers:
        config += [f"[marketplaces.{provider}]", "source_type = 'local'",
                   f"source = '/opt/agent-bundle/marketplaces/{provider}'", ""]
    for provider, _, name, _ in plugins:
        config += [f"[plugins.'{name}@{market_name(provider)}']", "enabled = true", ""]
    (output / "agent_bundle" / "marketplaces.toml").write_text("\n".join(config))
    (output / "agent_bundle" / "plugin-list.txt").write_bytes((
        "\n".join(f"{name}@{market_name(provider)}" for provider, _, name, _ in plugins) + "\n").encode())
    shutil.copyfile(ROOT / "src" / "agent_entrypoint.sh", output / "agent_entrypoint.sh")
    shutil.copyfile(ROOT / "src" / "agent_env_launcher.mjs", output / "agent_env_launcher.mjs")
    for name in ("train_driver_supervisor.mjs", "train_driver_proxy.mjs", "train_helper_mcp.json"):
        shutil.copyfile(ROOT / "src" / name, output / name)
    shutil.copyfile(ROOT / "Dockerfile.agent.train", output / "Dockerfile.agent.train")
    dockerfile = (ROOT / "Dockerfile.agent").read_text()
    dockerfile = dockerfile.replace("COPY src/agent_entrypoint.sh /usr/local/bin/agent-entrypoint",
                                    "COPY agent_entrypoint.sh /usr/local/bin/agent-entrypoint")
    dockerfile = dockerfile.replace("COPY src/agent_env_launcher.mjs /usr/local/bin/agent-env-launcher.mjs",
                                    "COPY agent_env_launcher.mjs /usr/local/bin/agent-env-launcher.mjs")
    marker = "COPY agent_bundle/ /opt/agent-bundle/"
    installs = "\n".join(
        f"RUN cd /opt/agent-bundle/marketplaces/ai-marketplace/plugins/{name} && npm ci --omit=dev --ignore-scripts --no-audit --no-fund"
        for name in ("jira", "train-driver"))
    dockerfile = dockerfile.replace(marker, marker + "\n" + installs)
    (output / "Dockerfile.agent").write_text(dockerfile)
    audit = ROOT / ".runtime" / "tests" / "agent-export"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / "manifest.json").write_text(json.dumps(sorted(manifest, key=lambda row: row["path"]), indent=2) + "\n")
    print(json.dumps({"context": str(output), "plugins": len(plugins), "standalone": len(standalone),
                      "files": len(manifest), "bytes": sum(row["bytes"] for row in manifest)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    args = parser.parse_args()
    export(cache=args.codex_home / "plugins" / "cache", skills=args.codex_home / "skills")
