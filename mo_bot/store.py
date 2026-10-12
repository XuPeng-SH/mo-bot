import hashlib
import json
import sqlite3
import time
from pathlib import Path


ACTIVE = ("reviewing", "publishing", "canceling")


class Store:
    """One workflow authority; slots retain only conversation/process ownership."""

    def __init__(self, path: Path, service_id: str, sessions: int):
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS slots(
                id INTEGER PRIMARY KEY, session_id TEXT, generation INTEGER NOT NULL DEFAULT 0,
                model TEXT, effort TEXT, ready INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, repo TEXT NOT NULL, pr INTEGER NOT NULL, head TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN
                    ('queued','reviewing','publishing','canceling','done','failed','superseded')),
                metadata TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                slot_id INTEGER REFERENCES slots(id), comment_id INTEGER, comment_state TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, error TEXT, result TEXT, turn_dir TEXT, claim_attempted INTEGER NOT NULL DEFAULT 0,
                UNIQUE(repo,pr,head)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS active_slot ON jobs(slot_id)
                WHERE state IN ('reviewing','canceling');
            CREATE UNIQUE INDEX IF NOT EXISTS active_pr ON jobs(repo,pr)
                WHERE state IN ('reviewing','publishing','canceling');
        """)
        if "turn_dir" not in {r[1] for r in self.db.execute("PRAGMA table_info(jobs)")}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN turn_dir TEXT")
            self.db.commit()
        existing = self.db.execute(
            "SELECT value FROM meta WHERE key='service_id'"
        ).fetchone()
        if existing and existing[0] != service_id:
            self.db.close()
            raise ValueError("state database belongs to another service ID")
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO meta VALUES('service_id',?)", (service_id,)
            )
            for slot in range(sessions):
                self.db.execute("INSERT OR IGNORE INTO slots(id) VALUES(?)", (slot,))
        self.service_id = service_id
        self.sessions = sessions

    def key(self, repo: str, pr: int, head: str) -> str:
        return hashlib.sha256(
            f"{self.service_id}:{repo}:{pr}:{head}".encode()
        ).hexdigest()[:24]

    def enqueue(self, candidate: dict) -> bool:
        key = self.key(candidate["repo"], candidate["number"], candidate["headRefOid"])
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            # Repeated snapshots, including a previously failed task, never re-enqueue it.
            if self.db.execute("SELECT 1 FROM jobs WHERE id=?", (key,)).fetchone():
                return False
            self.db.execute(
                """UPDATE jobs SET state='superseded',error='newer commit queued',updated=?
                WHERE repo=? AND pr=? AND state='queued'""",
                (now, candidate["repo"], candidate["number"]),
            )
            self.db.execute(
                "INSERT INTO jobs(id,repo,pr,head,state,metadata,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                (
                    key,
                    candidate["repo"],
                    candidate["number"],
                    candidate["headRefOid"],
                    "queued",
                    json.dumps(candidate),
                    now,
                    now,
                ),
            )
        return True

    def job(self, key: str):
        return self.db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()

    def jobs(self, states=None):
        if states:
            query = (
                "SELECT * FROM jobs WHERE state IN ("
                + ",".join("?" for _ in states)
                + ") ORDER BY created"
            )
            return self.db.execute(query, states).fetchall()
        return self.db.execute("SELECT * FROM jobs ORDER BY created DESC").fetchall()

    def slots(self):
        return self.db.execute(
            "SELECT * FROM slots WHERE id<? ORDER BY id", (self.sessions,)
        ).fetchall()

    def update(self, key: str, **fields):
        allowed = {
            "state",
            "slot_id",
            "comment_id",
            "attempts",
            "error",
            "metadata",
            "result",
            "turn_dir",
            "claim_attempted",
            "comment_state",
        }
        if not fields or not set(fields) <= allowed:
            raise ValueError("unsupported job update")
        fields["updated"] = time.time()
        with self.db:
            self.db.execute(
                "UPDATE jobs SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?",
                (*fields.values(), key),
            )

    def slot_update(self, slot: int, **fields):
        if not fields or not set(fields) <= {
            "session_id",
            "generation",
            "model",
            "effort",
            "ready",
        }:
            raise ValueError("unsupported slot update")
        with self.db:
            self.db.execute(
                "UPDATE slots SET "
                + ",".join(k + "=?" for k in fields)
                + " WHERE id=?",
                (*fields.values(), slot),
            )

    def claim(self, slot: int, eligible=None):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            slot_row = self.db.execute(
                "SELECT * FROM slots WHERE id=?", (slot,)
            ).fetchone()
            if not slot_row or not slot_row["ready"]:
                return None
            if self.db.execute(
                "SELECT 1 FROM jobs WHERE slot_id=? AND state IN ('reviewing','canceling')",
                (slot,),
            ).fetchone():
                return None
            choices = self.db.execute("""SELECT j.* FROM jobs j WHERE j.state='queued'
                AND NOT EXISTS(SELECT 1 FROM jobs a WHERE a.repo=j.repo AND a.pr=j.pr
                    AND a.state IN ('reviewing','publishing','canceling')) ORDER BY j.created""").fetchall()
            job = next(
                (j for j in choices if eligible is None or j["id"] in eligible), None
            )
            if job is None:
                return None
            self.db.execute(
                "UPDATE jobs SET state='reviewing',slot_id=?,updated=? WHERE id=?",
                (slot, time.time(), job["id"]),
            )
        return self.job(job["id"])

    def close(self):
        self.db.close()
