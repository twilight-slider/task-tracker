"""Run inside a synthetic agent container to check mounted file access."""

import argparse
import errno
import json
from pathlib import Path
import tempfile


DENIED = {errno.EACCES, errno.EPERM, errno.ENOENT, errno.EROFS}


def probe(operation, path, expected):
    path = Path(path)
    try:
        if operation == "read":
            with path.open("rb") as file:
                file.read(1)
        elif operation == "write":
            with tempfile.NamedTemporaryFile(dir=path, prefix=".aidev79-probe-") as file:
                file.write(b"probe")
                file.flush()
        elif operation == "modify":
            with path.open("r+b"):
                pass
        else:
            raise ValueError(f"Unsupported operation: {operation}")
        allowed, error = True, None
    except OSError as exc:
        allowed, error = False, exc.errno

    return {
        "operation": operation,
        "path": str(path),
        "allowed": allowed,
        "expected": expected,
        "errno": error,
        "passed": allowed == expected and (allowed or error in DENIED),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("allow-read", "deny-read", "allow-write", "deny-write", "deny-modify"):
        parser.add_argument(f"--{option}", action="append", required=True)
    args = parser.parse_args()
    results = [
        probe(operation, path, expected)
        for operation, expected, paths in (
            ("read", True, args.allow_read),
            ("read", False, args.deny_read),
            ("write", True, args.allow_write),
            ("write", False, args.deny_write),
            ("modify", False, args.deny_modify),
        )
        for path in paths
    ]
    print(json.dumps(results, ensure_ascii=False))
    return 0 if all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
