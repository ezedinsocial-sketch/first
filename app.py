from flask import Flask, render_template, request, redirect, url_for, flash, send_file, session, g, abort
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
import hmac
import os
import sys
import re
import secrets
import sqlite3
import webbrowser
import pandas as pd
import io
from werkzeug.security import check_password_hash, generate_password_hash

# Handle PyInstaller's temporary path for static/template files
if getattr(sys, 'frozen', False):
    template_folder = os.path.join(sys._MEIPASS, 'templates')
    static_folder = os.path.join(sys._MEIPASS, 'static')
    app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)
else:
    app = Flask(__name__)

app.config.update(
    SECRET_KEY=os.environ.get("YANET_SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("YANET_COOKIE_SECURE", "false").lower() == "true",
)

DB_FILE = Path(os.environ.get("YANET_DB_FILE", Path(__file__).with_name("yanet_portal.db")))
# Added support for explicit sub-units and Day Care Surgery Units
VALID_UNITS = {"Ward", "ICU 1", "ICU 2", "ER", "Day Care Surgery Unit", "Waiting List"}
ACTIVE_UNITS = VALID_UNITS - {"Waiting List"}
VALID_DISCHARGE_OUTCOMES = {"Improved", "Same", "LAMA", "Down referral", "Up referral", "Deteriorated", "Death"}
VALID_BIOPSY_STATUSES = {"No", "Yes", "Pending", "Delayed"}
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,50}$")
PASSWORD_HASH_METHOD = "pbkdf2:sha256"

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def get_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]

@app.context_processor
def inject_template_globals():
    return {
        "csrf_token": get_csrf_token,
        "current_user": g.get("current_user"),
        "today_str": datetime.today().strftime("%Y-%m-%d"),
    }

@app.before_request
def load_user_and_check_csrf():
    g.current_user = None
    user_id = session.get("user_id")
    if user_id:
        conn = get_db_connection()
        user = conn.execute("SELECT id, username, role, is_active FROM users WHERE id=?", (user_id,)).fetchone()
        conn.close()
        if user and user["is_active"]:
            g.current_user = dict(user)
        else:
            session.clear()

    if request.method == "POST":
        submitted_token = request.form.get("csrf_token", "")
        if not hmac.compare_digest(submitted_token, session.get("csrf_token", "")):
            abort(400, "Invalid or missing CSRF token.")

@app.after_request
def apply_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if g.get("current_user"):
        response.headers["Cache-Control"] = "no-store"
    return response

def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not g.get("current_user"):
            flash("Please sign in to continue.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped_view

def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not g.get("current_user"):
            flash("Please sign in to continue.", "warning")
            return redirect(url_for("login"))
        if g.current_user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped_view

def parse_date(value, field_name, *, required=True):
    if not value:
        if required:
            raise ValueError(f"{field_name} is required.")
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field_name} must use YYYY-MM-DD format.") from exc

def valid_choice(value, choices, field_name):
    if value not in choices:
        raise ValueError(f"Invalid {field_name}.")
    return value

def required_text(value, field_name, max_length=500):
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{field_name} is required.")
    if len(value) > max_length:
        raise ValueError(f"{field_name} is too long.")
    return value

def patient_is_editable(patient, required_status=None):
    if not patient:
        raise ValueError("Patient record does not exist.")
    if patient["file_locked"]:
        raise ValueError("This patient file is locked. An administrator must unlock it before editing.")
    if required_status and patient["status"] != required_status:
        raise ValueError(f"This action is only available for {required_status.lower()} records.")

def audit_log(conn, action, chart_number=None, details=None):
    conn.execute(
        "INSERT INTO audit_log (timestamp, user_id, username, action, chart_number, details) VALUES (?, ?, ?, ?, ?, ?)",
        (datetime.utcnow().isoformat(timespec="seconds"), g.current_user["id"] if g.get("current_user") else None,
         g.current_user["username"] if g.get("current_user") else "system", action, chart_number, details),
    )

