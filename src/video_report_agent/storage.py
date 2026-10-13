"""Neutral SQLite business records. Callers own transactions when passing ``db``.

Run files are exports and execution materials, never a query fallback. Connections
are short lived and are not shared across workers or threads.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

TERMINAL = {"RENDERED", "FAILED", "CANCELLED"}
TEXT_FILES = (
    "asr.json",
    "asr-task.json",
    "source-text-events.jsonl",
    "canonical-transcript.jsonl",
    "transcript-manifest.json",
    "transcript.md",
    "accepted-multi-character-replacements.jsonl",
    "subtitle-ocr-events.jsonl",
    "subtitle-ocr-frame-candidates.jsonl",
    "download/source.info.json",
    "source-context.json",
    "term-review.json",
    "transcript.corrected.md",
)
SCHEMA_VERSION = 1
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS core_migrations(version INTEGER PRIMARY KEY, applied_at REAL)",
    """CREATE TABLE IF NOT EXISTS videos (
        id TEXT PRIMARY KEY, platform TEXT, external_id TEXT, part INTEGER,
        metadata_json TEXT NOT NULL,
        UNIQUE(platform,external_id,part))""",
    """CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY, video_id TEXT REFERENCES videos(id), workspace TEXT NOT NULL,
        input_json TEXT NOT NULL, status_json TEXT NOT NULL, created_at REAL,
        managed INTEGER NOT NULL DEFAULT 0, execution_id TEXT, supervisor_pid INTEGER,
        worker_pid INTEGER, deadline REAL, execution_state TEXT NOT NULL DEFAULT 'QUEUED',
        result_json TEXT, result_execution_id TEXT, finalized INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS runs_video ON runs(video_id,created_at)",
    """CREATE TABLE IF NOT EXISTS run_documents (
        run_id TEXT NOT NULL REFERENCES runs(id), name TEXT NOT NULL, body TEXT NOT NULL,
        PRIMARY KEY(run_id,name))""",
    """CREATE TABLE IF NOT EXISTS reports (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
        transcript_key TEXT, html TEXT NOT NULL, created_at REAL)""",
    """CREATE TABLE IF NOT EXISTS artifacts (
        run_id TEXT NOT NULL REFERENCES runs(id), name TEXT NOT NULL, path TEXT NOT NULL,
        kind TEXT NOT NULL, available INTEGER NOT NULL, registered_at REAL NOT NULL,
        PRIMARY KEY(run_id,name))""",
    """CREATE TABLE IF NOT EXISTS cache_entries (
        kind TEXT NOT NULL, cache_key TEXT NOT NULL, state TEXT NOT NULL
          CHECK(state IN ('PROCESSING','READY','FAILED')),
        producer_run TEXT REFERENCES runs(id), execution_id TEXT, deadline REAL,
        payload_json TEXT, error TEXT, updated_at REAL NOT NULL,
        PRIMARY KEY(kind,cache_key))""",
    """CREATE TABLE IF NOT EXISTS run_cache_uses (
        run_id TEXT NOT NULL REFERENCES runs(id), kind TEXT NOT NULL, cache_key TEXT NOT NULL,
        source_run TEXT REFERENCES runs(id), PRIMARY KEY(run_id,kind))""",
    """CREATE TABLE IF NOT EXISTS run_statistics (
        run_id TEXT PRIMARY KEY REFERENCES runs(id), payload_json TEXT NOT NULL,
        updated_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS execution_events (
        run_id TEXT NOT NULL REFERENCES runs(id), execution_id TEXT NOT NULL,
        sequence INTEGER NOT NULL, payload_json TEXT NOT NULL,
        PRIMARY KEY(run_id,execution_id,sequence))""",
    """CREATE TABLE IF NOT EXISTS import_sources (
        source TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
        imported_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS storage_control (
        id INTEGER PRIMARY KEY CHECK(id=1), writes_opened_at REAL)""",
)


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def connect(path, *, readonly=False):
    path = Path(path).resolve()
    db = sqlite3.connect(
        path.as_uri() + "?mode=ro" if readonly else path,
        uri=readonly,
        timeout=30,
        isolation_level=None,
    )
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


class StaleExecution(ValueError):
    pass


class CacheProductionFailed(RuntimeError):
    pass


