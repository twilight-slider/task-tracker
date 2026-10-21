"""Disposable WSL permission probe for a root helper sharing an agent container.

Run from Ubuntu/WSL 2. This is evidence only; it installs no runtime helper.
"""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.docker_controller import DockerController


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / ".runtime" / "tests" / "t8-private-helper"
IMAGE = "python:3.11-bookworm"
HELPER = r'''
import json, os, pathlib, time
p = pathlib.Path('/root/task/ai_actions/train-run.yaml')
before = p.read_bytes()
if before != b'fixture: initial\n': raise RuntimeError('CAS precondition failed')
temp = p.with_name('.train-run.yaml.probe-tmp')
with temp.open('xb') as stream:
    stream.write(b'fixture: helper-written\n')
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temp, p)
print(json.dumps({'helper_uid': os.getuid(), 'read': True, 'cas_write': p.read_bytes() == b'fixture: helper-written\n'}), flush=True)
while True: time.sleep(1)
'''
AGENT = r'''
import ctypes, errno, json, os, pathlib
p = pathlib.Path('/root/task/ai_actions/train-run.yaml')
attempts = {}
def check(label, action):
    try: action(); attempts[label] = 'ALLOWED'
    except OSError as error: attempts[label] = errno.errorcode.get(error.errno, str(error.errno))
check('stat', lambda: p.stat())
check('read', lambda: p.read_bytes())
check('write', lambda: p.write_bytes(b'agent-written\n'))
link = pathlib.Path('/update/probe-link')
link.symlink_to(p)
check('safe_update_symlink', lambda: link.read_bytes())
hardlink = pathlib.Path('/update/preexisting-hardlink')
if hardlink.exists(): check('preexisting_hardlink', lambda: hardlink.read_bytes())
check('proc_fd', lambda: list(pathlib.Path('/proc/1/fd').iterdir()))
check('proc_root', lambda: pathlib.Path('/proc/1/root/root/task/ai_actions/train-run.yaml').read_bytes())
check('proc_root_dir', lambda: pathlib.Path('/proc/1/root/root/task').stat())
check('proc_mem', lambda: pathlib.Path('/proc/1/mem').open('rb'))
libc = ctypes.CDLL(None, use_errno=True)
result = libc.ptrace(16, 1, None, None)
attempts['ptrace'] = 'ALLOWED' if result == 0 else errno.errorcode.get(ctypes.get_errno(), 'UNKNOWN')
print(json.dumps({'agent_uid': os.getuid(), 'attempts': attempts}))
'''


def docker(*args):
    return subprocess.run(["docker", *map(str, args)], check=True, text=True,
                          capture_output=True, timeout=40).stdout.strip()


