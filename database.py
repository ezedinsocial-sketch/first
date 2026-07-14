"""Database initialization and schema."""
from models import get_db_connection
from config import DB_FILE, BED_CAPACITY_DEFAULTS


def init_db():
    """Initialize the database with required tables."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # Users table
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
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_administrator_account ON users(role) WHERE role='admin'")

    # Audit log table
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

    # Global settings table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS global_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    # Initialize default settings
    for key, val in BED_CAPACITY_DEFAULTS.items():
        cursor.execute("SELECT * FROM global_settings WHERE key=?", (key,))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO global_settings VALUES (?, ?)", (key, val))

    # Room transfers table
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

    # Patients table
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
