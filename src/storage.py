"""SQLite persistence layer.

Two de-duplication rules:
  1. Same canonical URL      -> same paper (primary key `uid`).
  2. Same normalised title   -> treated as a duplicate across sources.
Also keeps a log of every fetch run so failures are visible over time.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from src.models import Paper

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    uid           TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL,
    source_name   TEXT NOT NULL,
    title         TEXT NOT NULL,
    title_key     TEXT NOT NULL,
    url           TEXT NOT NULL,
    abstract      TEXT,
    authors       TEXT,              -- JSON list
    published     TEXT,              -- ISO-8601, may be NULL
    fetched_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_papers_title_key ON papers(title_key);
CREATE INDEX IF NOT EXISTS idx_papers_published ON papers(published);

CREATE TABLE IF NOT EXISTS fetch_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    status      TEXT NOT NULL,       -- ok | error
    n_fetched   INTEGER DEFAULT 0,
    n_new       INTEGER DEFAULT 0,
    message     TEXT
);

-- Stage 1: one row per paper that went through pre-filtering / screening.
CREATE TABLE IF NOT EXISTS screening (
    uid           TEXT PRIMARY KEY REFERENCES papers(uid),
    status        TEXT NOT NULL,     -- scored | excluded
    score         INTEGER,
    market_focus  TEXT,
    topic         TEXT,
    reason        TEXT,              -- LLM reason, or exclusion reason
    model         TEXT,
    screened_at   TEXT NOT NULL
);

-- Stage 2: full newsletter analysis for selected papers.
CREATE TABLE IF NOT EXISTS analysis (
    uid           TEXT PRIMARY KEY REFERENCES papers(uid),
    payload       TEXT NOT NULL,     -- JSON: summary, key_takeaways, relevance, market_focus, ...
    market_focus  TEXT NOT NULL,
    model         TEXT,
    analysed_at   TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def save_papers(self, papers: list[Paper]) -> int:
        """Insert papers that are not already stored. Returns number of new rows."""
        new = 0
        with self._conn() as conn:
            for p in papers:
                exists = conn.execute(
                    "SELECT 1 FROM papers WHERE uid = ? OR title_key = ?",
                    (p.uid, p.title_key),
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    """INSERT INTO papers (uid, source_id, source_name, title, title_key,
                       url, abstract, authors, published, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        p.uid, p.source_id, p.source_name, p.title, p.title_key,
                        p.url, p.abstract, json.dumps(p.authors),
                        p.published.isoformat() if p.published else None,
                        p.fetched_at.isoformat(),
                    ),
                )
                new += 1
        return new

    def log_fetch(self, source_id: str, status: str, n_fetched: int = 0,
                  n_new: int = 0, message: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO fetch_log (run_at, source_id, status, n_fetched, n_new, message)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (datetime.now(timezone.utc).isoformat(), source_id, status,
                 n_fetched, n_new, message[:500]),
            )

    def count(self, source_id: str | None = None) -> int:
        with self._conn() as conn:
            if source_id:
                row = conn.execute("SELECT COUNT(*) FROM papers WHERE source_id = ?", (source_id,)).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM papers").fetchone()
        return row[0]

    def recent(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM papers ORDER BY COALESCE(published, fetched_at) DESC LIMIT ?",
                (limit,),
            ).fetchall()

    # -- analysis stage ------------------------------------------------------
    def candidates(self, since_iso: str) -> list[dict]:
        """Papers first seen, or published, on/after `since_iso`."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM papers
                   WHERE fetched_at >= ? OR (published IS NOT NULL AND published >= ?)
                   ORDER BY COALESCE(published, fetched_at) DESC""",
                (since_iso, since_iso),
            ).fetchall()
        return [_paper_dict(r) for r in rows]

    def all_papers(self) -> list[dict]:
        with self._conn() as conn:
            return [_paper_dict(r) for r in conn.execute("SELECT * FROM papers").fetchall()]

    def all_analyses(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT uid, market_focus, model FROM analysis").fetchall()
        return [dict(r) for r in rows]

    def get_screening(self) -> dict[str, dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM screening").fetchall()
        return {r["uid"]: dict(r) for r in rows}

    def save_screening(self, results: list[dict], model: str | None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            for r in results:
                conn.execute(
                    """INSERT OR REPLACE INTO screening
                       (uid, status, score, market_focus, topic, reason, model, screened_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (r["uid"], r["status"], r.get("score"), r.get("market_focus"),
                     r.get("topic"), r.get("reason"), model, now),
                )

    def get_analysis(self, uid: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM analysis WHERE uid = ?", (uid,)).fetchone()
        if not row:
            return None
        return {**json.loads(row["payload"]), "model": row["model"], "analysed_at": row["analysed_at"]}

    def save_analysis(self, uid: str, payload: dict, model: str | None) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO analysis (uid, payload, market_focus, model, analysed_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (uid, json.dumps(payload, ensure_ascii=False), payload["market_focus"], model,
                 datetime.now(timezone.utc).isoformat()),
            )


def _paper_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["authors"] = json.loads(d["authors"] or "[]")
    return d