def ensure_unit_capacity(conn, unit, exclude_chart_number=None):
    # Normalized matching logic mapping sub-units onto primary resource counts
    capacity_key = {
        "Ward": "total_ward_beds",
        "ICU 1": "total_icu_beds",
        "ICU 2": "total_icu_beds",
        "ER": "total_er_beds",
        "Day Care Surgery Unit": "total_daycare_beds",
    }.get(unit)
    
    if not capacity_key:
        return
    capacity_row = conn.execute("SELECT value FROM global_settings WHERE key=?", (capacity_key,)).fetchone()
    try:
        capacity = int(capacity_row['value']) if capacity_row else 0
    except (TypeError, ValueError) as exc:
        raise ValueError("The configured bed capacity is invalid. Ask an administrator to correct it.") from exc
    if capacity < 0:
        raise ValueError("The configured bed capacity is invalid. Ask an administrator to correct it.")
    
    # Bundle ICU 1 and ICU 2 items dynamically to subtract from the same database bed pool
    occupied_units = ("ICU 1", "ICU 2") if unit in {"ICU 1", "ICU 2"} else (unit,)
    placeholders = ",".join("?" for _ in occupied_units)
    query = f"SELECT COUNT(*) FROM patients WHERE status='Admitted' AND disposition_unit IN ({placeholders})"
    parameters = list(occupied_units)
    if exclude_chart_number:
        query += " AND chart_number != ?"
        parameters.append(exclude_chart_number)
    occupied = conn.execute(query, parameters).fetchone()[0]
    if occupied >= capacity:
        raise ValueError(f"No {unit} bed is available. Place the patient on the waiting list or choose another unit.")

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'staff')),
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
            created_at TEXT NOT NULL,
            created_by INTEGER,
            last_login_at TEXT,
            FOREIGN KEY(created_by) REFERENCES users(id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            user_id INTEGER,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            chart_number TEXT,
            details TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    ''')
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_administrator_account ON users(role) WHERE role='admin'")
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS global_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    
    defaults = {
        'total_er_beds': '10',
        'total_icu_beds': '6',
        'total_ward_beds': '26',
        'total_daycare_beds': '10'
    }
    for key, val in defaults.items():
        cursor.execute("SELECT * FROM global_settings WHERE key=?", (key,))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO global_settings VALUES (?, ?)", (key, val))

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS room_transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chart_number TEXT NOT NULL,
            room_service TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT,
            days_spent INTEGER DEFAULT 0,
            FOREIGN KEY(chart_number) REFERENCES patients(chart_number)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS patients (
            chart_number TEXT PRIMARY KEY,
            full_name TEXT NOT NULL,
            phone TEXT,
            gender TEXT,
            admission_type TEXT,
            surgeon_name TEXT,
            diagnosis TEXT,
            date_admission TEXT,
            admission_officer TEXT,
            history_done TEXT DEFAULT 'No',
            pe_done TEXT DEFAULT 'No',
            ix_done TEXT DEFAULT 'No',
            pre_anesthesia_done TEXT DEFAULT 'No',
            disposition_unit TEXT,
            status TEXT DEFAULT 'Admitted',
            lost_to_followup_reason TEXT,
            date_discharge TEXT,
            procedure_done TEXT,
            discharge_status TEXT,
            op_note_complete TEXT DEFAULT 'No',
            biopsy_status TEXT DEFAULT 'No',
            biopsy_appointment_date TEXT,
            biopsy_final_result TEXT DEFAULT 'Pending Review',
            biopsy_result_received TEXT DEFAULT 'No',
            biopsy_delay_reason TEXT,
            appointment_date TEXT,
            discharge_officer TEXT,
            
            general_status_2w TEXT,
            deterioration_details TEXT,
            hospital_rating_2w INTEGER,
            ssi_surveillance_2w TEXT DEFAULT 'No',
            caller_name_2w TEXT,
            
            call_2w_state TEXT DEFAULT 'Pending', 
            call_2w_retry_date TEXT,
            call_2w_unreachable_reason TEXT,
            
            general_condition_4w TEXT,
            service_complaints TEXT,
            physician_opinion TEXT,
            nursing_opinion TEXT,
            price_worthiness TEXT,
            ssi_surveillance_4w TEXT DEFAULT 'No',
            caller_name_4w TEXT,
            
            call_4w_state TEXT DEFAULT 'Pending', 
            call_4w_retry_date TEXT,
            call_4w_unreachable_reason TEXT,
            
            waiting_list_leave_reason TEXT,
            file_locked INTEGER DEFAULT 0,
            high_risk_ssi INTEGER DEFAULT 0
        )
    ''')
    
    conn.commit()
    conn.close()

@app.route('/')
def home():
    return redirect(url_for('dashboard'))

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    conn = get_db_connection()
    has_users = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    if has_users:
        conn.close()
        return redirect(url_for('login'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        password_confirmation = request.form.get('password_confirmation', '')
        if not USERNAME_RE.fullmatch(username):
            flash("Username must be 3–50 characters and use alphanumeric characters, dot, dash, or underscore.", "danger")
        elif len(password) < 12:
            flash("Admin password must be at least 12 characters.", "danger")
        elif password != password_confirmation:
            flash("Password confirmation does not match.", "danger")
        else:
            try:
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
                return redirect(url_for('login'))
            except sqlite3.IntegrityError:
                conn.rollback()
                conn.close()
                flash("An administrator account has already been created. Please sign in.", "warning")
                return redirect(url_for('login'))
    conn.close()
    return render_template('index.html', view='setup')

@app.route('/login', methods=['GET', 'POST'])
def login():
    conn = get_db_connection()
    has_users = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    conn.close()
    if not has_users:
        return redirect(url_for('setup'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user and user['is_active'] and check_password_hash(user['password_hash'], password):
            conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (datetime.utcnow().isoformat(timespec="seconds"), user['id']))
            g.current_user = dict(user)
            audit_log(conn, "user_login", details=f"role={user['role']}")
            conn.commit()
            conn.close()
            session.clear()
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('dashboard'))
        conn.close()
        flash("Invalid username or password.", "danger")
    return render_template('index.html', view='login')

@app.route('/logout', methods=['POST'])
@login_required
def logout():
    conn = get_db_connection()
    audit_log(conn, "user_logout")
    conn.commit()
    conn.close()
    session.clear()
    return redirect(url_for('login'))

@app.route('/account/password', methods=['GET', 'POST'])
@login_required
def change_own_password():
    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirmation = request.form.get('password_confirmation', '')
        conn = get_db_connection()
        user = conn.execute("SELECT password_hash FROM users WHERE id=?", (g.current_user['id'],)).fetchone()
        if not user or not check_password_hash(user['password_hash'], current_password):
            flash('Your current password is incorrect.', 'danger')
        elif len(new_password) < 12:
            flash('New password must be at least 12 characters.', 'danger')
        elif new_password != confirmation:
            flash('Password confirmation does not match.', 'danger')
        else:
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_password, method=PASSWORD_HASH_METHOD), g.current_user['id']))
            audit_log(conn, 'own_password_changed')
            conn.commit()
            flash('Password changed successfully.', 'success')
        conn.close()
    return render_template('index.html', view='account_password')