class Store:
    def __init__(self, path, *, initialize=True):
        self.path = Path(path).resolve()
        if not initialize:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            for statement in SCHEMA:
                db.execute(statement)
            versions = [row[0] for row in db.execute("SELECT version FROM core_migrations")]
            if versions and max(versions) > SCHEMA_VERSION:
                raise ValueError("Database schema is newer than this Core")
            db.execute(
                "INSERT OR IGNORE INTO core_migrations VALUES (?,?)", (SCHEMA_VERSION, time.time())
            )
            db.execute("INSERT OR IGNORE INTO storage_control(id) VALUES (1)")

    @contextmanager
    def transaction(self, db=None):
        if db is not None:
            if not db.in_transaction:
                raise ValueError("Caller connection must have an active transaction")
            yield db
            return
        connection = connect(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read(self):
        db = connect(self.path, readonly=True)
        try:
            yield db
        finally:
            db.close()

    def register(self, run, metadata, status, *, db=None, managed=False):
        with self.transaction(db) as db:
            identity = metadata.get("video_id")
            platform = external = part = None
            if identity and identity.startswith("bilibili-"):
                _, external, page = identity.split("-")
                platform, part = "bilibili", int(page[1:])
            if identity:
                db.execute(
                    "INSERT INTO videos VALUES (?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                    (identity, platform, external, part, encode(metadata)),
                )
            db.execute(
                "INSERT INTO runs(id,video_id,workspace,input_json,status_json,created_at,"
                "managed,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    run.name,
                    identity,
                    str(run.resolve()),
                    encode(metadata),
                    encode(status),
                    status.get("started_at"),
                    int(managed),
                    time.time(),
                ),
            )

    def run(self, run_id, *, db=None):
        if db is None:
            with self.read() as db:
                return self.run(run_id, db=db)
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        record = dict(row)
        for column in ("input_json", "status_json", "result_json"):
            record[column.removesuffix("_json")] = json.loads(record.pop(column) or "null")
        return record

    def metadata(self, run_id, metadata, *, db=None, execution_id=None):
        with self.transaction(db) as db:
            record = self.run(run_id, db=db)
            if execution_id is not None:
                self.check_execution(record, execution_id)
            db.execute(
                "UPDATE runs SET input_json=?,updated_at=? WHERE id=?",
                (encode(metadata), time.time(), run_id),
            )
            if record["video_id"]:
                db.execute(
                    "UPDATE videos SET metadata_json=? WHERE id=?",
                    (encode(metadata), record["video_id"]),
                )

    @staticmethod
    def check_execution(record, execution_id):
        if record["execution_id"] != execution_id or record["finalized"]:
            raise StaleExecution("Execution no longer owns this run")

    def claim_run(self, run_id, *, db=None, deadline=None):
        with self.transaction(db) as db:
            record = self.run(run_id, db=db)
            if record["execution_state"] != "QUEUED" or record["finalized"]:
                raise StaleExecution("Run has already been claimed")
            execution = uuid.uuid4().hex
            db.execute(
                "UPDATE runs SET execution_id=?,execution_state='CLAIMED',deadline=?,"
                "updated_at=? WHERE id=?",
                (execution, deadline or time.time() + 1800, time.time(), run_id),
            )
            return execution

    def supervise(self, run_id, execution_id):
        with self.transaction() as db:
            record = self.run(run_id, db=db)
            self.check_execution(record, execution_id)
            if record["supervisor_pid"] is not None or record["execution_state"] != "CLAIMED":
                raise StaleExecution("Execution already has a supervisor")
            db.execute(
                "UPDATE runs SET supervisor_pid=?,execution_state='RUNNING' WHERE id=?",
                (os.getpid(), run_id),
            )

    def worker(self, run_id, execution_id, pid):
        with self.transaction() as db:
            record = self.run(run_id, db=db)
            self.check_execution(record, execution_id)
            db.execute("UPDATE runs SET worker_pid=? WHERE id=?", (pid, run_id))

    def stage(self, run_id, execution_id, status):
        with self.transaction() as db:
            self.check_execution(self.run(run_id, db=db), execution_id)
            db.execute(
                "UPDATE runs SET status_json=?,updated_at=? WHERE id=?",
                (encode(status), time.time(), run_id),
            )

    def result(self, run_id, execution_id, status, *, db=None, exited=False):
        with self.transaction(db) as db:
            record = self.run(run_id, db=db)
            self.check_execution(record, execution_id)
            if record["result"] is not None and not exited:
                return record["result"]
            db.execute(
                "UPDATE runs SET result_json=?,result_execution_id=?,"
                "execution_state=?,updated_at=? WHERE id=?",
                (
                    encode(status),
                    execution_id,
                    "EXITED" if exited else "RESULT",
                    time.time(),
                    run_id,
                ),
            )
            return status

    def finalize(self, run_id, execution_id, status, *, db=None):
        with self.transaction(db) as db:
            record = self.run(run_id, db=db)
            if record["finalized"]:
                return record["status"]
            self.check_execution(record, execution_id)
            if status["state"] not in TERMINAL:
                raise ValueError("Finalization requires a terminal result")
            db.execute(
                "UPDATE runs SET status_json=?,execution_state='EXITED',finalized=1,"
                "updated_at=? WHERE id=?",
                (encode(status), time.time(), run_id),
            )
            return status

    def document(self, run_id, name, body, *, db=None):
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("Document must be relative to the run")
        with self.transaction(db) as db:
            db.execute(
                "INSERT INTO run_documents VALUES (?,?,?) ON CONFLICT(run_id,name) "
                "DO UPDATE SET body=excluded.body",
                (run_id, name, body),
            )

    def documents(self, run_id, *, db=None):
        if db is None:
            with self.read() as db:
                return self.documents(run_id, db=db)
        return {
            row["name"]: row["body"]
            for row in db.execute("SELECT name,body FROM run_documents WHERE run_id=?", (run_id,))
        }

    def capture(self, run, execution_id, *, names=TEXT_FILES):
        documents = self.documents(run.name) if names == TEXT_FILES else {}
        documents.update(
            {name: (run / name).read_text() for name in names if (run / name).is_file()}
        )
        with self.transaction() as db:
            self.check_execution(self.run(run.name, db=db), execution_id)
            for name, body in documents.items():
                self.document(run.name, name, body, db=db)
        return documents

    def save_report(self, run, execution_id, html, *, db=None):
        with self.transaction(db) as db:
            self.check_execution(self.run(run.name, db=db), execution_id)
            use = db.execute(
                "SELECT cache_key FROM run_cache_uses WHERE run_id=? AND kind='transcript'",
                (run.name,),
            ).fetchone()
            db.execute(
                "INSERT INTO reports VALUES (?,?,?,?,?) ON CONFLICT(run_id) "
                "DO UPDATE SET html=excluded.html",
                (run.name, run.name, use[0] if use else None, html, time.time()),
            )

    def html(self, run_id):
        with self.read() as db:
            row = db.execute("SELECT html FROM reports WHERE run_id=?", (run_id,)).fetchone()
            return row[0] if row else None

    def export(self, run_id, *, include_report=True):
        with self.read() as db:
            record = self.run(run_id, db=db)
            documents = self.documents(run_id, db=db)
        run = Path(record["workspace"])
        run.mkdir(parents=True, exist_ok=True)
        for name, body in {
            "input.json": encode(record["input"]),
            "status.json": encode(record["status"]),
            **documents,
        }.items():
            path = run / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        if include_report and (html := self.html(run_id)) is not None:
            (run / "report.html").write_text(html)
        return run

    def artifact(self, run_id, name, path, kind, *, db=None):
        path = Path(path)
        with self.transaction(db) as db:
            db.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?,?,?) ON CONFLICT(run_id,name) "
                "DO UPDATE SET path=excluded.path,available=excluded.available",
                (run_id, name, str(path.resolve()), kind, int(path.is_file()), time.time()),
            )

    def statistics(self, run_id, payload, *, db=None):
        with self.transaction(db) as db:
            payload = dict(payload)
            events = payload.pop("pipeline_events", [])
            execution = self.run(run_id, db=db)["execution_id"] or "historical:" + run_id
            for sequence, event in enumerate(events):
                db.execute(
                    "INSERT OR IGNORE INTO execution_events VALUES (?,?,?,?)",
                    (run_id, execution, sequence, encode(event)),
                )
            db.execute(
                "INSERT INTO run_statistics VALUES (?,?,?) ON CONFLICT(run_id) "
                "DO UPDATE SET payload_json=excluded.payload_json,"
                "updated_at=excluded.updated_at",
                (run_id, encode(payload), time.time()),
            )

    def claim_cache(self, kind, key, run_id, execution_id):
        with self.transaction() as db:
            record = self.run(run_id, db=db)
            self.check_execution(record, execution_id)
            row = db.execute(
                "SELECT * FROM cache_entries WHERE kind=? AND cache_key=?", (kind, key)
            ).fetchone()
            if row and row["state"] == "READY":
                return "READY", dict(row)
            if row and row["state"] == "PROCESSING":
                return "WAIT", dict(row)
            db.execute(
                "INSERT INTO cache_entries VALUES (?,?,'PROCESSING',?,?,?,NULL,NULL,?) "
                "ON CONFLICT(kind,cache_key) DO UPDATE SET state='PROCESSING',"
                "producer_run=excluded.producer_run,execution_id=excluded.execution_id,"
                "deadline=excluded.deadline,payload_json=NULL,error=NULL,"
                "updated_at=excluded.updated_at",
                (kind, key, run_id, execution_id, record["deadline"], time.time()),
            )
            return "PRODUCE", None

    def ready_cache(self, kind, key, run_id, execution_id, payload):
        with self.transaction() as db:
            self.check_execution(self.run(run_id, db=db), execution_id)
            changed = db.execute(
                "UPDATE cache_entries SET state='READY',payload_json=?,"
                "updated_at=? WHERE kind=? AND cache_key=? AND state='PROCESSING' "
                "AND producer_run=? AND execution_id=?",
                (encode(payload), time.time(), kind, key, run_id, execution_id),
            )
            if changed.rowcount != 1:
                raise StaleExecution("Cache ownership changed")
            self.use_cache(run_id, kind, key, run_id, db=db)

    def use_cache(self, run_id, kind, key, source_run, *, db=None):
        with self.transaction(db) as db:
            db.execute(
                "INSERT INTO run_cache_uses VALUES (?,?,?,?) ON CONFLICT(run_id,kind) "
                "DO UPDATE SET cache_key=excluded.cache_key,source_run=excluded.source_run",
                (run_id, kind, key, source_run),
            )

    def release_caches(self, run_id, execution_id, reason, *, db=None):
        """Supervisor only: call after the producer's process group has exited."""
        with self.transaction(db) as db:
            db.execute(
                "UPDATE cache_entries SET state='FAILED',error=?,updated_at=? "
                "WHERE producer_run=? AND execution_id=? AND state='PROCESSING'",
                (reason, time.time(), run_id, execution_id),
            )

    def release_cache(self, kind, key, run_id, execution_id, reason):
        with self.transaction() as db:
            db.execute(
                "UPDATE cache_entries SET state='FAILED',error=?,updated_at=? "
                "WHERE kind=? AND cache_key=? AND producer_run=? AND execution_id=? "
                "AND state='PROCESSING'",
                (reason, time.time(), kind, key, run_id, execution_id),
            )

    def cache(self, kind, key):
        with self.read() as db:
            row = db.execute(
                "SELECT * FROM cache_entries WHERE kind=? AND cache_key=?", (kind, key)
            ).fetchone()
            return dict(row) if row else None

    def invalidate_media(self, kind, key, producer, execution):
        with self.transaction() as db:
            db.execute(
                "UPDATE cache_entries SET state='FAILED',error='Media unavailable' "
                "WHERE kind=? AND cache_key=? AND producer_run=? AND execution_id=? "
                "AND state='READY'",
                (kind, key, producer, execution),
            )

    def mark_writes_open(self, *, db=None):
        with self.transaction(db) as db:
            db.execute(
                "UPDATE storage_control SET writes_opened_at=COALESCE(writes_opened_at,?)",
                (time.time(),),
            )

    def reconcile_producer(self, run_id, execution_id):
        """Release only a confirmed dead producer. Active supervisors own deadlines."""
        record = self.run(run_id)
        if record["execution_state"] == "EXITED" and not pid_alive(record["worker_pid"]):
            self.release_caches(run_id, execution_id, "Producer exited before completing cache")
            return True
        if pid_alive(record["supervisor_pid"]):
            return False
        worker = record["worker_pid"]
        if pid_alive(worker):
            # Check the recorded execution before killing an orphan process group.
            import signal

            state, command = process_info(worker)
            if state.startswith("Z"):
                self.release_caches(run_id, execution_id, "Producer exited before completing cache")
                return True
            if execution_id not in command or str(Path(record["workspace"])) not in command:
                return False
            os.killpg(worker, signal.SIGKILL)
            for _ in range(100):
                if not pid_alive(worker):
                    break
                state, _ = process_info(worker)
                if state.startswith("Z"):
                    break
                time.sleep(0.01)
            if pid_alive(worker) and not state.startswith("Z"):
                return False
        self.release_caches(run_id, execution_id, "Producer exited before completing cache")
        return True

    def reconcile_caches(self):
        with self.read() as db:
            producers = db.execute(
                "SELECT DISTINCT producer_run,execution_id "
                "FROM cache_entries WHERE state='PROCESSING'"
            ).fetchall()
        for row in producers:
            self.reconcile_producer(row[0], row[1])


def process_info(pid):
    # Debian slim images do not ship ps. /proc is the Linux process authority.
    proc = Path("/proc") / str(pid)
    if Path("/proc/self/stat").exists():
        try:
            state = (proc / "stat").read_text().rsplit(") ", 1)[1].split()[0]
            command = (
                (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            )
            return state, command
        except FileNotFoundError:
            return "", ""
    import subprocess

    state = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, check=False
    ).stdout.strip()
    command = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False
    ).stdout
    return state, command


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
