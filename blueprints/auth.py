"""Authentication routes: login, logout, setup, password change."""
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, g
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import datetime
import sqlite3

from models import get_db_connection, get_user_by_username, get_user
from validators import validate_username, validate_password, validate_passwords_match
from utils import login_required, audit_log, get_csrf_token
from config import PASSWORD_HASH_METHOD

auth = Blueprint('auth', __name__)


@auth.before_app_request
def load_user_and_check_csrf():
    """Load current user and validate CSRF token."""
    g.current_user = None
    user_id = session.get("user_id")
    if user_id:
        conn = get_db_connection()
        user = get_user(conn, user_id)
        conn.close()
        if user and user["is_active"]:
            g.current_user = dict(user)
        else:
            session.clear()

    if request.method == "POST":
        import hmac
        submitted_token = request.form.get("csrf_token", "")
        if not hmac.compare_digest(submitted_token, session.get("csrf_token", "")):
            from werkzeug.exceptions import BadRequest
            raise BadRequest("Invalid or missing CSRF token.")


@auth.app_context_processor
def inject_template_globals():
    """Inject CSRF token and user data into templates."""
    return {
        "csrf_token": get_csrf_token,
        "current_user": g.get("current_user"),
        "today_str": datetime.today().strftime("%Y-%m-%d"),
    }


@auth.after_app_request
def apply_security_headers(response):
    """Apply security headers to response."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if g.get("current_user"):
        response.headers["Cache-Control"] = "no-store"
    return response


@auth.route('/setup', methods=['GET', 'POST'])
def setup():
    """Setup initial admin account."""
    conn = get_db_connection()
    has_users = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    if has_users:
        conn.close()
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        try:
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            password_confirmation = request.form.get('password_confirmation', '')
            
            validate_username(username)
            validate_password(password, "Admin password")
            validate_passwords_match(password, password_confirmation)
            
            new_admin = conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
                (username, generate_password_hash(password, method=PASSWORD_HASH_METHOD), datetime.utcnow().isoformat(timespec="seconds")),
            )
            conn.execute(
                "INSERT INTO audit_log (timestamp, user_id, username, action, details) VALUES (?, ?, ?, ?, ?)",
                (datetime.utcnow().isoformat(timespec="seconds"), new_admin.lastrowid, username, 'administrator_account_created', 'initial setup'),
            )
            conn.commit()
            conn.close()
            flash("Administrator account created. Please sign in.", "success")
            return redirect(url_for('auth.login'))
        except (ValueError, sqlite3.IntegrityError) as e:
            conn.rollback()
            conn.close()
            flash(str(e) if isinstance(e, ValueError) else "An administrator account has already been created.", "danger")
    
    conn.close()
    return render_template('index.html', view='setup')


@auth.route('/login', methods=['GET', 'POST'])
def login():
    """User login."""
    conn = get_db_connection()
    has_users = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    conn.close()
    
    if not has_users:
        return redirect(url_for('auth.setup'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        conn = get_db_connection()
        user = get_user_by_username(conn, username)
        
        if user and user['is_active'] and check_password_hash(user['password_hash'], password):
            conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (datetime.utcnow().isoformat(timespec="seconds"), user['id']))
            g.current_user = dict(user)
            audit_log(conn, "user_login", details=f"role={user['role']}")
            conn.commit()
            conn.close()
            session.clear()
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('patients.dashboard'))
        
        conn.close()
        flash("Invalid username or password.", "danger")
    
    return render_template('index.html', view='login')


@auth.route('/logout', methods=['POST'])
@login_required
def logout():
    """User logout."""
    conn = get_db_connection()
    audit_log(conn, "user_logout")
    conn.commit()
    conn.close()
    session.clear()
    return redirect(url_for('auth.login'))


@auth.route('/account/password', methods=['GET', 'POST'])
@login_required
def change_own_password():
    """Change logged-in user's password."""
    if request.method == 'POST':
        try:
            current_password = request.form.get('current_password', '')
            new_password = request.form.get('new_password', '')
            confirmation = request.form.get('password_confirmation', '')
            
            conn = get_db_connection()
            user = conn.execute("SELECT password_hash FROM users WHERE id=?", (g.current_user['id'],)).fetchone()
            
            if not user or not check_password_hash(user['password_hash'], current_password):
                flash('Your current password is incorrect.', 'danger')
            else:
                validate_password(new_password, 'New password')
                validate_passwords_match(new_password, confirmation)
                
                conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_password, method=PASSWORD_HASH_METHOD), g.current_user['id']))
                audit_log(conn, 'own_password_changed')
                conn.commit()
                flash('Password changed successfully.', 'success')
            
            conn.close()
        except ValueError as e:
            flash(str(e), 'danger')
    
    return render_template('index.html', view='account_password')