@app.route('/admin/users')
@admin_required
def manage_users():
    conn = get_db_connection()
    users = [dict(row) for row in conn.execute(
        "SELECT id, username, role, is_active, created_at, last_login_at FROM users ORDER BY role DESC, username COLLATE NOCASE"
    ).fetchall()]
    audit_entries = [dict(row) for row in conn.execute(
        "SELECT timestamp, username, action, chart_number, details FROM audit_log ORDER BY id DESC LIMIT 100"
    ).fetchall()]
    conn.close()
    return render_template('index.html', view='users', users=users, audit_entries=audit_entries)

@app.route('/admin/users', methods=['POST'])
@admin_required
def create_user():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    password_confirmation = request.form.get('password_confirmation', '')
    if not USERNAME_RE.fullmatch(username):
        flash("Username must be 3–50 alphanumeric characters.", "danger")
    elif len(password) < 12:
        flash("Staff password must be at least 12 characters.", "danger")
    elif password != password_confirmation:
        flash("Password confirmation does not match.", "danger")
    else:
        conn = get_db_connection()
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at, created_by) VALUES (?, ?, 'staff', ?, ?)",
                (username, generate_password_hash(password, method=PASSWORD_HASH_METHOD), datetime.utcnow().isoformat(timespec="seconds"), g.current_user['id']),
            )
            audit_log(conn, "staff_account_created", details=f"username={username}")
            conn.commit()
            flash(f"Staff account '{username}' created.", "success")
        except sqlite3.IntegrityError:
            conn.rollback()
            flash("That username is already in use.", "danger")
        finally:
            conn.close()
    return redirect(url_for('manage_users'))

@app.route('/admin/users/<int:user_id>/password', methods=['POST'])
@admin_required
def reset_user_password(user_id):
    password = request.form.get('password', '')
    password_confirmation = request.form.get('password_confirmation', '')
    if len(password) < 12:
        flash("New password must be at least 12 characters.", "danger")
    elif password != password_confirmation:
        flash("Password confirmation does not match.", "danger")
    else:
        conn = get_db_connection()
        user = conn.execute("SELECT username, role FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or user['role'] != 'staff':
            flash("Only staff passwords can be reset here.", "danger")
        else:
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(password, method=PASSWORD_HASH_METHOD), user_id))
            audit_log(conn, "staff_password_reset", details=f"username={user['username']}")
            conn.commit()
            flash(f"Password reset for '{user['username']}'.", "success")
        conn.close()
    return redirect(url_for('manage_users'))

@app.route('/admin/users/<int:user_id>/toggle-active', methods=['POST'])
@admin_required
def toggle_user_active(user_id):
    conn = get_db_connection()
    user = conn.execute("SELECT username, role, is_active FROM users WHERE id=?", (user_id,)).fetchone()
    if not user or user['role'] != 'staff':
        flash("Only staff accounts can be modified here.", "danger")
    else:
        new_state = 0 if user['is_active'] else 1
        conn.execute("UPDATE users SET is_active=? WHERE id=?", (new_state, user_id))
        audit_log(conn, "staff_account_disabled" if not new_state else "staff_account_enabled", details=f"username={user['username']}")
        conn.commit()
        flash(f"Staff account '{user['username']}' {'enabled' if new_state else 'disabled'}.", "info")
    conn.close()
    return redirect(url_for('manage_users'))

