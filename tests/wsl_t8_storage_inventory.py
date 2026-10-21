"""Read-only size and link inventory for the existing Tracker tasksRoot."""

import json
import os
from pathlib import Path
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path("/mnt/d/Projects/Tracker/tasks")


def main():
    if SOURCE.is_symlink() or not SOURCE.is_dir():
        raise ValueError("tasksRoot must be a plain directory")
    counters = {"files": 0, "bytes": 0, "directories": 0, "symlinks": 0,
                "shared_hardlinks": 0, "task_protected_files": 0,
                "task_protected_bytes": 0, "ai_actions_files": 0,
                "ai_actions_bytes": 0}
    task_folders = 0
    for year in SOURCE.iterdir():
        if year.is_dir() and not year.is_symlink():
            task_folders += sum(1 for item in year.iterdir() if item.is_dir() and not item.is_symlink())
    for directory, folders, files in os.walk(SOURCE, followlinks=False):
        counters["directories"] += 1
        for name in folders + files:
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                counters["symlinks"] += 1
            elif stat.S_ISREG(info.st_mode):
                counters["files"] += 1
                counters["bytes"] += info.st_size
                counters["shared_hardlinks"] += int(info.st_nlink > 1)
                parts = path.relative_to(SOURCE).parts
                if ".protected" in parts:
                    counters["task_protected_files"] += 1
                    counters["task_protected_bytes"] += info.st_size
                if "ai_actions" in parts:
                    counters["ai_actions_files"] += 1
                    counters["ai_actions_bytes"] += info.st_size
    result = {"source_fstype": subprocess.check_output([
        "findmnt", "-no", "FSTYPE", "-T", str(SOURCE)], text=True).strip(),
        "task_folders": task_folders, **counters}
    artifact = ROOT / ".runtime" / "tests" / "t8-preinstall"
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "storage-summary.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
