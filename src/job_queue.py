"""Persistent Tracker job reservation and nonblocking execution."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import sha256
from hmac import compare_digest
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import secrets
import sqlite3
import time
from threading import Event, Lock


class JobError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class JobQueue:
    FOLDER_GRANT_OPERATIONS = frozenset(("get_tasks_folder", "get_task_projects",
        "register_task_project", "create_task_folder", "create_local_task_folder"))
    def __init__(self, path, runner=None, task_validator=None):
        self.path = Path(path)
        if not self.path.is_absolute() or self.path.parent.is_symlink() or self.path.is_symlink():
            raise ValueError("Job index requires a plain absolute path")
        import fcntl
        self.owner_lock = os.open(str(self.path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.owner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(self.owner_lock)
            raise RuntimeError("Job index already has an owner") from error
        os.fchmod(self.owner_lock, 0o600)
        self.lock = Lock()
        self.parent_locks_lock = Lock()
        self.parent_locks = {}
        self.runner = runner
        self.task_validator = task_validator
        self.stops = {}
        self.pool = ThreadPoolExecutor(max_workers=8) if runner else None
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        os.chmod(self.path, 0o600)
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, "
                        "control_hash TEXT NOT NULL, status TEXT NOT NULL, prompt TEXT NOT NULL, "
                        "reply TEXT, result TEXT, rights_version INTEGER NOT NULL DEFAULT 0, "
                        "run_id TEXT, recovery_status TEXT, codex_thread_id TEXT, events TEXT)")
        columns = {column[1] for column in self.db.execute("PRAGMA table_info(jobs)")}
        for column in ("run_id", "recovery_status", "codex_thread_id", "events"):
            if column not in columns:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_turn ON jobs(thread_id) WHERE "
                        "status IN ('queued','running','waiting','stop_requested','unknown')")
        self.db.execute("CREATE TABLE IF NOT EXISTS parent_rights (thread_id TEXT PRIMARY KEY, "
                        "version INTEGER NOT NULL, repositories TEXT NOT NULL, pending INTEGER NOT NULL, "
                        "pending_repositories TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS parent_capabilities (thread_id TEXT PRIMARY KEY, "
                        "task_key TEXT NOT NULL, token_hash TEXT, expires_at INTEGER NOT NULL DEFAULT 0)")
        self.db.execute("CREATE TABLE IF NOT EXISTS folder_grants (token_hash TEXT PRIMARY KEY, "
                        "thread_id TEXT NOT NULL, operation TEXT NOT NULL, arguments TEXT NOT NULL, "
                        "expires_at INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0)")
        # A bootstrap attempt may have been interrupted after token issuance.
        # Never carry an unspent one-shot grant into a new Tracker process.
        self.db.execute("UPDATE folder_grants SET used=1 WHERE used=0")
        self.db.execute("UPDATE jobs SET recovery_status=status, status='unknown' "
                        "WHERE status IN ('queued','running','stop_requested')")
        self.db.commit()
        if runner and hasattr(runner, "set_start_guard"):
            runner.set_start_guard(self._start_guard)
        if runner and hasattr(runner, "set_capability_provider"):
            runner.set_capability_provider(self.issue_capability)

    def issue_capability(self, thread_id):
        """Rotate a per-parent capability immediately before an agent starts."""
        with self.lock:
            row = self.db.execute("SELECT task_key FROM parent_capabilities WHERE thread_id=?",
                                  (thread_id,)).fetchone()
            if (row is None or self._rights(thread_id)[2] or
                    (self.task_validator is not None and not self.task_validator(row[0]))):
                return None
            token = secrets.token_urlsafe(32)
            self.db.execute("UPDATE folder_grants SET used=1 WHERE thread_id=?", (thread_id,))
            self.db.execute("UPDATE parent_capabilities SET token_hash=?, expires_at=? WHERE thread_id=?",
                            (sha256(token.encode()).hexdigest(), int(time.time()) + 24 * 3600, thread_id))
            self.db.commit()
            return row[0], token

    def _authorize_capability_unlocked(self, token):
        if not isinstance(token, str) or len(token) < 32 or len(token) > 256:
            return None
        digest = sha256(token.encode()).hexdigest()
        row = self.db.execute("SELECT c.task_key, c.token_hash, c.expires_at, r.pending "
                              "FROM parent_capabilities c JOIN parent_rights r ON r.thread_id=c.thread_id "
                              "WHERE c.token_hash=?", (digest,)).fetchone()
        if not row or not compare_digest(row[1], digest) or row[2] <= int(time.time()) or row[3]:
            return None
        return row[0]

    def authorize_capability(self, token):
        with self.lock:
            return self._authorize_capability_unlocked(token)

    @contextmanager
    def scoped_access(self, token):
        """Serialize a scoped operation with revocation and rights changes."""
        with self.lock:
            yield self._authorize_capability_unlocked(token)

    def issue_folder_grant(self, thread_id, operation, arguments, ttl=300):
        """Trusted host API: issue one exact, one-use folder operation, never via MCP."""
        if (not isinstance(thread_id, str) or not thread_id or len(thread_id) > 128 or
                operation not in self.FOLDER_GRANT_OPERATIONS or not isinstance(arguments, dict) or
                type(ttl) is not int or not 1 <= ttl <= 900):
            raise JobError("INVALID_REQUEST")
        token = secrets.token_urlsafe(32)
        with self.lock:
            _, _, pending, _ = self._rights(thread_id)
            if pending:
                raise JobError("RIGHTS_UPDATE_PENDING")
            binding = self.db.execute("SELECT token_hash,expires_at FROM parent_capabilities "
                                      "WHERE thread_id=?", (thread_id,)).fetchone()
            if not binding or not binding[0] or binding[1] <= int(time.time()):
                raise JobError("TASK_NOT_FOUND")
            self.db.execute("INSERT INTO folder_grants VALUES (?,?,?,?,?,0)",
                            (sha256(token.encode()).hexdigest(), thread_id, operation,
                             json.dumps(arguments, sort_keys=True), min(binding[1], int(time.time()) + ttl)))
            self.db.commit()
        return token

    def _folder_grant_unlocked(self, token):
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            return None
        row = self.db.execute("SELECT g.operation,g.arguments,g.expires_at,g.used,r.pending,c.token_hash,c.expires_at "
                              "FROM folder_grants g JOIN parent_rights r ON r.thread_id=g.thread_id "
                              "JOIN parent_capabilities c ON c.thread_id=g.thread_id "
                              "WHERE g.token_hash=?", (sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or row[2] <= int(time.time()) or row[3] or row[4] or not row[5] or row[6] <= int(time.time()):
            return None
        return row[0], json.loads(row[1])

    def authorize_folder_grant(self, token):
        with self.lock:
            return self._folder_grant_unlocked(token)

    def revoke_folder_grant(self, token):
        with self.lock:
            self.db.execute("UPDATE folder_grants SET used=1 WHERE token_hash=?",
                            (sha256(token.encode()).hexdigest(),))
            self.db.commit()

    @contextmanager
    def folder_grant_access(self, token, operation, arguments):
        with self.lock:
            grant = self._folder_grant_unlocked(token)
            allowed = grant == (operation, arguments)
            if allowed:
                self.db.execute("UPDATE folder_grants SET used=1 WHERE token_hash=?",
                                (sha256(token.encode()).hexdigest(),))
                self.db.commit()
            yield allowed

    def scoped_snapshot_root_unlocked(self, token, view_path):
        """Map one agent repository view to a selected trusted host path."""
        if not isinstance(view_path, str) or not view_path.startswith("/workspace/repos/"):
            return None
        path = PurePosixPath(view_path)
        if (str(path) != view_path or any(part in (".", "..") for part in view_path.split("/")) or
                len(path.parts) < 4):
            return None
        repo_id = path.parts[3]
        row = self.db.execute("SELECT r.repositories,r.pending FROM parent_capabilities c "
                              "JOIN parent_rights r ON r.thread_id=c.thread_id WHERE c.token_hash=?",
                              (sha256(token.encode()).hexdigest(),)).fetchone()
        catalog = getattr(self.runner, "catalog", None)
        if not row or row[1] or not isinstance(catalog, dict) or repo_id not in json.loads(row[0]) or repo_id not in catalog:
            return None
        base = Path(catalog[repo_id])
        root = base.joinpath(*path.parts[4:])
        return str(root) if self._plain_repo_path(base, root) else None

    @staticmethod
    def _plain_repo_path(base, root):
        try:
            if not base.is_absolute() or base.is_symlink() or root != base and base not in root.parents:
                return False
            current = base
            for part in root.relative_to(base).parts:
                current /= part
                if current.is_symlink():
                    return False
            return root.is_dir() and root.resolve(strict=True) == root
        except (OSError, ValueError):
            return False

    def snapshot_root_allowed_unlocked(self, token, project_root):
        """Called under scoped_access: snapshots must stay within current repo rights."""
        if not isinstance(project_root, str) or not Path(project_root).is_absolute():
            return False
        digest = sha256(token.encode()).hexdigest()
        row = self.db.execute("SELECT c.thread_id, r.repositories, r.pending "
                              "FROM parent_capabilities c JOIN parent_rights r ON r.thread_id=c.thread_id "
                              "WHERE c.token_hash=?", (digest,)).fetchone()
        catalog = getattr(self.runner, "catalog", None)
        if not row or row[2] or not isinstance(catalog, dict):
            return False
        try:
            selected = json.loads(row[1])
            root = Path(project_root)
            return any(repo_id in catalog and
                       self._plain_repo_path(Path(catalog[repo_id]), root)
                       for repo_id in selected)
        except (TypeError, ValueError):
            return False

    def _revoke_capability(self, thread_id):
        self.db.execute("UPDATE parent_capabilities SET token_hash=NULL, expires_at=0 WHERE thread_id=?",
                        (thread_id,))
        self.db.execute("UPDATE folder_grants SET used=1 WHERE thread_id=?", (thread_id,))

    def _parent_lock(self, thread_id):
        with self.parent_locks_lock:
            return self.parent_locks.setdefault(thread_id, Lock())

    def _rights(self, thread_id):
        row = self.db.execute("SELECT version, repositories, pending, pending_repositories "
                              "FROM parent_rights WHERE thread_id=?", (thread_id,)).fetchone()
        if row is None:
            defaults = list(getattr(self.runner, "default_repository_ids", ()))
            self.db.execute("INSERT INTO parent_rights VALUES (?,?,?,?,NULL)",
                            (thread_id, 0, json.dumps(defaults), 0))
            self.db.commit()
            return (0, json.dumps(defaults), 0, None)
        return row

    @contextmanager
    def _start_guard(self, thread_id, version):
        with self._parent_lock(thread_id):
            with self.lock:
                current = self._rights(thread_id)
                allowed = not current[2] and current[0] == version
            yield allowed

    def close(self):
        if self.pool:
            self.pool.shutdown(wait=True, cancel_futures=True)
        self.db.close()
        os.close(self.owner_lock)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @staticmethod
    def _text(args, key):
        value = args.get(key)
        if not isinstance(value, str) or not value or len(value) > 65536:
            raise JobError("INVALID_REQUEST")
        return value

    def _job(self, args):
        job_id = self._text(args, "job_id")
        key = self._text(args, "control_key")
        row = self.db.execute("SELECT id, thread_id, control_hash, status, result, rights_version, "
                              "codex_thread_id, events "
                              "FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or not compare_digest(row[2], sha256(key.encode()).hexdigest()):
            raise JobError("JOB_NOT_FOUND")
        return row

    @staticmethod
    def _public(row):
        return {"job_id": row[0], "thread_id": row[1], "status": row[3],
                "result": row[4], "rights_version": row[5],
                "codex_thread_id": row[6], "events": row[7]}

    def _dispatch(self, job_id, thread_id, run_id, prompt, reply=None, native_thread_id=None,
                  repositories=None, rights_version=0):
        if self.pool:
            stop = Event()
            self.stops[job_id] = stop
            try:
                self.pool.submit(self._run, job_id, thread_id, run_id, prompt, reply, stop,
                                 native_thread_id, repositories, rights_version)
            except Exception:
                self.stops.pop(job_id, None)
                self.db.execute("UPDATE jobs SET status='unknown' WHERE id=?", (job_id,))
                self.db.commit()
                return "unknown"
        return "queued"

    def _run(self, job_id, thread_id, run_id, prompt, reply, stop, native_thread_id,
             repositories, rights_version):
        with self.lock:
            if self.db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] != "queued":
                self.stops.pop(job_id, None)
                return
            self.db.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
            self.db.commit()
        try:
            outcome = self.runner(job_id, thread_id, run_id, prompt, reply, stop, native_thread_id,
                                  repositories, rights_version)
            if not isinstance(outcome, dict) or outcome.get("status") not in ("completed", "waiting", "stopped", "unknown"):
                raise ValueError("Runner returned no terminal or waiting state")
        except Exception:
            outcome = {"status": "unknown"}
        self._finish(job_id, run_id, outcome, stop, rights_version)

    def _finish(self, job_id, run_id, outcome, stop, rights_version=0):
        status, result = outcome["status"], outcome.get("result")
        events, native_thread_id = outcome.get("events"), outcome.get("codex_thread_id")
        if result is not None and not isinstance(result, str):
            status, result = "unknown", None
        if events is not None and (not isinstance(events, str) or len(events) > 8 * 1024 * 1024):
            status, events = "unknown", None
        with self.lock:
            current = self.db.execute("SELECT status, run_id, codex_thread_id, rights_version, thread_id FROM jobs WHERE id=?",
                                      (job_id,)).fetchone()
            if current is None or current[1] != run_id:
                self.stops.pop(job_id, None)
                return
            if current[0] == "stop_requested" and (status != "stopped" or not stop.is_set()):
                status, result = "unknown", None
            if current[3] != rights_version and status in ("waiting", "completed"):
                status, result = "unknown", None
            parent_version, _, pending, _ = self._rights(current[4])
            if status in ("waiting", "completed") and (pending or parent_version != rights_version):
                status, result = "unknown", None
            if current[2] and native_thread_id != current[2] and (
                    native_thread_id is not None or status in ("waiting", "completed")):
                status, result = "unknown", None
                native_thread_id = None
            if native_thread_id is not None and not isinstance(native_thread_id, str):
                status, result = "unknown", None
                native_thread_id = None
            self.db.execute("UPDATE jobs SET status=?, result=?, events=?, codex_thread_id=COALESCE(?,codex_thread_id), "
                            "recovery_status=NULL WHERE id=?",
                            (status, result, events, native_thread_id, job_id))
            self._revoke_capability(current[4])
            self.db.commit()
            self.stops.pop(job_id, None)

    def reconcile(self, controller):
        """Inspect persisted attempts; never create or start one on recovery."""
        with self.lock:
            rows = self.db.execute("SELECT id, thread_id, run_id, recovery_status, codex_thread_id, rights_version FROM jobs "
                                   "WHERE status='unknown' AND run_id IS NOT NULL").fetchall()
        for job_id, thread_id, run_id, prior, native_thread_id, rights_version in rows:
            try:
                outcome = controller.inspect_job(job_id, thread_id, run_id)
            except Exception:
                with self.lock:
                    self._revoke_capability(thread_id)
                    self.db.commit()
                continue
            with self.lock:
                current_version, _, pending, _ = self._rights(thread_id)
                if pending or current_version != rights_version:
                    continue
                if outcome["status"] in ("completed", "waiting") and prior != "stop_requested" and (not native_thread_id or native_thread_id == outcome.get("codex_thread_id")):
                    self.db.execute("UPDATE jobs SET status=?, result=?, events=?, "
                                    "codex_thread_id=COALESCE(?,codex_thread_id), recovery_status=NULL WHERE id=?",
                                    (outcome["status"], outcome.get("result"), outcome.get("events"),
                                     outcome.get("codex_thread_id"), job_id))
                    self._revoke_capability(thread_id)
                elif outcome["status"] == "running" and self.pool:
                    stop = Event()
                    if prior == "stop_requested":
                        stop.set()
                    self.stops[job_id] = stop
                    try:
                        self.pool.submit(self._monitor_existing, controller, job_id, thread_id,
                                         run_id, stop, rights_version)
                    except Exception:
                        self.stops.pop(job_id, None)
                        continue
                    self.db.execute("UPDATE jobs SET status=?, recovery_status=NULL WHERE id=?",
                                    ("stop_requested" if stop.is_set() else "running", job_id))
                else:
                    self._revoke_capability(thread_id)
                self.db.commit()

    def _monitor_existing(self, controller, job_id, thread_id, run_id, stop, rights_version):
        try:
            outcome = controller.monitor(job_id, thread_id, run_id, stop)
        except Exception:
            outcome = {"status": "unknown"}
        self._finish(job_id, run_id, outcome, stop, rights_version)

    def _update_repositories(self, args):
        with self.lock:
            row = self._job(args)
        if not self.runner or not hasattr(self.runner, "stop_parent"):
            raise JobError("REPOSITORY_UPDATE_NOT_READY")
        expected = args.get("expected_rights_version")
        requested = args.get("repositories")
        if type(expected) is not int or expected < 0:
            raise JobError("INVALID_REQUEST")
        try:
            selected = self.runner.validate_repository_ids(requested)
        except (AttributeError, ValueError, TypeError):
            raise JobError("INVALID_REPOSITORY")
        thread_id = row[1]
        with self._parent_lock(thread_id):
            with self.lock:
                self._job(args)
                version, _, pending, pending_json = self._rights(thread_id)
                if pending:
                    if version != expected or json.loads(pending_json) != selected:
                        raise JobError("RIGHTS_UPDATE_PENDING")
                else:
                    if version != expected:
                        raise JobError("RIGHTS_VERSION_CONFLICT")
                    self.db.execute("UPDATE parent_rights SET pending=1, pending_repositories=? "
                                    "WHERE thread_id=?", (json.dumps(selected), thread_id))
                    self._revoke_capability(thread_id)
                    self.db.commit()
            try:
                confirmed = self.runner.stop_parent(thread_id)
            except Exception:
                confirmed = False
            if not confirmed:
                return {"status": "pending", "rights_version": version}
            if hasattr(self.runner, "validate_mount_safety"):
                try:
                    self.runner.validate_mount_safety(selected)
                except ValueError:
                    raise JobError("REPOSITORY_MOUNT_UNSAFE")
            with self.lock:
                self.db.execute("UPDATE parent_rights SET version=?, repositories=?, pending=0, "
                                "pending_repositories=NULL WHERE thread_id=? AND version=? AND pending=1",
                                (version + 1, json.dumps(selected), thread_id, version))
                self.db.commit()
            return {"status": "updated", "rights_version": version + 1,
                    "repositories": selected}

    def _stop(self, args):
        with self.lock:
            row = self._job(args)
            if row[3] == "unknown" and self.runner and hasattr(self.runner, "stop_job"):
                run_id = self.db.execute("SELECT run_id FROM jobs WHERE id=?", (row[0],)).fetchone()[0]
                if not run_id:
                    return {"job_id": row[0], "status": "unknown"}
            else:
                if row[3] == "queued":
                    status = "stopped"
                    self.stops.pop(row[0], None)
                elif row[3] == "waiting":
                    status = "stopped"
                elif row[3] == "running":
                    status = "stop_requested"
                    if row[0] in self.stops:
                        self.stops[row[0]].set()
                else:
                    status = row[3]
                self.db.execute("UPDATE jobs SET status=? WHERE id=?", (status, row[0]))
                self._revoke_capability(row[1])
                self.db.commit()
                return {"job_id": row[0], "status": status}
        try:
            status = self.runner.stop_job(row[0], row[1], run_id)["status"]
        except Exception:
            status = "unknown"
        with self.lock:
            self.db.execute("UPDATE jobs SET status=? WHERE id=? AND run_id=? AND status='unknown'",
                            (status, row[0], run_id))
            self._revoke_capability(row[1])
            self.db.commit()
        return {"job_id": row[0], "status": status}

    def call(self, name, args):
        if name == "bootstrap_folder":
            if not self.runner or not hasattr(self.runner, "bootstrap_folder"):
                raise JobError("BOOTSTRAP_NOT_READY")
            thread_id = self._text(args, "thread_id")
            operation = self._text(args, "operation")
            arguments = args.get("arguments")
            if operation not in self.FOLDER_GRANT_OPERATIONS or not isinstance(arguments, dict):
                raise JobError("INVALID_REQUEST")
            grant = self.issue_folder_grant(thread_id, operation, arguments)
            try:
                response = self.runner.bootstrap_folder(thread_id, operation, arguments, grant)
                if not isinstance(response, dict):
                    raise JobError("BOOTSTRAP_FAILED")
                if response.get("isError"):
                    detail = response.get("structuredContent")
                    raise JobError(detail.get("code", "BOOTSTRAP_FAILED") if isinstance(detail, dict)
                                   else "BOOTSTRAP_FAILED")
                return {"operation": operation, "result": response.get("structuredContent")}
            finally:
                self.revoke_folder_grant(grant)
        if name == "start":
            thread_id = self._text(args, "thread_id")
            prompt = self._text(args, "prompt")
            task_key = args.get("task_key")
            if task_key is None and getattr(self.runner, "task_update", None):
                raise JobError("TASK_NOT_FOUND")
            if task_key is not None:
                if (not isinstance(task_key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*-[0-9]+", task_key)
                        or self.task_validator is None or not self.task_validator(task_key)):
                    raise JobError("TASK_NOT_FOUND")
            if len(thread_id) > 128 or any(ord(c) < 33 or ord(c) > 126 for c in thread_id):
                raise JobError("INVALID_REQUEST")
            job_id = secrets.token_hex(16)
            run_id = secrets.token_hex(16)
            key = secrets.token_urlsafe(32)
            with self.lock:
                rights_version, rights_json, pending, _ = self._rights(thread_id)
                if pending:
                    raise JobError("RIGHTS_UPDATE_PENDING")
                bound = self.db.execute("SELECT task_key FROM parent_capabilities WHERE thread_id=?",
                                        (thread_id,)).fetchone()
                if bound and task_key != bound[0]:
                    raise JobError("TASK_BINDING_CONFLICT")
                if self.runner and hasattr(self.runner, "validate_mount_safety"):
                    try:
                        self.runner.validate_mount_safety(json.loads(rights_json))
                    except ValueError:
                        raise JobError("REPOSITORY_MOUNT_UNSAFE")
                try:
                    self.db.execute("INSERT INTO jobs (id,thread_id,control_hash,status,prompt,run_id,rights_version) "
                                    "VALUES (?,?,?,?,?,?,?)",
                                    (job_id, thread_id, sha256(key.encode()).hexdigest(), "queued",
                                     prompt, run_id, rights_version))
                    if task_key is not None and bound is None:
                        self.db.execute("INSERT INTO parent_capabilities (thread_id,task_key) VALUES (?,?)",
                                        (thread_id, task_key))
                    self.db.commit()
                except sqlite3.IntegrityError as error:
                    raise JobError("THREAD_BUSY") from error
                status = self._dispatch(job_id, thread_id, run_id, prompt, repositories=json.loads(rights_json),
                                        rights_version=rights_version)
            return {"job_id": job_id, "thread_id": thread_id, "control_key": key, "status": status}
        if name == "stop":
            return self._stop(args)
        if name == "update_repositories":
            return self._update_repositories(args)
        with self.lock:
            row = self._job(args)
            if name == "status":
                result = self._public(row)
                if args.get("summary_only") is True:
                    result.pop("events", None)
                return result
            if name == "reply":
                rights_version, rights_json, pending, _ = self._rights(row[1])
                if pending:
                    raise JobError("RIGHTS_UPDATE_PENDING")
                if row[3] != "waiting":
                    raise JobError("JOB_NOT_WAITING")
                bound = self.db.execute("SELECT task_key FROM parent_capabilities WHERE thread_id=?",
                                        (row[1],)).fetchone()
                if (getattr(self.runner, "task_update", None) and
                        (bound is None or self.task_validator is None or
                         not self.task_validator(bound[0]))):
                    raise JobError("TASK_NOT_FOUND")
                if self.runner and hasattr(self.runner, "validate_mount_safety"):
                    try:
                        self.runner.validate_mount_safety(json.loads(rights_json))
                    except ValueError:
                        raise JobError("REPOSITORY_MOUNT_UNSAFE")
                reply = self._text(args, "reply")
                prompt = self.db.execute("SELECT prompt FROM jobs WHERE id=?", (row[0],)).fetchone()[0]
                if getattr(self.runner, "requires_resume", False) and not row[6]:
                    raise JobError("NATIVE_THREAD_UNKNOWN")
                run_id = secrets.token_hex(16)
                self.db.execute("UPDATE jobs SET status='queued', reply=?, result=NULL, events=NULL, "
                                "run_id=?, rights_version=? WHERE id=?",
                                (reply, run_id, rights_version, row[0]))
                self.db.commit()
                status = self._dispatch(row[0], row[1], run_id, prompt, reply, row[6],
                                        json.loads(rights_json), rights_version)
                return {"job_id": row[0], "status": status}
            raise JobError("UNKNOWN_TOOL")

    def handle(self, name, args):
        try:
            if not isinstance(args, dict):
                raise JobError("INVALID_REQUEST")
            return {"ok": True, "data": self.call(name, args)}
        except JobError as error:
            return {"ok": False, "code": error.code, "message": str(error)}
        except Exception:
            return {"ok": False, "code": "INTERNAL_ERROR", "message": "Job operation failed"}