@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db_connection()
    patients = [dict(r) for r in conn.execute("SELECT * FROM patients").fetchall()]
    
    gs = {}
    for row in conn.execute("SELECT * FROM global_settings").fetchall():
        try:
            gs[row['key']] = max(0, int(row['value']))
        except (TypeError, ValueError):
            gs[row['key']] = 0
            
    tot_icu = gs.get('total_icu_beds', 6)
    tot_ward = gs.get('total_ward_beds', 26)
    tot_er = gs.get('total_er_beds', 10)
    tot_daycare = gs.get('total_daycare_beds', 10)
    
    active_in_patients = [p for p in patients if p['status'] == 'Admitted']
    tracked_out_patients = [p for p in patients if p['status'] in ['Discharged', 'Lost to Follow-up', 'Left Waiting List']]
    waiting_list_patients = [p for p in patients if p['status'] == 'Waiting List']
    surgeons = [dict(r) for r in conn.execute("SELECT DISTINCT surgeon_name FROM patients WHERE surgeon_name IS NOT NULL AND surgeon_name != ''").fetchall()]
    
    # Unified tracking for sub-units ICU 1 and ICU 2
    icu_occupied = conn.execute("SELECT COUNT(*) FROM patients WHERE status='Admitted' AND disposition_unit IN ('ICU 1', 'ICU 2')").fetchone()[0]
    ward_occupied = conn.execute("SELECT COUNT(*) FROM patients WHERE status='Admitted' AND disposition_unit='Ward'").fetchone()[0]
    er_occupied = conn.execute("SELECT COUNT(*) FROM patients WHERE status='Admitted' AND disposition_unit='ER'").fetchone()[0]
    daycare_occupied = conn.execute("SELECT COUNT(*) FROM patients WHERE status='Admitted' AND disposition_unit='Day Care Surgery Unit'").fetchone()[0]
    
    today = datetime.today()
    today_str = today.strftime("%Y-%m-%d")
    
    alert_2w_list, retry_2w_list, missed_2w_list = [], [], []
    alert_4w_list, retry_4w_list, missed_4w_list = [], [], []
    biopsy_alert_list, biopsy_delayed_list = [], []

    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')

    total_ssi_cases = 0
    total_tracked_followups = 0
    total_discharged_patients = 0
    total_op_notes_completed = 0
    
    los_icu_accum = 0
    los_icu_count = 0
    los_ward_accum = 0
    los_ward_count = 0

    for p in patients:
        p['length_of_stay'] = 0
        p['biopsy_overdue'] = 'No'
        
        if p['date_admission']:
            try:
                adm_d = datetime.strptime(p['date_admission'], "%Y-%m-%d")
                end_d = datetime.strptime(p['date_discharge'], "%Y-%m-%d") if p['date_discharge'] else today
                p['length_of_stay'] = (end_d - adm_d).days
                
                if p['status'] in ['Discharged', 'Lost to Follow-up']:
                    if p['disposition_unit'] in ['ICU 1', 'ICU 2']:
                        los_icu_accum += p['length_of_stay']
                        los_icu_count += 1
                    else:
                        los_ward_accum += p['length_of_stay']
                        los_ward_count += 1
            except (TypeError, ValueError):
                pass

        if p['status'] == 'Discharged' and p['biopsy_status'] in ['Yes', 'Pending', 'Delayed'] and p['biopsy_result_received'] != 'Yes':
            if p['biopsy_appointment_date'] and today_str > p['biopsy_appointment_date']:
                p['biopsy_overdue'] = 'Yes'
            
            if p['biopsy_status'] == 'Delayed':
                biopsy_delayed_list.append(p)
            else:
                biopsy_alert_list.append(p)

        if p['status'] == 'Discharged' and p['discharge_status'] != 'Death':
            try:
                dis_d = datetime.strptime(p['date_discharge'], "%Y-%m-%d")
                days_since_discharge = (today - dis_d).days
                
                if p['call_2w_state'] == 'Pending' and days_since_discharge >= 14:
                    alert_2w_list.append(p)
                elif p['call_2w_state'] == 'Retry' and p['call_2w_retry_date']:
                    if today_str >= p['call_2w_retry_date']:
                        retry_2w_list.append(p)
                    else:
                        missed_2w_list.append(p)
                        
                if p['call_4w_state'] == 'Pending' and days_since_discharge >= 28:
                    alert_4w_list.append(p)
                elif p['call_4w_state'] == 'Retry' and p['call_4w_retry_date']:
                    if today_str >= p['call_4w_retry_date']:
                        retry_4w_list.append(p)
                    else:
                        missed_4w_list.append(p)
            except (TypeError, ValueError):
                pass

            in_range = True
            if start_date and p['date_discharge'] and p['date_discharge'] < start_date: in_range = False
            if end_date and p['date_discharge'] and p['date_discharge'] > end_date: in_range = False

            if in_range:
                total_discharged_patients += 1
                if p['op_note_complete'] == 'Yes':
                    total_op_notes_completed += 1
                if p['general_status_2w'] or p['general_condition_4w']:
                    total_tracked_followups += 1
                    if p['ssi_surveillance_2w'] in ['Clinical Signs', 'Diagnosis by a Clinician'] or p['ssi_surveillance_4w'] in ['Clinical Signs', 'Diagnosis by a Clinician']:
                        total_ssi_cases += 1

    ssi_rate = round((total_ssi_cases / total_tracked_followups) * 100, 1) if total_tracked_followups > 0 else 0.0
    avg_los_icu = round(los_icu_accum / los_icu_count, 1) if los_icu_count > 0 else 0.0
    avg_los_ward = round(los_ward_accum / los_ward_count, 1) if los_ward_count > 0 else 0.0
    op_note_completion_rate = round((total_op_notes_completed / total_discharged_patients) * 100, 1) if total_discharged_patients > 0 else 0.0

    totals = {
        'ward_max': tot_ward,
        'ward_avail': max(0, tot_ward - ward_occupied),
        'icu_max': tot_icu,
        'icu_avail': max(0, tot_icu - icu_occupied),
        'er_max': tot_er,
        'er_avail': max(0, tot_er - er_occupied),
        'daycare_max': tot_daycare,
        'daycare_avail': max(0, tot_daycare - daycare_occupied),
        'alert_2w_cnt': len(alert_2w_list) + len(retry_2w_list),
        'alert_4w_cnt': len(alert_4w_list) + len(retry_4w_list),
        'missed_2w_cnt': len(missed_2w_list),
        'missed_4w_cnt': len(missed_4w_list),
        'biopsy_alerts': len(biopsy_alert_list),
        'biopsy_delayed_cnt': len(biopsy_delayed_list),
        'ssi_rate': ssi_rate,
        'avg_los_icu': avg_los_icu,
        'avg_los_ward': avg_los_ward,
        'op_note_rate': op_note_completion_rate,
        'total_audited': total_tracked_followups
    }
    
    conn.close()
    return render_template(
        'index.html', view='dashboard', patients=patients, 
        active_in_patients=active_in_patients, tracked_out_patients=tracked_out_patients, 
        waiting_list_patients=waiting_list_patients, surgeons=surgeons, totals=totals, today_str=today_str,
        alert_2w_list=alert_2w_list, retry_2w_list=retry_2w_list, missed_2w_list=missed_2w_list,
        alert_4w_list=alert_4w_list, retry_4w_list=retry_4w_list, missed_4w_list=missed_4w_list,
        biopsy_alert_list=biopsy_alert_list, biopsy_delayed_list=biopsy_delayed_list,
        start_date=start_date, end_date=end_date
    )