def probe(kind, private, update):
    name = f"tt-private-probe-{kind}-{os.getpid()}"
    ai_actions = private / "ai_actions"
    target = ai_actions / "train-run.yaml"
    if kind == "ext4":
        # Create root-owned fixture using only DAC permissions, with all capabilities dropped.
        docker("run", "--rm", "--user", "0:0", "--cap-drop", "ALL", "--network", "none",
               "--read-only", "-v", f"{private.parent}:/fixture:rw", IMAGE, "python", "-c",
               "from pathlib import Path; p=Path('/fixture/private/ai_actions'); "
               "p.mkdir(parents=True,mode=0o700); p.parent.chmod(0o700); "
               "f=p/'train-run.yaml'; f.write_bytes(b'fixture: initial\\n'); f.chmod(0o600)")
    else:
        private.mkdir(mode=0o700)
        ai_actions.mkdir(mode=0o700)
        target.write_bytes(b"fixture: initial\n")
        target.chmod(0o600)
        private.chmod(0o700)
    update.mkdir(mode=0o777)
    result = {"filesystem": kind, "private_mode": oct(stat.S_IMODE(private.stat().st_mode)),
              "target_mode": (oct(stat.S_IMODE(target.stat().st_mode)) if kind == "drvfs"
                              else "0600 set by root-owned fixture")}
    result["host_findmnt"] = subprocess.run(
        ["findmnt", "-T", str(private), "-n", "-o", "FSTYPE,OPTIONS"],
        check=True, text=True, capture_output=True).stdout.strip()
    if kind == "drvfs":
        alias = update / "preexisting-hardlink"
        try:
            os.link(target, alias)
            result["hardlink"] = {"created": True, "source_nlink": target.stat().st_nlink,
                                  "alias_nlink": alias.stat().st_nlink}
            sessions = private.parent / "sessions"
            sessions.mkdir()
            controller = DockerController("fixture", sessions, task_update=update,
                                          protected_root=private)
            try:
                controller.validate_mount_safety([])
                result["hardlink"]["scan_rejected"] = False
            except ValueError:
                result["hardlink"]["scan_rejected"] = True
        except OSError as error:
            result["hardlink"] = {"created": False, "error": type(error).__name__,
                                  "errno": error.errno}
    started = False
    try:
        docker("run", "-d", "--name", name, "--label", "com.tasktracker.test=aidev79-private-helper",
               "--user", "0:0", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--network", "none", "--ipc", "private", "--pids-limit", "64", "--read-only",
               "--tmpfs", "/tmp:rw,noexec,nosuid,size=8m", "-v", f"{private}:/root/task:rw",
               "-v", f"{update}:/update:rw", IMAGE, "python", "-u", "-c", HELPER)
        started = True
        for _ in range(30):
            logs = docker("logs", name)
            if logs:
                result["helper"] = json.loads(logs.splitlines()[0])
                break
            time.sleep(.1)
        else:
            state = json.loads(docker("inspect", name))[0]["State"]
            raise RuntimeError(f"Helper did not initialize: {state['Status']} exit={state['ExitCode']}; logs={docker('logs', name)}")
        inspect = json.loads(docker("inspect", name))[0]
        host = inspect["HostConfig"]
        result["container"] = {"cap_drop": host["CapDrop"], "security_opt": host["SecurityOpt"],
                               "network_mode": host["NetworkMode"], "pid_mode": host["PidMode"],
                               "privileged": host["Privileged"], "readonly_rootfs": host["ReadonlyRootfs"],
                               "mounts": [m["Destination"] for m in inspect["Mounts"]]}
        parent = json.loads(docker("exec", name, "python", "-c",
            "import json,os,stat; s=os.stat('/root'); print(json.dumps({'uid':s.st_uid,'mode':oct(stat.S_IMODE(s.st_mode))}))"))
        result["container"]["private_parent"] = parent
        if parent != {"uid": 0, "mode": "0o700"}:
            raise RuntimeError("Container private parent is not root-owned 0700")
        if sorted(result["container"]["mounts"]) != ["/root/task", "/update"]:
            raise RuntimeError("Unexpected container bind mounts")
        result["container"]["mountinfo"] = docker("exec", name, "python", "-c",
            "print('\\n'.join(line for line in open('/proc/self/mountinfo') "
            "if ' /root/task ' in line or ' /update ' in line))")
        result["agent"] = json.loads(docker("exec", "--user", "1000:1000", name,
                                             "python", "-c", AGENT))
        result["helper_final_bytes"] = docker("exec", name, "python", "-c",
            "from pathlib import Path; print(Path('/root/task/ai_actions/train-run.yaml').read_text(),end='')") + "\n"
        attempts = result["agent"]["attempts"]
        container = result["container"]
        result["security_assertions"] = (
            result["helper"]["helper_uid"] == 0 and result["agent"]["agent_uid"] == 1000 and
            container["cap_drop"] == ["ALL"] and "no-new-privileges" in container["security_opt"] and
            not container["privileged"] and container["readonly_rootfs"] and
            container["network_mode"] == "none" and container["pid_mode"] == "" and
            sorted(container["mounts"]) == ["/root/task", "/update"] and
            container["private_parent"] == {"uid": 0, "mode": "0o700"})
        hardlink_safe = (kind != "drvfs" or not result["hardlink"]["created"] or
                         result["hardlink"]["scan_rejected"])
        result["pass"] = (result["security_assertions"] and hardlink_safe and
                          result["helper"]["cas_write"] and
                          result["helper_final_bytes"] == "fixture: helper-written\n" and
                          all(attempts[key] in ("EACCES", "EPERM") for key in attempts
                              if key != "preexisting_hardlink"))
    finally:
        if started:
            docker("rm", "-f", name)
    return result


def main():
    if os.name != "posix" or not Path("/proc/version").read_text().lower().find("microsoft") >= 0:
        raise RuntimeError("Run only inside Ubuntu/WSL 2")
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    ext4 = Path(tempfile.mkdtemp(prefix="aidev79-private-helper-", dir=Path.home()))
    drvfs = ARTIFACT / "drvfs"
    if drvfs.exists():
        raise RuntimeError("Existing drvfs fixture; inspect it before retry")
    report = {"cases": []}
    report["docker_security_options"] = json.loads(docker("info", "--format", "{{json .SecurityOptions}}"))
    report["docker_rootless"] = any("rootless" in option.lower() for option in report["docker_security_options"])
    try:
        for kind, base in (("ext4", ext4), ("drvfs", drvfs)):
            base.mkdir(mode=0o700, exist_ok=True)
            if kind == "ext4":
                base.chmod(0o777)
            try:
                report["cases"].append(probe(kind, base / "private", base / "update"))
            except Exception as error:
                report["cases"].append({"filesystem": kind, "error": type(error).__name__ + ": " + str(error)})
        for case in report["cases"]:
            print(json.dumps(case))
        (ARTIFACT / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    finally:
        if (ext4 / "private").exists():
            docker("run", "--rm", "--user", "0:0", "--cap-drop", "ALL", "--network", "none",
                   "--read-only", "-v", f"{ext4}:/fixture:rw", IMAGE, "python", "-c",
                   "import shutil; shutil.rmtree('/fixture/private')")
        for base in (ext4, drvfs):
            if base.exists():
                resolved = base.resolve()
                allowed = (base == ext4 and resolved.parent == Path.home().resolve()) or (
                    base == drvfs and resolved.parent == ARTIFACT.resolve())
                if not allowed:
                    raise RuntimeError("Unsafe fixture cleanup path")
                shutil.rmtree(resolved)
    if not all(case.get("pass") for case in report["cases"]):
        raise SystemExit("Permission boundary did not pass both fixtures")


if __name__ == "__main__":
    main()
