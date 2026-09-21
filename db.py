"""SQLite schema and helpers for MoverSync Outreach."""
import sqlite3
import os
from datetime import datetime, date, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "prospects.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS prospects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    priority_rank INTEGER,
    tier TEXT,
    sales_score REAL,
    company_name TEXT,
    decision_maker TEXT,
    direct_email TEXT,
    company_email TEXT,
    phone TEXT,
    city_region TEXT,
    personalization_hook TEXT,
    sales_angle TEXT,

    -- campaign state
    status TEXT DEFAULT 'pending',          -- pending | queued | sent | followup1 | followup2 | breakup | replied | bounced | unsubscribed | skipped
    emails_sent INTEGER DEFAULT 0,
    first_sent_at TEXT,
    last_sent_at TEXT,
    next_followup_due TEXT,
    last_template_used TEXT,

    -- engagement
    replied INTEGER DEFAULT 0,
    replied_at TEXT,
    bounced INTEGER DEFAULT 0,
    bounced_at TEXT,
    bounce_reason TEXT,
    unsubscribed INTEGER DEFAULT 0,

    -- inbox / reply mapping
    reply_subject TEXT,
    reply_snippet TEXT,

    -- bookkeeping
    imported_at TEXT DEFAULT CURRENT_TIMESTAMP,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospects(status);
CREATE INDEX IF NOT EXISTS idx_prospects_tier ON prospects(tier);
CREATE INDEX IF NOT EXISTS idx_prospects_rank ON prospects(priority_rank);

CREATE TABLE IF NOT EXISTS send_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prospect_id INTEGER,
    sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
    template TEXT,
    to_email TEXT,
    cc_email TEXT,
    subject TEXT,
    body TEXT,
    smtp_response TEXT,
    success INTEGER,
    FOREIGN KEY(prospect_id) REFERENCES prospects(id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prospect_id INTEGER,
    at TEXT DEFAULT CURRENT_TIMESTAMP,
    event_type TEXT,
    detail TEXT
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def log_event(prospect_id, event_type, detail=""):
    conn = connect()
    conn.execute(
        "INSERT INTO events (prospect_id, event_type, detail) VALUES (?,?,?)",
        (prospect_id, event_type, detail),
    )
    conn.commit()
    conn.close()


def fetch_due_prospects(limit=50):
    """Return prospects ready to send: pending, or followups due."""
    conn = connect()
    today = date.today().isoformat()
    rows = conn.execute(
        """
        SELECT * FROM prospects
        WHERE (status = 'pending' AND direct_email IS NOT NULL AND direct_email != '')
           OR (status IN ('sent','followup1','followup2')
               AND next_followup_due IS NOT NULL
               AND next_followup_due <= ?)
        ORDER BY priority_rank ASC
        LIMIT ?
        """,
        (today, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_prospect(prospect_id, **fields):
    if not fields:
        return
    keys = ", ".join(f"{k} = ?" for k in fields.keys())
    values = list(fields.values()) + [prospect_id]
    conn = connect()
    conn.execute(f"UPDATE prospects SET {keys} WHERE id = ?", values)
    conn.commit()
    conn.close()


def get_stats():
    conn = connect()
    s = conn.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent,
            SUM(CASE WHEN status='replied' THEN 1 ELSE 0 END) AS replied,
            SUM(CASE WHEN status='bounced' THEN 1 ELSE 0 END) AS bounced,
            SUM(CASE WHEN status='unsubscribed' THEN 1 ELSE 0 END) AS unsubscribed,
            COALESCE(SUM(emails_sent), 0) AS emails_total
        FROM prospects
    """).fetchone()
    conn.close()
    return dict(s)

def sent_today_count():
    """Count successful sends today (UTC)."""
    from datetime import datetime
    today = datetime.utcnow().strftime("%Y-%m-%d")
    conn = connect()
    n = conn.execute(
        "SELECT COUNT(*) FROM send_log WHERE success = 1 AND substr(sent_at, 1, 10) = ?",
        (today,),
    ).fetchone()[0]
    conn.close()
    return n

def get_status_counts():
    """Return dict of {status: count} for all prospects."""
    conn = connect()
    rows = conn.execute("""
        SELECT status, COUNT(*) AS n
        FROM prospects
        GROUP BY status
    """).fetchall()
    conn.close()
    return {r["status"]: r["n"] for r in rows}


def get_tier_counts():
    """Return dict of {tier: count} for all prospects."""
    conn = connect()
    rows = conn.execute("""
        SELECT tier, COUNT(*) AS n
        FROM prospects
        GROUP BY tier
        ORDER BY tier ASC
    """).fetchall()
    conn.close()
    return {r["tier"]: r["n"] for r in rows}