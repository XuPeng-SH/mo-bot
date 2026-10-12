import hashlib
import json
import sqlite3
import time
from pathlib import Path


ACTIVE = ("reviewing", "publishing", "canceling")


JOBS_SQL = """CREATE TABLE jobs(
    id TEXT PRIMARY KEY, repo TEXT NOT NULL, pr INTEGER, target_number INTEGER, head TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','reviewing','publishing','canceling','done','failed','superseded')),
    metadata TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
    slot_id INTEGER REFERENCES slots(id), comment_id INTEGER, comment_state TEXT,
    attempts INTEGER NOT NULL DEFAULT 0, error TEXT, result TEXT, turn_dir TEXT,
    retry_at REAL NOT NULL DEFAULT 0,
    claim_attempted INTEGER NOT NULL DEFAULT 0, review_id INTEGER, review_event TEXT,
    review_attempted INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'review' CHECK(kind IN ('review','work')),
    target_type TEXT NOT NULL DEFAULT 'pr' CHECK(target_type IN ('pr','issue')),
    trigger_key TEXT NOT NULL DEFAULT '', phase TEXT NOT NULL DEFAULT 'triage', data TEXT NOT NULL DEFAULT '{}'
)"""


class Store:
    """One workflow authority; slots retain only conversation/process ownership."""

    def __init__(
        self,
        path: Path,
        service_id: str,
        sessions: int,
        worker_sessions: int = 0,
        classifier_sessions: int = 0,
    ):
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
        """)
        existing = self.db.execute(
            "SELECT value FROM meta WHERE key='service_id'"
        ).fetchone()
        if existing and existing[0] != service_id:
            self.db.close()
            raise ValueError("state database belongs to another service ID")
        self.service_id = service_id
        self.migrate()
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO meta VALUES('service_id',?)", (service_id,)
            )
        for role, count in (
            ("review", sessions),
            ("worker", worker_sessions),
            ("classifier", classifier_sessions),
        ):
            saved = self.db.execute(
                "SELECT value FROM meta WHERE key=?", (f"pool_capacity:{role}",)
            ).fetchone()
            self.resize_pool(role, int(saved[0]) if saved else count, persist=False)

    def capacity(self, role):
        return self.db.execute(
            "SELECT COUNT(*) FROM slots WHERE role=? AND enabled=1", (role,)
        ).fetchone()[0]

    def resize_pool(self, role, count, defaults=None, *, persist=True):
        if (
            role not in ("review", "worker", "classifier")
            or type(count) is not int
            or not 0 <= count <= 8
            or (role == "classifier" and count > 1)
        ):
            raise ValueError("invalid pool role or session count")
        with self.db:
            self.db.execute("UPDATE slots SET enabled=0 WHERE role=?", (role,))
            for index in range(count):
                row = self.db.execute(
                    "SELECT id FROM slots WHERE role=? AND pool_index=?", (role, index)
                ).fetchone()
                if row:
                    self.db.execute(
                        "UPDATE slots SET enabled=1 WHERE id=?", (row["id"],)
                    )
                else:
                    slot_id = self.db.execute(
                        "SELECT COALESCE(MAX(id),-1)+1 FROM slots"
                    ).fetchone()[0]
                    settings = (defaults or {}).get(index, {})
                    self.db.execute(
                        "INSERT INTO slots(id,role,pool_index,enabled,model,effort,node) VALUES(?,?,?,1,?,?,?)",
                        (
                            slot_id,
                            role,
                            index,
                            settings.get("model"),
                            settings.get("effort"),
                            settings.get("node", "local"),
                        ),
                    )
                    if settings.get("endpoint"):
                        self.db.execute(
                            "INSERT INTO meta(key,value) VALUES(?,?)",
                            (f"slot_endpoint:{slot_id}", settings["endpoint"]),
                        )
            if persist:
                self.db.execute(
                    "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"pool_capacity:{role}", str(count)),
                )

    def migrate(self):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(slots)")}
            for name, decl in (
                ("role", "TEXT NOT NULL DEFAULT 'review'"),
                ("pool_index", "INTEGER"),
                ("enabled", "INTEGER NOT NULL DEFAULT 1"),
                ("node", "TEXT NOT NULL DEFAULT 'local'"),
            ):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE slots ADD COLUMN {name} {decl}")
            self.db.execute("UPDATE slots SET pool_index=id WHERE pool_index IS NULL")
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS slot_role_index ON slots(role,pool_index)"
            )
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(jobs)")}
            if not columns or "target_number" not in columns:
                self.db.execute(JOBS_SQL.replace("jobs(", "jobs_new(", 1))
                if columns:
                    common = [
                        r[1]
                        for r in self.db.execute("PRAGMA table_info(jobs_new)")
                        if r[1] in columns
                    ]
                    names = ",".join(common)
                    self.db.execute(
                        f"INSERT INTO jobs_new({names}) SELECT {names} FROM jobs"
                    )
                    self.db.execute(
                        "UPDATE jobs_new SET target_number=pr WHERE target_number IS NULL"
                    )
                    self.db.execute("DROP TABLE jobs")
                self.db.execute("ALTER TABLE jobs_new RENAME TO jobs")
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(jobs)")}
            for name, decl in (
                ("review_id", "INTEGER"),
                ("review_event", "TEXT"),
                ("review_attempted", "INTEGER NOT NULL DEFAULT 0"),
                ("retry_at", "REAL NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
            self.db.execute("DROP INDEX IF EXISTS active_slot")
            self.db.execute("DROP INDEX IF EXISTS active_pr")
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS review_identity ON jobs(repo,pr,head) WHERE kind='review'"
            )
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS work_identity ON jobs(repo,target_number,target_type,trigger_key,head) WHERE kind='work'"
            )
            self.db.execute(
                "CREATE UNIQUE INDEX active_slot ON jobs(slot_id) WHERE state IN ('reviewing','canceling') OR (kind='work' AND state IN ('queued','publishing') AND slot_id IS NOT NULL)"
            )
            self.db.execute(
                "CREATE UNIQUE INDEX active_pr ON jobs(repo,pr) WHERE state IN ('reviewing','publishing','canceling') OR (kind='work' AND state IN ('queued','publishing') AND slot_id IS NOT NULL)"
            )
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS active_issue ON jobs(repo,target_number) WHERE kind='work' AND target_type='issue' AND (state IN ('reviewing','publishing','canceling') OR (state='queued' AND slot_id IS NOT NULL))"
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created DESC)"
            )
            self.db.execute("CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state)")
            statements = [
                "CREATE TABLE IF NOT EXISTS evidence(repo TEXT NOT NULL, number INTEGER NOT NULL, head TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, disposition TEXT, job_id TEXT, PRIMARY KEY(repo,number,head,key))",
                "CREATE TABLE IF NOT EXISTS effects(key TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), kind TEXT NOT NULL, intent TEXT NOT NULL, attempted INTEGER NOT NULL DEFAULT 0, result TEXT, error TEXT, created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS issue_members(job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, repo TEXT NOT NULL, number INTEGER NOT NULL, issue_id INTEGER NOT NULL, trigger_key TEXT NOT NULL, PRIMARY KEY(job_id,number))",
                "CREATE INDEX IF NOT EXISTS issue_members_lookup ON issue_members(repo,number,trigger_key)",
            ]
            for statement in statements:
                if statement.strip():
                    self.db.execute(statement)
            effect_columns = {
                r[1] for r in self.db.execute("PRAGMA table_info(effects)")
            }
            if "receipt" not in effect_columns:
                self.db.execute("ALTER TABLE effects ADD COLUMN receipt TEXT")
            # Backfill singleton issue jobs written by older service versions.
            # Group membership is committed atomically with metadata going forward.
            for row in self.db.execute(
                "SELECT id,repo,target_number,trigger_key,metadata FROM jobs WHERE kind='work' AND target_type='issue'"
            ).fetchall():
                metadata = json.loads(row["metadata"])
                members = metadata.get("members") or [
                    {
                        "number": row["target_number"],
                        "issue_id": metadata.get("identity", {}).get("issue_id"),
                        "trigger_key": row["trigger_key"],
                    }
                ]
                for member in members:
                    if (
                        type(member.get("number")) is int
                        and type(member.get("issue_id")) is int
                        and isinstance(member.get("trigger_key"), str)
                    ):
                        self.db.execute(
                            "INSERT OR IGNORE INTO issue_members(job_id,repo,number,issue_id,trigger_key) VALUES(?,?,?,?,?)",
                            (
                                row["id"],
                                row["repo"],
                                member["number"],
                                member["issue_id"],
                                member["trigger_key"],
                            ),
                        )
            if self.db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("workflow migration failed foreign-key integrity")

    def key(self, repo: str, pr: int, head: str) -> str:
        return hashlib.sha256(
            f"{self.service_id}:{repo}:{pr}:{head}".encode()
        ).hexdigest()[:24]

    def enqueue(self, candidate: dict) -> bool:
        kind = candidate.get("kind", "review")
        target_type = candidate.get("target_type", "pr")
        trigger = candidate.get("trigger_key", "")
        key = (
            self.key(candidate["repo"], candidate["number"], candidate["headRefOid"])
            if kind == "review"
            else self.work_key(candidate)
        )
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            # Repeated snapshots, including a previously failed task, never re-enqueue it.
            if self.db.execute("SELECT 1 FROM jobs WHERE id=?", (key,)).fetchone():
                return False
            members = candidate.get("members", [])
            if target_type == "issue":
                if not members:
                    members = [
                        {
                            "number": candidate["number"],
                            "issue_id": candidate["identity"]["issue_id"],
                            "trigger_key": trigger,
                        }
                    ]
                for member in members:
                    existing = self.db.execute(
                        "SELECT j.state,m.trigger_key FROM issue_members m JOIN jobs j ON j.id=m.job_id WHERE m.repo=? AND m.number=?",
                        (candidate["repo"], member["number"]),
                    ).fetchall()
                    if any(
                        row["trigger_key"] == member["trigger_key"] for row in existing
                    ):
                        return False
                    if any(
                        row["state"] in ACTIVE
                        and row["trigger_key"] != member["trigger_key"]
                        for row in existing
                    ):
                        raise ValueError(
                            "issue already belongs to an active task with changed input"
                        )
            self.db.execute(
                """UPDATE jobs SET state='superseded',error='newer commit queued',updated=?
                WHERE repo=? AND pr=? AND kind=? AND target_type=? AND head<>? AND state='queued' AND slot_id IS NULL""",
                (
                    now,
                    candidate["repo"],
                    candidate["number"] if target_type == "pr" else None,
                    kind,
                    target_type,
                    candidate["headRefOid"],
                ),
            )
            self.db.execute(
                "INSERT INTO jobs(id,repo,pr,head,state,metadata,created,updated,kind,target_type,trigger_key,target_number) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    candidate["repo"],
                    candidate["number"] if target_type == "pr" else None,
                    candidate["headRefOid"],
                    "queued",
                    json.dumps(candidate),
                    now,
                    now,
                    kind,
                    target_type,
                    trigger,
                    candidate["number"],
                ),
            )
            if target_type == "issue":
                self._insert_issue_members(key, candidate["repo"], members)
            for atom in candidate.get("atoms", []):
                self.db.execute(
                    "UPDATE evidence SET disposition='queued',job_id=? WHERE repo=? AND number=? AND head=? AND key=? AND (disposition IS NULL OR disposition IN ('blocked','failed','superseded'))",
                    (
                        key,
                        candidate["repo"],
                        candidate["number"],
                        candidate["headRefOid"],
                        atom["key"],
                    ),
                )
        return True

    def _insert_issue_members(self, job_id, repo, members):
        for member in members:
            self.db.execute(
                "INSERT INTO issue_members(job_id,repo,number,issue_id,trigger_key) VALUES(?,?,?,?,?)",
                (
                    job_id,
                    repo,
                    member["number"],
                    member["issue_id"],
                    member["trigger_key"],
                ),
            )

    def issue_task_for_member(self, repo, number, trigger_key=None):
        query = "SELECT j.* FROM issue_members m JOIN jobs j ON j.id=m.job_id WHERE m.repo=? AND m.number=?"
        params = [repo, number]
        if trigger_key is not None:
            query += " AND m.trigger_key=?"
            params.append(trigger_key)
        return self.db.execute(
            query + " ORDER BY j.created DESC LIMIT 1", params
        ).fetchone()

    def merge_issue_member(self, job_id, member, metadata, *, replay_phase=None):
        """Atomically extend one issue task and claim the added issue identity."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            job = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["kind"] != "work" or job["target_type"] != "issue":
                return False
            if job["state"] == "queued" and job["slot_id"] is None:
                pass
            elif job["state"] == "reviewing" and job["phase"] in ("triage", "design"):
                pass
            else:
                return False
            if self.db.execute(
                "SELECT 1 FROM issue_members WHERE job_id=? AND repo=? AND number=?",
                (job_id, job["repo"], member["number"]),
            ).fetchone():
                return False
            prior = self.db.execute(
                "SELECT j.state,m.trigger_key FROM issue_members m JOIN jobs j ON j.id=m.job_id WHERE m.repo=? AND m.number=?",
                (job["repo"], member["number"]),
            ).fetchall()
            if any(row["trigger_key"] == member["trigger_key"] for row in prior):
                return False
            if any(row["state"] in ACTIVE for row in prior):
                return False
            data = json.loads(job["data"])
            if replay_phase:
                data["replay_phase"] = replay_phase
            self.db.execute(
                "UPDATE jobs SET metadata=?,data=?,updated=? WHERE id=?",
                (json.dumps(metadata), json.dumps(data), time.time(), job_id),
            )
            self._insert_issue_members(job_id, job["repo"], [member])
            return True

    def job(self, key: str):
        return self.db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()

    def jobs(self, states=None, limit=None):
        if states:
            query = (
                "SELECT * FROM jobs WHERE state IN ("
                + ",".join("?" for _ in states)
                + ") ORDER BY created"
            )
            parameters = list(states)
        else:
            query, parameters = "SELECT * FROM jobs ORDER BY created DESC", []
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        return self.db.execute(query, parameters).fetchall()

    def slots(self, *, include_disabled=False):
        if include_disabled:
            return self.db.execute("SELECT * FROM slots ORDER BY id").fetchall()
        return self.db.execute(
            "SELECT s.* FROM slots s WHERE enabled=1 OR EXISTS(SELECT 1 FROM jobs j WHERE j.slot_id=s.id AND (j.state IN ('reviewing','canceling') OR (j.kind='work' AND j.state IN ('queued','publishing')))) ORDER BY id"
        ).fetchall()

    def summaries(self, limit=200):
        # Do not hydrate immutable CI/comment evidence on every status tick.
        # Keep recent jobs plus every active/affine job visible.
        return self.db.execute(
            """SELECT id,repo,pr,target_number,target_type,head,kind,phase,state,
            created,updated,slot_id,attempts,error,retry_at,comment_id,comment_state,
            review_id,review_event,review_attempted,
            json_object('title',json_extract(metadata,'$.title'),
                'ci',json_extract(metadata,'$.ci'),'forced',json_extract(metadata,'$.forced'),
                'members',json_extract(metadata,'$.members')) AS metadata,
            json_object('manual_reconciliation',json_extract(data,'$.manual_reconciliation'),
                'publication_next_at',json_extract(data,'$.publication_next_at'),
                'publication_failures',json_extract(data,'$.publication_failures'),
                'publication_started_at',json_extract(data,'$.publication_started_at'),
                'phase_started_at',json_extract(data,'$.phase_started_at'),
                'paused',json_extract(data,'$.paused')) AS data,
            CASE WHEN result IS NULL THEN NULL ELSE json_object(
                'assessment',json_object('decision',json_extract(result,'$.assessment.decision'),
                    'summary',json_extract(result,'$.assessment.summary')),
                'session_id',json_extract(result,'$.session_id'),
                'summary',json_extract(result,'$.summary'),
                'model',json_extract(result,'$.model'),'effort',json_extract(result,'$.effort')) END AS result
            FROM jobs WHERE id IN(SELECT id FROM jobs ORDER BY created DESC LIMIT ?)
                OR state IN ('reviewing','publishing','canceling')
                OR (kind='work' AND state='queued' AND slot_id IS NOT NULL)
            ORDER BY created DESC""",
            (limit,),
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
            "retry_at",
            "claim_attempted",
            "comment_state",
            "review_id",
            "review_event",
            "review_attempted",
            "phase",
            "data",
            "pr",
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
            "enabled",
            "node",
        }:
            raise ValueError("unsupported slot update")
        with self.db:
            self.db.execute(
                "UPDATE slots SET "
                + ",".join(k + "=?" for k in fields)
                + " WHERE id=?",
                (*fields.values(), slot),
            )

    def bind_node(self, slot, node, identity):
        """A conversation ID is meaningful only on its original endpoint."""
        key = f"slot_endpoint:{slot['id']}"
        previous = self.db.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        changed = slot["node"] != node or (previous and previous[0] != identity)
        with self.db:
            if changed:
                leased = self.db.execute(
                    "SELECT 1 FROM jobs WHERE slot_id=? AND (state IN ('reviewing','canceling') OR (kind='work' AND state IN ('queued','publishing')))",
                    (slot["id"],),
                ).fetchone()
                if leased:
                    raise ValueError(
                        f"cannot move leased session {slot['id']} to a different node"
                    )
                self.db.execute(
                    "UPDATE slots SET session_id=NULL,ready=0,generation=generation+1,node=? WHERE id=?",
                    (node, slot["id"]),
                )
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, identity),
            )

    def claim(self, slot: int, eligible=None):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            slot_row = self.db.execute(
                "SELECT * FROM slots WHERE id=?", (slot,)
            ).fetchone()
            if not slot_row or not slot_row["ready"]:
                return None
            if slot_row["role"] == "classifier":
                return None
            reserved = self.db.execute(
                "SELECT * FROM jobs WHERE slot_id=? AND kind='work' AND state='queued'",
                (slot,),
            ).fetchone()
            if reserved:
                if json.loads(reserved["data"]).get("paused"):
                    return None
                if reserved["retry_at"] > time.time() or (
                    eligible is not None and reserved["id"] not in eligible
                ):
                    return None
                job = reserved
            else:
                if not slot_row["enabled"]:
                    return None
                if self.db.execute(
                    "SELECT 1 FROM jobs WHERE slot_id=? AND (state IN ('reviewing','canceling') OR (kind='work' AND state='publishing'))",
                    (slot,),
                ).fetchone():
                    return None
                kind = "review" if slot_row["role"] == "review" else "work"
                choices = self.db.execute(
                    """SELECT j.* FROM jobs j WHERE j.state='queued' AND j.kind=? AND j.retry_at<=? AND (j.slot_id IS NULL OR j.slot_id=?)
                    AND COALESCE(json_extract(j.data,'$.paused'),0)=0
                    AND NOT EXISTS(SELECT 1 FROM jobs a WHERE a.id<>j.id AND a.repo=j.repo AND (a.pr=j.pr OR (a.target_type='issue' AND j.target_type='issue' AND (a.target_number=j.target_number OR EXISTS(SELECT 1 FROM issue_members am JOIN issue_members jm ON jm.repo=am.repo AND jm.number=am.number WHERE am.job_id=a.id AND jm.job_id=j.id)))) AND
                    (a.state IN ('reviewing','publishing','canceling') OR (a.kind='work' AND a.state='queued' AND a.slot_id IS NOT NULL))) ORDER BY j.created""",
                    (kind, time.time(), slot),
                ).fetchall()
                job = next(
                    (
                        j
                        for j in choices
                        if (eligible is None or j["id"] in eligible)
                        and not self.uncertain_pr(
                            j["repo"],
                            j["head"],
                            json.loads(j["metadata"])
                            .get("identity", {})
                            .get("head_repo_id"),
                            json.loads(j["metadata"])
                            .get("identity", {})
                            .get("head_ref"),
                        )
                    ),
                    None,
                )
            if job is None:
                return None
            self.db.execute(
                "UPDATE jobs SET state='reviewing',slot_id=?,updated=? WHERE id=?",
                (slot, time.time(), job["id"]),
            )
        return self.job(job["id"])

    def uncertain_pr(self, repo, head, head_repo_id=None, head_ref=None):
        for effect in self.db.execute(
            "SELECT e.intent FROM effects e JOIN jobs j ON j.id=e.job_id WHERE e.kind='create-pr' AND e.attempted=1 AND e.result IS NULL AND j.repo=? AND j.target_type='issue' AND j.state='publishing' AND j.pr IS NULL",
            (repo,),
        ):
            intent = json.loads(effect["intent"])
            if intent["candidate"] == head or (
                head_repo_id is not None
                and intent["head_repo_id"] == head_repo_id
                and intent["head_ref"] == head_ref
            ):
                return True
        return False

    def work_key(self, candidate):
        scope = [
            self.service_id,
            "work",
            candidate.get("target_type", "pr"),
            candidate["repo"],
            candidate["number"],
            candidate["headRefOid"]
            if candidate.get("target_type", "pr") == "pr"
            else None,
            candidate["trigger_key"],
        ]
        return hashlib.sha256(
            json.dumps(scope, separators=(",", ":")).encode()
        ).hexdigest()

    def fresh_evidence(self, repo, number, head, atoms):
        fresh = []
        with self.db:
            for atom in atoms:
                self.db.execute(
                    "INSERT OR IGNORE INTO evidence(repo,number,head,key,payload) VALUES(?,?,?,?,?)",
                    (repo, number, head, atom["key"], json.dumps(atom)),
                )
                row = self.db.execute(
                    "SELECT e.disposition,j.state FROM evidence e LEFT JOIN jobs j ON j.id=e.job_id WHERE e.repo=? AND e.number=? AND e.head=? AND e.key=?",
                    (repo, number, head, atom["key"]),
                ).fetchone()
                if not row["disposition"] or (
                    row["disposition"] in ("blocked", "failed", "superseded")
                    and row["state"] in ("failed", "superseded")
                ):
                    fresh.append(atom)
        return fresh

    def dispose_evidence(self, job, disposition):
        meta = json.loads(job["metadata"])
        with self.db:
            for atom in meta.get("atoms", []):
                self.db.execute(
                    "UPDATE evidence SET disposition=?,job_id=? WHERE repo=? AND number=? AND head=? AND key=?",
                    (
                        disposition,
                        job["id"],
                        job["repo"],
                        job["target_number"],
                        job["head"],
                        atom["key"],
                    ),
                )

    def carry_evidence(self, job, head, disposition):
        with self.db:
            for atom in json.loads(job["metadata"]).get("atoms", []):
                self.db.execute(
                    "INSERT INTO evidence(repo,number,head,key,payload,disposition,job_id) VALUES(?,?,?,?,?,?,?) ON CONFLICT(repo,number,head,key) DO UPDATE SET disposition=excluded.disposition,job_id=excluded.job_id",
                    (
                        job["repo"],
                        job["target_number"],
                        head,
                        atom["key"],
                        json.dumps(atom),
                        disposition,
                        job["id"],
                    ),
                )
            # A discovery racing an uncertain push may already have queued the
            # identical atoms on the candidate. Cancel only wholly handled tasks.
            for queued in self.db.execute(
                "SELECT * FROM jobs WHERE kind='work' AND target_type='pr' AND repo=? AND target_number=? AND head=? AND state='queued' AND slot_id IS NULL",
                (job["repo"], job["target_number"], head),
            ).fetchall():
                atoms = json.loads(queued["metadata"]).get("atoms", [])
                handled = {
                    a["key"] for a in json.loads(job["metadata"]).get("atoms", [])
                }
                if atoms and all(a["key"] in handled for a in atoms):
                    self.db.execute(
                        "UPDATE jobs SET state='superseded',error='evidence already handled by delivered candidate',updated=? WHERE id=?",
                        (time.time(), queued["id"]),
                    )

    def effect(self, key):
        return self.db.execute("SELECT * FROM effects WHERE key=?", (key,)).fetchone()

    def prepare_effect(self, key, job_id, kind, intent):
        encoded = json.dumps(intent, sort_keys=True, separators=(",", ":"))
        with self.db:
            old = self.effect(key)
            if old and old["intent"] != encoded:
                raise ValueError(
                    "effect intent changed; manual reconciliation required"
                )
            self.db.execute(
                "INSERT OR IGNORE INTO effects(key,job_id,kind,intent,created) VALUES(?,?,?,?,?)",
                (key, job_id, kind, encoded, time.time()),
            )
        return self.effect(key)

    def effect_update(self, key, **fields):
        if not fields or not set(fields) <= {"attempted", "result", "error", "receipt"}:
            raise ValueError("invalid effect update")
        with self.db:
            self.db.execute(
                "UPDATE effects SET "
                + ",".join(k + "=?" for k in fields)
                + " WHERE key=?",
                (*fields.values(), key),
            )

    def close(self):
        self.db.close()