@app.route('/patient/<string:chart_number>')
@login_required
def patient_profile(chart_number):
    conn = get_db_connection()
    patient = conn.execute("SELECT * FROM patients WHERE chart_number=?", (chart_number,)).fetchone()
    transfers = [dict(r) for r in conn.execute("SELECT * FROM room_transfers WHERE chart_number=? ORDER BY id ASC", (chart_number,)).fetchall()]
    conn.close()
    
    if not patient:
        flash("Patient profile does not exist.", "danger")
        return redirect(url_for('dashboard'))
    return render_template('index.html', view='profile', p=dict(patient), transfers=transfers, today_str=datetime.today().strftime("%Y-%m-%d"))

@app.route('/unlock_profile/<string:chart_number>', methods=['POST'])
@admin_required
def unlock_profile(chart_number):
    conn = get_db_connection()
    patient = conn.execute("SELECT chart_number FROM patients WHERE chart_number=?", (chart_number,)).fetchone()
    if not patient:
        flash("Patient profile does not exist.", "danger")
    else:
        conn.execute("UPDATE patients SET file_locked=0 WHERE chart_number=?", (chart_number,))
        audit_log(conn, "patient_file_unlocked", chart_number)
        conn.commit()
        flash("File unlocked for re-editing.", "warning")
    conn.close()
    return redirect(url_for('patient_profile', chart_number=chart_number))

@app.route('/change_room', methods=['POST'])
@login_required
def change_room():
    chart = request.form.get('chart_number', '').strip()
    new_unit = request.form.get('new_disposition_unit')
    conn = get_db_connection()
    try:
        valid_choice(new_unit, ACTIVE_UNITS, "destination unit")
        change_date = parse_date(request.form.get('change_date'), "Reallocation date")
        patient = conn.execute("SELECT * FROM patients WHERE chart_number=?", (chart,)).fetchone()
        patient_is_editable(patient, "Admitted")
        ensure_unit_capacity(conn, new_unit, exclude_chart_number=chart)
        admission_date = parse_date(patient['date_admission'], "Admission date")
        if change_date < admission_date:
            raise ValueError("Reallocation date cannot be before admission date.")
        active_trans = conn.execute("SELECT * FROM room_transfers WHERE chart_number=? AND end_date IS NULL", (chart,)).fetchone()
        if active_trans:
            start_date = parse_date(active_trans['start_date'], "Current room start date")
            if change_date < start_date:
                raise ValueError("Reallocation date cannot be before current room start date.")
            days = (change_date - start_date).days
            conn.execute("UPDATE room_transfers SET end_date=?, days_spent=? WHERE id=?", (change_date.isoformat(), days, active_trans['id']))
        conn.execute("INSERT INTO room_transfers (chart_number, room_service, start_date) VALUES (?, ?, ?)", (chart, new_unit, change_date.isoformat()))
        conn.execute("UPDATE patients SET disposition_unit=? WHERE chart_number=?", (new_unit, chart))
        audit_log(conn, "room_changed", chart, f"unit={new_unit}; date={change_date.isoformat()}")
        conn.commit()
        flash(f"Room allocation updated to: {new_unit}", "info")
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), "danger")
    conn.close()
    return redirect(url_for('patient_profile', chart_number=chart))

