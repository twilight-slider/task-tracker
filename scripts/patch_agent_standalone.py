"""Fail-closed Linux adaptations of pinned standalone skill copies only."""

from hashlib import sha256
from pathlib import Path
import shutil


EXPECTED = {
    "skill-plugin-marketplace-workflow/SKILL.md": "42f8a82a650ba54f0e620a844c6ec25b1458d89b6ebb50a12533f71129e5c71a",
    "work-tools/SKILL.md": "9e93ad2987571ce93a64cc0ad7b07340dc6ed7ee2f99e89e3dedd6c68793f46c",
    "work-tools/references/search-git.md": "67e92584aa4de50749e5ec020f0fc3cc0c1db1bc3a8afa57beeff7f1f72ce885",
    "xlsx-workflow/SKILL.md": "cbaba1d5ada679349b10ea58f8f62882ac1d912a1f39b8d76406732c35d2f5af",
    "lmd-db-state-check/artifacts/s2t-xlsx/scripts/apply_s2t_blocks.py": "b2d09ebb24fd863d0477950810398e8c39e456eae09708a1fc08b95b54b97642",
    "lmd-db-state-check/SKILL.md": "1f1368a3ab1510cb608930502b59c337f1d5c2acfed7afdb068d2a7f2fe6d17c",
    "lmd-db-state-check/references/s2t-xlsx.md": "4b800216cc93746d957e0c2404f9679f2c61e3c47726dcaeeedb8af18bdfbf9b",
    "lmd-db-state-check/references/s2t-excel/discovery.md": "a6d6ecb77712fc6cb7ee663b6172ef2f8dfe25534ccc6ded0e2d1c79c796f46b",
}

LINUX_SKILL_NOTE = """## Linux agent container

In the Linux agent container, run standard-library validators with `python3`.
When a plugin declares Python dependencies, create a virtual environment in that
plugin's repository with `python3 -m venv .venv`, install only its declared pinned
requirements, and run validators through `.venv/bin/python`. Never install into
the system Python. The Windows instructions below apply only on the host.

"""

WORK_TOOLS_NOTE = """## Linux agent container

Run `sh "$CODEX_HOME/skills/work-tools/scripts/linux-runtime-check.sh" local` before
local `python3`, `git`, or `rg` workflows. The `sqlserver`, `kafka`, `helpdesk`,
and `excel` modes report whether a Linux backend exists; they do not connect to
external services. The `.ps1` helpers and Windows paths below are host-only.
Never invoke a Windows helper from the container or claim its service checks ran.

"""

SEARCH_NOTE = """## Linux agent container

Use `rg --files` and `rg` for discovery, `git status --short` and `git diff --check`
for repository checks, and Python `pathlib`/`open` for known files. Paths are
Linux paths inside the assigned mounts. The Windows guidance below is host-only.

"""

XLSX_NOTE = """## Linux agent container

Microsoft Excel COM is unavailable here. `scripts/xlsx_audit.py` and direct OOXML
inspection can verify ZIP structure, formulas, and cached values, but cannot
prove Excel's live `.Value2` or `.Text`. Use an explicit fallback only when that
limit fits the task, and report that live Excel recalculation was not verified.

"""

LMD_NOTE = """## Linux S2T attachment download

In the Linux agent container use
`python3 "$CODEX_HOME/skills/lmd-db-state-check/artifacts/s2t-xlsx/scripts/download_s2t_attachment.py"`
with explicit `CONFLUENCE_BASE_URL` and `CONFLUENCE_API_TOKEN` from the per-agent
secret allowlist. `--list-only` reads attachment metadata; downloads are limited
to the configured HTTPS origin. Save downloads under `/workspace/task/update`
and record the selected baseline there; `/workspace/task/origin` is not mounted.
The Windows `.ps1` remains host-only. Excel COM
validation is unavailable; use the explicit OOXML-only gate where appropriate.

"""

DISCOVERY_NOTE = """## Linux agent container

Use `python3 "$CODEX_HOME/skills/lmd-db-state-check/artifacts/s2t-xlsx/scripts/download_s2t_attachment.py" --page-id 205225775 --list-only`
to identify the latest business version. Save under the writable
`/workspace/task/update` with `--output-directory /workspace/task/update` and
record the selected baseline in task-folder artifacts. The `origin` folder is
not mounted in the agent container.
The Confluence HTTPS origin and
token must both come from the per-agent environment; an arbitrary repository
`.env` file must not be mixed with a trusted token. The PowerShell commands below
are for the Windows host only.

"""


