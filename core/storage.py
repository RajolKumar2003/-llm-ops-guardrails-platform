"""
storage.py
----------
Everything to do with saving and reading back what happened when the app
called the LLM. Using SQLite here on purpose - it's a single file, ships
with Python, and needs zero setup. For a real production system you'd
swap this for Postgres/BigQuery, but the table shape and the questions
you ask of it (cost over time, guardrail trigger rate, latency spread)
stay exactly the same, which is the point of keeping this layer separate
from the LLM-calling code in llm_client.py.
"""

import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "platform.db"


def _ensure_db_folder():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_connection():
    """Small wrapper so every function doesn't repeat the open/close boilerplate."""
    _ensure_db_folder()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Creates the two tables we need if they don't already exist. Safe to
    call on every app startup - CREATE TABLE IF NOT EXISTS is idempotent."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                prompt TEXT NOT NULL,
                response TEXT,
                model TEXT,
                latency_s REAL,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                estimated_cost_usd REAL,
                injection_detected INTEGER DEFAULT 0,
                pii_detected INTEGER DEFAULT 0,
                blocked INTEGER DEFAULT 0,
                block_reason TEXT,
                used_own_key INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS eval_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                suite TEXT NOT NULL,
                question TEXT,
                passed INTEGER,
                score REAL,
                notes TEXT
            )
        """)


def log_call(prompt, response, model, latency_s, prompt_tokens, completion_tokens,
             total_tokens, estimated_cost_usd, injection_detected=False,
             pii_detected=False, blocked=False, block_reason=None, used_own_key=False):
    """One row per LLM call. This is the raw material every chart on the
    dashboard is built from, so keep it complete even for blocked calls -
    a blocked call is itself a data point worth seeing on the dashboard."""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO calls (
                timestamp, prompt, response, model, latency_s,
                prompt_tokens, completion_tokens, total_tokens, estimated_cost_usd,
                injection_detected, pii_detected, blocked, block_reason, used_own_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            time.time(), prompt, response, model, latency_s,
            prompt_tokens, completion_tokens, total_tokens, estimated_cost_usd,
            int(injection_detected), int(pii_detected), int(blocked), block_reason,
            int(used_own_key),
        ))


def log_eval_result(suite, question, passed, score, notes=""):
    """Records one question's result from an eval run (see eval/run_eval.py).
    Keeping every historical run, not just the latest, is what lets the
    dashboard show a quality trend over time instead of a single snapshot."""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO eval_runs (timestamp, suite, question, passed, score, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (time.time(), suite, question, int(passed), score, notes))


def get_recent_calls(limit=200):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM calls ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_eval_history(suite=None, limit=500):
    with get_connection() as conn:
        if suite:
            rows = conn.execute(
                "SELECT * FROM eval_runs WHERE suite = ? ORDER BY timestamp DESC LIMIT ?",
                (suite, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM eval_runs ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def get_summary_stats():
    """Aggregate numbers for the dashboard's top cards. Doing this as one
    SQL query rather than pulling all rows into Python and summing them
    in a loop is the difference that actually matters once this table has
    thousands of rows instead of a few dozen."""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*) as total_calls,
                SUM(blocked) as total_blocked,
                SUM(injection_detected) as total_injection_flags,
                SUM(pii_detected) as total_pii_flags,
                AVG(latency_s) as avg_latency,
                SUM(estimated_cost_usd) as total_cost,
                SUM(total_tokens) as total_tokens
            FROM calls
        """).fetchone()
        return dict(row) if row else {}