@app.route('/log_pipeline_call', methods=['POST'])
@login_required
def log_pipeline_call():
    chart = request.form.get('chart_number', '').strip()
    call_type = request.form.get('call_type')
    reachable = request.form.get('reachable')
    conn = get_db_connection()
    try:
        valid_choice(call_type, {'2w', '4w'}, 'follow-up type')
        valid_choice(reachable, {'Yes', 'No'}, 'reachability status')
        patient = conn.execute("SELECT * FROM patients WHERE chart_number=?", (chart,)).fetchone()
        patient_is_editable(patient, 'Discharged')
        if patient['discharge_status'] == 'Death':
            raise ValueError("Follow-up calls cannot be recorded for deceased patients.")

        retry_dt = (datetime.today() + timedelta(days=7)).strftime("%Y-%m-%d")
        if reachable == 'No':
            reason = valid_choice(request.form.get('unreachable_reason'), {
                'Phone not working/Switched off', 'Patient or attendant not available',
                'Language barrier Structural issue', 'Other'
            }, 'unreachable reason')
            other_text = request.form.get('unreachable_reason_other', '').strip()
            if reason == 'Other' and not other_text:
                raise ValueError("Please provide detailed reason.")
            final_reason = f"Other: {other_text}" if reason == 'Other' else reason

            if call_type == '2w':
                conn.execute('''
                    UPDATE patients SET call_2w_state='Retry', call_2w_retry_date=?,
                    call_2w_unreachable_reason=?, general_status_2w='Not Available',
                    ssi_surveillance_2w='Not Available' WHERE chart_number=?
                ''', (retry_dt, final_reason, chart))
            else:
                conn.execute('''
                    UPDATE patients SET call_4w_state='Retry', call_4w_retry_date=?,
                    call_4w_unreachable_reason=?, general_condition_4w='Not Available',
                    ssi_surveillance_4w='Not Available' WHERE chart_number=?
                ''', (retry_dt, final_reason, chart))
            audit_log(conn, f"{call_type}_followup_retry", chart, f"retry_date={retry_dt}; reason={final_reason}")
            flash(f"Patient unreachable. Follow-up retry due on {retry_dt}.", "warning")
        elif call_type == '2w':
            rating = request.form.get('hospital_rating_2w', '')
            try:
                rating = int(rating)
            except ValueError as exc:
                raise ValueError("Rating must be a number from 1 to 5.") from exc
            if rating not in range(1, 6):
                raise ValueError("Rating must be from 1 to 5.")
            ssi_status = valid_choice(request.form.get('ssi_surveillance_2w'), {
                'No Sign of Infection', 'Clinical Signs', 'Diagnosis by a Clinician'
            }, '2-week SSI status')
            conn.execute('''
                UPDATE patients SET general_status_2w=?, deterioration_details=?, hospital_rating_2w=?,
                ssi_surveillance_2w=?, caller_name_2w=?, call_2w_state='Cleared',
                call_2w_retry_date=NULL, call_2w_unreachable_reason=NULL WHERE chart_number=?
            ''', (request.form.get('general_status_2w', '').strip(), request.form.get('deterioration_details', '').strip(),
                  rating, ssi_status, request.form.get('caller_name_2w', '').strip(), chart))
            audit_log(conn, '2w_followup_completed', chart)
            flash("2-week verification record saved.", "success")
        else:
            ssi_status = valid_choice(request.form.get('ssi_surveillance_4w'), {
                'No Sign of Infection', 'Clinical Signs', 'Diagnosis by a Clinician'
            }, '4-week SSI status')
            conn.execute('''
                UPDATE patients SET general_condition_4w=?, service_complaints=?, physician_opinion=?,
                nursing_opinion=?, price_worthiness=?, ssi_surveillance_4w=?, caller_name_4w=?,
                call_4w_state='Cleared', call_4w_retry_date=NULL, call_4w_unreachable_reason=NULL,
                file_locked=1 WHERE chart_number=?
            ''', (request.form.get('general_condition_4w', '').strip(), request.form.get('service_complaints', '').strip(),
                  request.form.get('physician_opinion', '').strip(), request.form.get('nursing_opinion', '').strip(),
                  request.form.get('price_worthiness', '').strip(), ssi_status,
                  request.form.get('caller_name_4w', '').strip(), chart))
            audit_log(conn, '4w_followup_completed_and_locked', chart)
            flash("4-week quality audit finalized and record locked.", "dark")
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), 'danger')
    conn.close()
    return redirect(url_for('patient_profile', chart_number=chart))