def adapt(context, checker):
    base = Path(context) / "agent_bundle" / "standalone-skills"
    changed = []
    for relative, expected in EXPECTED.items():
        target = base / relative
        original = target.read_bytes()
        if sha256(original).hexdigest() != expected:
            raise ValueError(f"Pinned standalone skill bytes changed: {relative}")
        source = original.decode("utf-8").replace("\r\n", "\n")
        if relative == "skill-plugin-marketplace-workflow/SKILL.md":
            adapted = source.replace("# Skill, Plugin, and Marketplace Workflow\n",
                                     "# Skill, Plugin, and Marketplace Workflow\n\n" + LINUX_SKILL_NOTE, 1)
        elif relative == "work-tools/SKILL.md":
            adapted = source.replace("# Work Tools\n", "# Work Tools\n\n" + WORK_TOOLS_NOTE, 1)
        elif relative == "work-tools/references/search-git.md":
            adapted = source.replace("# Search And Git\n", "# Search And Git\n\n" + SEARCH_NOTE, 1)
        elif relative == "xlsx-workflow/SKILL.md":
            adapted = source.replace("# XLSX Workflow\n", "# XLSX Workflow\n\n" + XLSX_NOTE, 1)
        elif relative == "lmd-db-state-check/SKILL.md":
            adapted = source.replace("# Проверка БД на соответствие ЛМД\n",
                                     "# Проверка БД на соответствие ЛМД\n\n" + LMD_NOTE, 1)
            artifact = "`scripts/download_s2t_attachment.ps1`, `scripts/apply_s2t_blocks.py`"
            if adapted.count(artifact) != 1:
                raise ValueError("LMD artifact list changed")
            adapted = adapted.replace(artifact,
                                      "`scripts/download_s2t_attachment.ps1` (Windows), "
                                      "`scripts/download_s2t_attachment.py` (Linux), `scripts/apply_s2t_blocks.py`", 1)
            if adapted == source:
                raise ValueError("LMD skill heading changed")
        elif relative == "lmd-db-state-check/references/s2t-xlsx.md":
            adapted = source.replace("# S2T XLSX\n", "# S2T XLSX\n\n" + LMD_NOTE, 1)
        elif relative == "lmd-db-state-check/references/s2t-excel/discovery.md":
            adapted = source.replace("# Wiki S2T Excel\n", "# Wiki S2T Excel\n\n" + DISCOVERY_NOTE, 1)
        else:
            old = '    if os.name != "nt":\n        return\n'
            if source.count(old) != 1:
                raise ValueError("Excel COM guard changed")
            adapted = source.replace(old, '    if os.name != "nt":\n        raise RuntimeError("Excel COM validation requires Windows; pass --skip-excel-validation for explicit OOXML-only output")\n')
            anchor = "    target, context = apply_spec(Path(args.task_dir).resolve(), Path(args.spec).resolve())\n"
            if source.count(anchor) != 1:
                raise ValueError("Excel CLI anchor changed")
            adapted = adapted.replace(anchor, '    if os.name != "nt" and not args.skip_excel_validation:\n        parser.error("Excel COM validation requires Windows; pass --skip-excel-validation for explicit OOXML-only output")\n\n' + anchor)
        if adapted == source:
            raise ValueError(f"Linux adaptation missing: {relative}")
        target.write_text(adapted, encoding="utf-8")
        changed.append(target)
    checker = Path(checker)
    if checker.is_symlink() or not checker.is_file():
        raise ValueError("Linux runtime checker missing or linked")
    destination = base / "work-tools" / "scripts" / "linux-runtime-check.sh"
    shutil.copyfile(checker, destination)
    changed.append(destination)
    source = Path(__file__).resolve().parents[1] / "src" / "s2t_attachment_linux.py"
    if source.is_symlink() or not source.is_file():
        raise ValueError("Linux S2T attachment helper missing or linked")
    target = base / "lmd-db-state-check" / "artifacts" / "s2t-xlsx" / "scripts" / "download_s2t_attachment.py"
    shutil.copyfile(source, target)
    changed.append(target)
    return changed
