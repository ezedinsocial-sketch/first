"""Admin-only routes: user management, bed capacity configuration."""
from flask import Blueprint, render_template, request, redirect, url_for, flash
from werkzeug.security import generate_password_hash
from datetime import datetime
import sqlite3

from models import get_db_connection, get_all_users, get_user, get_audit_log_entries, count_occupied_beds, get_global_setting
from validators import validate_username, validate_password, validate_passwords_match
from utils import admin_required, audit_log
from config import PASSWORD_HASH_METHOD, CAPACITY_KEY_MAP

admin = Blueprint('admin', __name__, url_prefix='/admin')


@admin.route('/users')
@admin_required
def manage_users():
    """View and manage user accounts."""
    conn = get_db_connection()
    users = get_all_users(conn)
    audit_entries = get_audit_log_entries(conn, 100)
    conn.close()
    return render_template('index.html', view='users', users=users, audit_entries=audit_entries)


@admin.route('/users', methods=['POST'])
@admin_required
def create_user():
    """Create a new staff account."""
    try:
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        password_confirmation = request.form.get('password_confirmation', '')
        
        validate_username(username)
        validate_password(password, "Staff password")
        validate_passwords_match(password, password_confirmation)
        
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at, created_by) VALUES (?, ?, 'staff', ?, ?)",
            (username, generate_password_hash(password, method=PASSWORD_HASH_METHOD), datetime.utcnow().isoformat(timespec="seconds"), request.g.current_user['id']),
        )
        audit_log(conn, "staff_account_created", details=f"username={username}")
        conn.commit()
        flash(f"Staff account '{username}' created.", "success")
    except (ValueError, sqlite3.IntegrityError) as e:
        conn.rollback()
        flash(str(e) if isinstance(e, ValueError) else "That username is already in use.", "danger")
    finally:
        conn.close()
    
    return redirect(url_for('admin.manage_users'))


@admin.route('/users/<int:user_id>/password', methods=['POST'])
@admin_required
def reset_user_password(user_id):
    """Reset a staff member's password."""
    try:
        password = request.form.get('password', '')
        password_confirmation = request.form.get('password_confirmation', '')
        
        validate_password(password)
        validate_passwords_match(password, password_confirmation)
        
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
    except ValueError as e:
        flash(str(e), "danger")
    
    return redirect(url_for('admin.manage_users'))


@admin.route('/users/<int:user_id>/toggle-active', methods=['POST'])
@admin_required
def toggle_user_active(user_id):
    """Enable or disable a staff account."""
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
    return redirect(url_for('admin.manage_users'))


@admin.route('/capacities', methods=['POST'])
@admin_required
def update_hospital_capacities():
    """Update bed capacity configuration."""
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
            'total_er_beds': count_occupied_beds(conn, 'ER'),
            'total_icu_beds': count_occupied_beds(conn, 'ICU 1'),  # ICU 1 and 2 share pool
            'total_ward_beds': count_occupied_beds(conn, 'Ward'),
            'total_daycare_beds': count_occupied_beds(conn, 'Day Care Surgery Unit'),
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
    finally:
        conn.close()
    
    return redirect(url_for('patients.dashboard'))
