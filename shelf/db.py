"""SQLite access layer. Stdlib only. Nothing to install."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "shelf.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # Background collectors and a hand-collection session write to the same
    # file. WAL allows concurrent readers alongside one writer, and a generous
    # busy timeout makes the brief overlap between two committers wait rather
    # than raise "database is locked" and lose an answer someone just typed.
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created, so an
    in-flight collection never has to be thrown away and restarted."""
    for table, column, ddl in (
        ("brands", "case_sensitive", "INTEGER NOT NULL DEFAULT 0"),
        ("mentions", "prompted", "INTEGER NOT NULL DEFAULT 0"),
        ("runs", "finish_reason", "TEXT"),
    ):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def upsert_brand(conn, name: str, domain: str | None, is_focus: bool,
                 aliases: list[str], case_sensitive: bool = False) -> int:
    cur = conn.execute(
        """INSERT INTO brands (name, domain, is_focus, aliases, case_sensitive)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
               domain=excluded.domain,
               is_focus=excluded.is_focus,
               aliases=excluded.aliases,
               case_sensitive=excluded.case_sensitive
           RETURNING id""",
        (name, domain, int(is_focus), json.dumps(aliases), int(case_sensitive)),
    )
    return cur.fetchone()[0]


def insert_prompt(conn, text: str, intent: str, persona: str, stage: str, subject: str | None) -> int:
    cur = conn.execute(
        """INSERT INTO prompts (text, intent, persona, stage, subject, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(text) DO UPDATE SET text=excluded.text
           RETURNING id""",
        (text, intent, persona, stage, subject, now()),
    )
    return cur.fetchone()[0]


def record_run(conn, *, prompt_id, engine, model, grounded, rep,
               latency_ms=None, response=None, error=None, finish_reason=None) -> int | None:
    """Insert a run, or overwrite a previous *failed* attempt at the same slot.

    Without the overwrite branch a rate-limited run is permanently stuck: the
    resume query re-queues it because error IS NOT NULL, then the UNIQUE
    constraint rejects the insert and the failure is silently kept forever.
    A successful run is never overwritten. That would let a re-run quietly
    mutate collected data.
    """
    try:
        cur = conn.execute(
            """INSERT INTO runs
                 (prompt_id, engine, model, grounded, rep, requested_at, latency_ms,
                  response, error, finish_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (prompt_id, engine, model, int(grounded), rep, now(), latency_ms, response,
             error, finish_reason),
        )
        return cur.fetchone()[0]
    except sqlite3.IntegrityError:
        cur = conn.execute(
            """UPDATE runs
                  SET requested_at=?, latency_ms=?, response=?, error=?, finish_reason=?
                WHERE prompt_id=? AND engine=? AND model=? AND grounded=? AND rep=?
                  AND error IS NOT NULL
              RETURNING id""",
            (now(), latency_ms, response, error, finish_reason,
             prompt_id, engine, model, int(grounded), rep),
        )
        row = cur.fetchone()
        return row[0] if row else None


def brands(conn, focus_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM brands"
    if focus_only:
        sql += " WHERE is_focus = 1"
    return list(conn.execute(sql + " ORDER BY name"))


def pending_runs(conn, engine: str, model: str, grounded: bool, reps: int, seed: int = 1337):
    """Prompts x repetitions not yet executed for this engine/model.

    Returned in a deterministic shuffled order, NOT prompt-id order. Prompt ids
    are sorted by intent, so id order means a run that stops early (rate limit,
    laptop closed, quota) yields a dataset made entirely of one intent and one
    stage. Shuffling makes any prefix of the collection a representative
    sample, so interim numbers are meaningful and an interrupted sweep is still
    usable. The seed keeps it reproducible.
    """
    done = {
        (r["prompt_id"], r["rep"])
        for r in conn.execute(
            "SELECT prompt_id, rep FROM runs WHERE engine=? AND model=? AND grounded=? AND error IS NULL",
            (engine, model, int(grounded)),
        )
    }
    out = []
    for p in conn.execute("SELECT * FROM prompts ORDER BY id"):
        for rep in range(reps):
            if (p["id"], rep) not in done:
                out.append((p, rep))

    import random
    random.Random(seed).shuffle(out)
    return out
