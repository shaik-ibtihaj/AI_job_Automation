"""Private durable job outcomes, run checkpoints, and process-held run lock."""
from contextlib import AbstractContextManager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class AlreadyRunning(RuntimeError):
    pass


class RunLock(AbstractContextManager):
    """OS file lock, automatically released on crash; a stale filename is harmless."""
    def __init__(self, path):
        self.path, self.stream = Path(path), None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0, os.SEEK_END)
        if not self.stream.tell():
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise AlreadyRunning("Another workflow is running. Close or stop it before starting again.") from exc
        return self

    def __exit__(self, *args):
        if self.stream:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_UN)
            self.stream.close()


PROTECTED = {"submitted_with_confirmation": 3, "outcome_uncertain": 2, "submission_in_progress": 1}


class Store(AbstractContextManager):
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                key TEXT PRIMARY KEY, payload TEXT NOT NULL, match TEXT NOT NULL,
                outcome TEXT NOT NULL, reason TEXT NOT NULL, first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL, confirmation TEXT, preparation TEXT);
            CREATE TABLE IF NOT EXISTS aliases (alias TEXT PRIMARY KEY, job_key TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (
                id TEXT PRIMARY KEY, job_key TEXT NOT NULL, started_at TEXT NOT NULL, result TEXT);
            CREATE TABLE IF NOT EXISTS searches (key TEXT PRIMARY KEY, cursor TEXT, complete INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, report TEXT);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
        """)
        self.db.commit()
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(searches)")}
        for name, declaration in (("page_offset", "INTEGER NOT NULL DEFAULT 0"), ("page_hash", "TEXT")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE searches ADD COLUMN {name} {declaration}")
        self.db.commit()

    def __exit__(self, *args):
        self.db.close()

    def all_jobs(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM jobs ORDER BY first_seen,key")]

    def get(self, key):
        row = self.db.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def upsert(self, job, match):
        aliases = job["identity_aliases"]
        placeholders = ",".join("?" for _ in aliases)
        known = list(self.db.execute(
            f"SELECT DISTINCT jobs.* FROM jobs JOIN aliases ON aliases.job_key=jobs.key WHERE aliases.alias IN ({placeholders})", aliases))
        now = utcnow()
        if known:
            chosen = max(known, key=lambda row: PROTECTED.get(row["outcome"], 0))
            key = chosen["key"]
            old = json.loads(chosen["payload"])
            for field in ("linkedin_job_url", "application_url"):
                job[field] = job[field] or old.get(field, "")
            for row in known:
                if row["key"] == key:
                    continue
                self.db.execute("UPDATE aliases SET job_key=? WHERE job_key=?", (key, row["key"]))
                self.db.execute("UPDATE attempts SET job_key=? WHERE job_key=?", (key, row["key"]))
                self.db.execute("DELETE FROM jobs WHERE key=?", (row["key"],))
            first_seen = min(row["first_seen"] for row in known)
            self.db.execute("UPDATE jobs SET payload=?, match=?, last_seen=?, first_seen=? WHERE key=?",
                            (json.dumps(job), json.dumps(match), now, first_seen, key))
        else:
            key = hashlib.sha256(aliases[0].encode()).hexdigest()
            self.db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
                            (key, json.dumps(job), json.dumps(match), "discovered", "Awaiting routing", now, now, None, None))
        for alias in aliases:
            self.db.execute("INSERT OR REPLACE INTO aliases VALUES (?,?)", (alias, key))
        self.db.commit()
        return key, bool(known)

    def outcome(self, key, status, reason, *, confirmation=None, preparation=None):
        row = self.get(key)
        if row["outcome"] in PROTECTED and status not in {"submitted_with_confirmation", "outcome_uncertain"}:
            return
        self.db.execute("UPDATE jobs SET outcome=?,reason=?,confirmation=COALESCE(?,confirmation),preparation=? WHERE key=?",
                        (status, reason, json.dumps(confirmation) if confirmation else None,
                         json.dumps(preparation) if preparation else None, key))
        self.db.commit()

    def begin_submission(self, key):
        if self.get(key)["outcome"] in PROTECTED:
            raise RuntimeError("Submission is already confirmed, in flight, or uncertain; manual checking required.")
        attempt = str(uuid.uuid4())
        with self.db:
            self.db.execute("INSERT INTO attempts VALUES (?,?,?,?)",
                            (attempt, key, utcnow(), None))
            self.db.execute("UPDATE jobs SET outcome='submission_in_progress',reason='Awaiting platform confirmation' WHERE key=?", (key,))
        return attempt

    def finish_attempt(self, attempt, result):
        self.db.execute("UPDATE attempts SET result=? WHERE id=?", (result, attempt))
        self.db.commit()

    def recover_interrupted(self):
        self.db.execute("UPDATE jobs SET outcome='outcome_uncertain', reason='Interrupted during submission; check platform before any retry' WHERE outcome='submission_in_progress'")
        count = self.db.execute("SELECT changes()").fetchone()[0]
        self.db.commit()
        return count

    def search_progress(self, key):
        row = self.db.execute("SELECT cursor,complete,page_offset,page_hash FROM searches WHERE key=?", (key,)).fetchone()
        return dict(row) if row else {}

    def save_search(self, key, cursor, complete, page_offset=0, page_hash=None):
        self.db.execute("INSERT OR REPLACE INTO searches (key,cursor,complete,page_offset,page_hash) VALUES (?,?,?,?,?)",
                        (key, cursor, int(complete), page_offset, page_hash))
        self.db.commit()

    def reset_searches(self):
        self.db.execute("DELETE FROM searches")
        self.db.commit()

    def start_run(self):
        run = str(uuid.uuid4())
        self.db.execute("INSERT INTO runs VALUES (?,?,NULL)", (run, utcnow()))
        self.db.commit()
        return run

    def finish_run(self, run, report):
        self.db.execute("UPDATE runs SET report=? WHERE id=?", (json.dumps(report), run))
        self.db.commit()
