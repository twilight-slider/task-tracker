"""Small live Codex app-server protocol probe; leaves files under .runtime/tests."""

import argparse
import json
import queue
import subprocess
import threading
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("codex", type=Path)
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--list-models", action="store_true")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    root = repo / ".runtime" / "tests" / "codex-appserver"
    root.mkdir(parents=True, exist_ok=True)
    if not args.codex.is_file():
        raise FileNotFoundError(args.codex)
    messages: queue.Queue[dict | None] = queue.Queue()
    error_log = (root / "app-server.stderr.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(args.codex), "app-server", "--stdio"],
        cwd=root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=error_log,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )

    def read() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except json.JSONDecodeError:
                messages.put({"invalid": line[:100]})
        messages.put(None)

    threading.Thread(target=read, daemon=True).start()

    def send(value: dict) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def response(request_id: int, timeout: float = 30) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"No response to request {request_id}")
            message = messages.get(timeout=remaining)
            if message is None:
                process.wait(timeout=5)
                raise RuntimeError(f"app-server exited with code {process.returncode}; see {error_log.name}")
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"Request {request_id}: {message['error']}")
                return message["result"]

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "aidev57-probe", "version": "0.1.0"}}})
        response(1)
        send({"method": "initialized", "params": {}})
        if args.list_models:
            send({"id": 99, "method": "model/list", "params": {}})
            listed = response(99)
            print(json.dumps(listed, ensure_ascii=False))
            return
        send({"id": 2, "method": "thread/start", "params": {
            "cwd": str(root), "ephemeral": True, "model": args.model}})
        started = response(2)
        thread_id = started["thread"]["id"]
        send({"id": 3, "method": "turn/start", "params": {
            "threadId": thread_id,
            "input": [{"type": "text", "text": "Ответь одним словом: OK"}]}})
        turn = response(3)
        turn_id = turn["turn"]["id"]
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            message = messages.get(timeout=deadline - time.monotonic())
            if message is None:
                process.wait(timeout=5)
                raise RuntimeError(f"app-server exited with code {process.returncode}; see {error_log.name}")
            if message.get("method") == "turn/completed":
                finished = message["params"]["turn"]
                if finished["id"] != turn_id:
                    continue
                status = finished["status"]
                print(f"app-server turn status={status}; thread_id={thread_id}; turn_id={turn_id}")
                if status != "completed":
                    raise RuntimeError(f"Codex turn status: {status}; details: {finished}")
                return
        raise TimeoutError("Codex turn did not complete")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        error_log.close()


if __name__ == "__main__":
    main()
