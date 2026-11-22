"""SQLite is the durable queue. A job in flight at a crash requires manual review."""

import hashlib
import json
import sqlite3
import time
from contextlib import closing, contextmanager


class Problem(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Store:
    def __init__(self, directory):
        self.path = directory / "tailprint.db"
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS printers (name TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS jobs (
                    actor TEXT, id TEXT, printer TEXT, copies INTEGER, name TEXT,
                    digest TEXT, created INTEGER, state TEXT, detail TEXT DEFAULT '',
                    pdf BLOB, options TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(actor,id));
                CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state,created);
            """)
            if "options" not in {r[1] for r in db.execute("PRAGMA table_info(jobs)")}:
                db.execute(
                    "ALTER TABLE jobs ADD COLUMN options TEXT NOT NULL DEFAULT '{}'"
                )
            db.execute(
                "UPDATE jobs SET state='uncertain',pdf=NULL,detail=? WHERE state='sending'",
                ("Агент перезапущен при передаче. Проверьте очередь принтера.",),
            )

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, timeout=15)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA secure_delete=ON")
            with db:
                yield db

    def published(self):
        with self.db() as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT name FROM printers ORDER BY name COLLATE NOCASE"
                )
            ]

    def publish(self, name, enabled):
        with self.db() as db:
            if enabled:
                db.execute("INSERT OR IGNORE INTO printers VALUES (?)", (name,))
            else:
                db.execute("DELETE FROM printers WHERE name=?", (name,))
                db.execute(
                    "UPDATE jobs SET state='failed',pdf=NULL,detail=? WHERE printer=? AND state='queued'",
                    ("Принтер снят с публикации.", name),
                )

    def enqueue(self, actor, key, printer, copies, name, pdf, options):
        options = json.dumps(options, sort_keys=True)
        digest = hashlib.sha256(pdf).hexdigest()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT printer,copies,name,digest,options FROM jobs WHERE actor=? AND id=?",
                (actor, key),
            ).fetchone()
            if old:
                if (
                    old["printer"],
                    old["copies"],
                    old["name"],
                    old["digest"],
                    old["options"],
                ) != (printer, copies, name, digest, options):
                    raise Problem(409, "Этот номер уже принадлежит другому заданию.")
                return
            if not db.execute(
                "SELECT 1 FROM printers WHERE name=?", (printer,)
            ).fetchone():
                raise Problem(409, "Принтер не опубликован.")
            count, size = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(length(pdf)),0) FROM jobs WHERE pdf IS NOT NULL"
            ).fetchone()
            if count >= 100 or size + len(pdf) > 250 * 1024**2:
                raise Problem(
                    429, "Очередь заполнена. Попробуйте после передачи текущих заданий."
                )
            db.execute(
                "INSERT INTO jobs (actor,id,printer,copies,name,digest,created,state,pdf,options) VALUES (?,?,?,?,?,?,?,'queued',?,?)",
                (
                    actor,
                    key,
                    printer,
                    copies,
                    name,
                    digest,
                    int(time.time()),
                    pdf,
                    options,
                ),
            )

    def take(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute(
                "SELECT * FROM jobs WHERE state='queued' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if job:
                db.execute(
                    "UPDATE jobs SET state='sending' WHERE actor=? AND id=?",
                    (job["actor"], job["id"]),
                )
            return job

    def finish(self, job, state, detail):
        with self.db() as db:
            db.execute(
                "UPDATE jobs SET state=?,detail=?,pdf=NULL WHERE actor=? AND id=?",
                (state, detail, job["actor"], job["id"]),
            )

    def history(self, actor):
        with self.db() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,name,printer,copies,state,detail,created FROM jobs WHERE actor=? ORDER BY rowid DESC LIMIT 60",
                    (actor,),
                )
            ]
