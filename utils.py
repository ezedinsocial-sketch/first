"""Utility functions: decorators, audit logging, security."""
import hmac
from functools import wraps
from datetime import datetime
from flask import session, g, redirect, url_for, flash, abort
from models import get_db_connection


def login_required(view):
    """Decorator to require login."""
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not g.get("current_user"):
            flash("Please sign in to continue.", "warning")
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped_view


def admin_required(view):
    """Decorator to require admin role."""
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not g.get("current_user"):
            flash("Please sign in to continue.", "warning")
            return redirect(url_for("auth.login"))
        if g.current_user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped_view


def get_csrf_token():
    """Get or create a CSRF token in the session."""
    import secrets
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def audit_log(conn, action, chart_number=None, details=None):
    """Log an action to the audit log."""
    conn.execute(
        "INSERT INTO audit_log (timestamp, user_id, username, action, chart_number, details) VALUES (?, ?, ?, ?, ?, ?)",
        (
            datetime.utcnow().isoformat(timespec="seconds"),
            g.current_user["id"] if g.get("current_user") else None,
            g.current_user["username"] if g.get("current_user") else "system",
            action,
            chart_number,
            details
        ),
    )


def patient_is_editable(patient, required_status=None):
    """Check if a patient record is editable."""
    if not patient:
        raise ValueError("Patient record does not exist.")
    if patient["file_locked"]:
        raise ValueError("This patient file is locked. An administrator must unlock it before editing.")
    if required_status and patient["status"] != required_status:
        raise ValueError(f"This action is only available for {required_status.lower()} records.")