@app.route('/admit', methods=['POST'])
@login_required
def admit():
    form = request.form
    chart_num = form.get('chart_number', '').strip()
    disp_unit = form.get('disposition_unit')
    conn = get_db_connection()
    try:
        chart_num = required_text(chart_num, 'Chart number', 100)
        valid_choice(disp_unit, VALID_UNITS, 'disposition unit')
        admission_date = parse_date(form.get('date_admission'), 'Admission date')
        admission_officer = required_text(form.get('admission_officer'), 'Admission officer', 100)
        existing = conn.execute("SELECT * FROM patients WHERE chart_number=?", (chart_num,)).fetchone()
        status = 'Waiting List' if disp_unit == 'Waiting List' else 'Admitted'

        if existing:
            patient_is_editable(existing)
            if existing['status'] != 'Waiting List' or status != 'Admitted':
                raise ValueError('This chart number already exists.')
            ensure_unit_capacity(conn, disp_unit, exclude_chart_number=chart_num)
            conn.execute('''
                UPDATE patients SET disposition_unit=?, status='Admitted', date_admission=?, admission_officer=?,
                waiting_list_leave_reason=NULL WHERE chart_number=?
            ''', (disp_unit, admission_date.isoformat(), admission_officer, chart_num))
            action = 'waiting_list_patient_admitted'
        else:
            full_name = required_text(form.get('full_name'), 'Full patient name', 200)
            if status == 'Admitted':
                ensure_unit_capacity(conn, disp_unit)
            conn.execute('''
                INSERT INTO patients (
                    chart_number, full_name, phone, gender, admission_type, surgeon_name, diagnosis, date_admission, admission_officer,
                    history_done, pe_done, ix_done, pre_anesthesia_done, disposition_unit, status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                chart_num, full_name, form.get('phone', '').strip(), form.get('gender'), form.get('admission_type'),
                form.get('surgeon_name', '').strip(), form.get('diagnosis', '').strip(), admission_date.isoformat(), admission_officer,
                form.get('history_done', 'No'), form.get('pe_done', 'No'), form.get('ix_done', 'No'), form.get('pre_anesthesia_done', 'No'),
                disp_unit, status
            ))
            action = 'patient_admitted' if status == 'Admitted' else 'patient_added_to_waiting_list'

        if status == 'Admitted':
            conn.execute("DELETE FROM room_transfers WHERE chart_number=?", (chart_num,))
            conn.execute("INSERT INTO room_transfers (chart_number, room_service, start_date) VALUES (?, ?, ?)",
                         (chart_num, disp_unit, admission_date.isoformat()))
        audit_log(conn, action, chart_num, f"unit={disp_unit}")
        conn.commit()
        flash(f"Admission registry updated for chart {chart_num}.", "success")
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), 'danger')
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/leave_waiting_list', methods=['POST'])
@login_required
def leave_waiting_list():
    chart = request.form.get('chart_number', '').strip()
    conn = get_db_connection()
    try:
        patient = conn.execute("SELECT * FROM patients WHERE chart_number=?", (chart,)).fetchone()
        patient_is_editable(patient, 'Waiting List')
        reason = valid_choice(request.form.get('waiting_list_leave_reason'), {
            'Went to other hospital', 'Will come back', 'Financial reason structural issue',
            'Dissatisfied with lack of bed space asset availability', 'Other'
        }, 'waiting-list leave reason')
        other_text = request.form.get('waiting_list_leave_reason_other', '').strip()
        if reason == 'Other' and not other_text:
            raise ValueError('Please provide detailed reason.')
        final_reason = f"Other: {other_text}" if reason == 'Other' else reason
        conn.execute("UPDATE patients SET status='Left Waiting List', waiting_list_leave_reason=? WHERE chart_number=?", (final_reason, chart))
        audit_log(conn, 'patient_left_waiting_list', chart, final_reason)
        conn.commit()
        flash("Patient removed from waiting list.", "info")
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), 'danger')
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/discharge', methods=['POST'])
@login_required
def discharge():
    form = request.form
    chart = form.get('discharge_chart_number', '').strip()
    conn = get_db_connection()
    try:
        status_type = valid_choice(form.get('discharge_status_selector', 'Discharged'), {'Discharged', 'Lost to Follow-up'}, 'discharge type')
        discharge_date = parse_date(form.get('date_discharge'), 'Discharge date')
        procedure_done = required_text(form.get('procedure_done'), 'Procedure', 500)
        discharge_officer = required_text(form.get('discharge_officer'), 'Discharge officer', 100)
        discharge_status = valid_choice(form.get('discharge_status'), VALID_DISCHARGE_OUTCOMES, 'discharge outcome')
        op_note_complete = valid_choice(form.get('op_note_complete', 'No'), {'Yes', 'No'}, 'operation-note status')
        biopsy_status = valid_choice(form.get('biopsy_status'), VALID_BIOPSY_STATUSES - {'Delayed'}, 'biopsy status')
        biopsy_date = parse_date(form.get('biopsy_appointment_date'), 'Biopsy date', required=False)
        appointment_date = parse_date(form.get('appointment_date'), 'Return date', required=False)
        patient = conn.execute("SELECT * FROM patients WHERE chart_number=?", (chart,)).fetchone()
        patient_is_editable(patient, 'Admitted')
        if discharge_date < parse_date(patient['date_admission'], 'Admission date'):
            raise ValueError('Discharge date cannot be before admission date.')
        reason_lost = required_text(form.get('lost_to_followup_reason'), 'Lost reason', 500) if status_type == 'Lost to Follow-up' else None
        high_risk = 1 if form.get('high_risk_ssi') == '1' else 0

        active_trans = conn.execute("SELECT * FROM room_transfers WHERE chart_number=? AND end_date IS NULL", (chart,)).fetchone()
        if active_trans:
            transfer_start = parse_date(active_trans['start_date'], 'Current room start date')
            if discharge_date < transfer_start:
                raise ValueError('Discharge date cannot be before current room start date.')
            conn.execute("UPDATE room_transfers SET end_date=?, days_spent=? WHERE id=?",
                         (discharge_date.isoformat(), (discharge_date - transfer_start).days, active_trans['id']))

        conn.execute('''
            UPDATE patients SET date_discharge=?, procedure_done=?, discharge_status=?, op_note_complete=?,
            biopsy_status=?, biopsy_appointment_date=?, appointment_date=?, discharge_officer=?,
            status=?, lost_to_followup_reason=?, call_2w_state='Pending', call_2w_retry_date=NULL,
            call_2w_unreachable_reason=NULL, call_4w_state='Pending', call_4w_retry_date=NULL,
            call_4w_unreachable_reason=NULL, high_risk_ssi=? WHERE chart_number=?
        ''', (discharge_date.isoformat(), procedure_done, discharge_status, op_note_complete, biopsy_status,
              biopsy_date.isoformat() if biopsy_date else None, appointment_date.isoformat() if appointment_date else None,
              discharge_officer, status_type, reason_lost, high_risk, chart))
        audit_log(conn, 'patient_discharged', chart, f"status={status_type}; outcome={discharge_status}")
        conn.commit()
        flash('Discharge record saved.', 'primary')
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), 'danger')
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/update_biopsy_result', methods=['POST'])
@login_required
def update_biopsy_result():
    chart = request.form.get('biopsy_chart_number', '').strip()
    conn = get_db_connection()
    try:
        patient = conn.execute("SELECT * FROM patients WHERE chart_number=?", (chart,)).fetchone()
        patient_is_editable(patient)
        status_selection = valid_choice(request.form.get('biopsy_status'), VALID_BIOPSY_STATUSES, 'biopsy status')
        result_text = required_text(request.form.get('biopsy_final_result'), 'Biopsy result', 5000)
        received = valid_choice(request.form.get('biopsy_result_received', 'No'), {'Yes', 'No'}, 'biopsy-received status')
        delay_reason = request.form.get('biopsy_delay_reason', '').strip()
        if status_selection == 'Delayed':
            if not delay_reason:
                raise ValueError('Please provide biopsy delay reason.')
            received = 'No'
        else:
            delay_reason = ''
        conn.execute('''
            UPDATE patients SET biopsy_status=?, biopsy_final_result=?, biopsy_result_received=?, biopsy_delay_reason=?
            WHERE chart_number=?
        ''', (status_selection, result_text, received, delay_reason, chart))
        audit_log(conn, 'biopsy_status_updated', chart, f"status={status_selection}; received={received}")
        conn.commit()
        flash('Biopsy status updated.', 'success')
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), 'danger')
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/update_hospital_capacities', methods=['POST'])
@admin_required
def update_hospital_capacities():
    conn = get_db_connection()
    try:
        capacities = {}
        for key in ['total_er_beds', 'total_icu_beds', 'total_ward_beds', 'total_daycare_beds']:
            raw_value = request.form.get(key, '')
            try:
                value = int(raw_value)
            except ValueError as exc:
                raise ValueError('Bed capacities must be whole numbers.') from exc
            if not 0 <= value <= 10000:
                raise ValueError('Bed capacities must be between 0 and 10,000.')
            capacities[key] = value
        occupied = {
            'total_er_beds': conn.execute("SELECT COUNT(*) FROM patients WHERE status='Admitted' AND disposition_unit='ER'").fetchone()[0],
            'total_icu_beds': conn.execute("SELECT COUNT(*) FROM patients WHERE status='Admitted' AND disposition_unit IN ('ICU 1', 'ICU 2')").fetchone()[0],
            'total_ward_beds': conn.execute("SELECT COUNT(*) FROM patients WHERE status='Admitted' AND disposition_unit='Ward'").fetchone()[0],
            'total_daycare_beds': conn.execute("SELECT COUNT(*) FROM patients WHERE status='Admitted' AND disposition_unit='Day Care Surgery Unit'").fetchone()[0],
        }
        for key, value in capacities.items():
            if value < occupied[key]:
                raise ValueError(f"Capacity cannot be lower than current occupancy ({occupied[key]}).")
            conn.execute("UPDATE global_settings SET value=? WHERE key=?", (str(value), key))
        audit_log(conn, 'bed_capacities_updated', details=str(capacities))
        conn.commit()
        flash('Bed capacities updated.', 'info')
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), 'danger')
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/export_excel')
@admin_required
def export_excel():
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    try:
        start = parse_date(start_date, 'Start date', required=False)
        end = parse_date(end_date, 'End date', required=False)
        if start and end and start > end:
            raise ValueError('Start date cannot be after end date.')
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('dashboard'))

    conn = get_db_connection()
    df_raw = pd.read_sql_query("SELECT * FROM patients", conn)
    audit_log(conn, 'patient_data_exported', details=f"start={start_date or 'all'}; end={end_date or 'all'}")
    conn.commit()
    conn.close()
    
    today_str = datetime.today().strftime("%Y-%m-%d")
    
    def check_overdue(row):
        if row['status'] == 'Discharged' and row['biopsy_status'] in ['Yes', 'Pending', 'Delayed'] and row['biopsy_result_received'] != 'Yes':
            if row['biopsy_appointment_date'] and today_str > row['biopsy_appointment_date']:
                return 'Yes'
        return 'No'
        
    df_raw['Is_Biopsy_Overdue'] = df_raw.apply(check_overdue, axis=1)
    
    if start_date:
        df_raw = df_raw[(df_raw['date_discharge'].isna()) | (df_raw['date_discharge'] >= start_date)]
    if end_date:
        df_raw = df_raw[(df_raw['date_discharge'].isna()) | (df_raw['date_discharge'] <= end_date)]

    for column in df_raw.select_dtypes(include='object').columns:
        df_raw[column] = df_raw[column].map(
            lambda value: f"'{value}" if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) else value
        )
        
    df_admitted = df_raw[df_raw['status'] == 'Admitted']
    df_discharged = df_raw[df_raw['status'] == 'Discharged']
    
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df_raw.to_excel(writer, index=False, sheet_name='Master_Surveillance_Data')
        df_admitted.to_excel(writer, index=False, sheet_name='Admitted_Patients')
        df_discharged.to_excel(writer, index=False, sheet_name='Discharged_Patients')
        
    output.seek(0)
    return send_file(output, as_attachment=True, download_name=f"Yanet_Surveillance_{datetime.today().strftime('%Y%m%d')}.xlsx")

init_db()

if __name__ == '__main__':
    # If compiled into an executable, automatically open the native browser window
    if getattr(sys, 'frozen', False):
        webbrowser.open("http://127.0.0.1:5000/")
        app.run(debug=False, port=5000)
    else:
        app.run(debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true', port=5000)