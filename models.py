"""Database access layer and models."""
import sqlite3
from datetime import datetime
from config import DB_FILE


def get_db_connection():
    """Get a database connection with row factory and foreign keys enabled."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_patient(conn, chart_number):
    """Fetch a patient by chart number."""
    return conn.execute(
        "SELECT * FROM patients WHERE chart_number=?",
        (chart_number,)
    ).fetchone()


def update_patient(conn, chart_number, **kwargs):
    """Update patient fields. Uses kwargs for column=value pairs."""
    if not kwargs:
        return
    columns = ", ".join([f"{k}=?" for k in kwargs.keys()])
    values = list(kwargs.values()) + [chart_number]
    conn.execute(f"UPDATE patients SET {columns} WHERE chart_number=?", values)


def get_all_patients(conn):
    """Fetch all patients."""
    return [dict(r) for r in conn.execute("SELECT * FROM patients").fetchall()]


def get_room_transfers(conn, chart_number):
    """Fetch room transfer history for a patient."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM room_transfers WHERE chart_number=? ORDER BY id ASC",
        (chart_number,)
    ).fetchall()]


def get_active_transfer(conn, chart_number):
    """Get the current active room transfer (end_date IS NULL)."""
    return conn.execute(
        "SELECT * FROM room_transfers WHERE chart_number=? AND end_date IS NULL",
        (chart_number,)
    ).fetchone()


def get_user(conn, user_id):
    """Fetch a user by ID."""
    return conn.execute(
        "SELECT id, username, role, is_active FROM users WHERE id=?",
        (user_id,)
    ).fetchone()


def get_user_by_username(conn, username):
    """Fetch a user by username."""
    return conn.execute(
        "SELECT * FROM users WHERE username=?",
        (username,)
    ).fetchone()


def get_all_users(conn):
    """Fetch all users ordered by role and username."""
    return [dict(row) for row in conn.execute(
        "SELECT id, username, role, is_active, created_at, last_login_at FROM users ORDER BY role DESC, username COLLATE NOCASE"
    ).fetchall()]


def get_global_settings(conn):
    """Fetch all global settings as a dictionary."""
    settings = {}
    for row in conn.execute("SELECT key, value FROM global_settings").fetchall():
        try:
            settings[row['key']] = max(0, int(row['value']))
        except (TypeError, ValueError):
            settings[row['key']] = 0
    return settings


def get_global_setting(conn, key, default=0):
    """Fetch a single global setting."""
    row = conn.execute("SELECT value FROM global_settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return max(0, int(row['value']))
    except (TypeError, ValueError):
        return default


def count_occupied_beds(conn, unit, exclude_chart_number=None):
    """Count occupied beds in a unit (handles ICU 1/2 as single pool)."""
    occupied_units = ("ICU 1", "ICU 2") if unit in {"ICU 1", "ICU 2"} else (unit,)
    placeholders = ",".join("?" for _ in occupied_units)
    query = f"SELECT COUNT(*) FROM patients WHERE status='Admitted' AND disposition_unit IN ({placeholders})"
    parameters = list(occupied_units)
    if exclude_chart_number:
        query += " AND chart_number != ?"
        parameters.append(exclude_chart_number)
    return conn.execute(query, parameters).fetchone()[0]


def get_distinct_surgeons(conn):
    """Fetch list of distinct surgeon names."""
    return [dict(r) for r in conn.execute(
        "SELECT DISTINCT surgeon_name FROM patients WHERE surgeon_name IS NOT NULL AND surgeon_name != ''"
    ).fetchall()]


def get_audit_log_entries(conn, limit=100):
    """Fetch recent audit log entries."""
    return [dict(row) for row in conn.execute(
        "SELECT timestamp, username, action, chart_number, details FROM audit_log ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()]
