"""Application configuration and constants."""
import os
from pathlib import Path
import secrets

# Database
DB_FILE = Path(os.environ.get("YANET_DB_FILE", Path(__file__).with_name("yanet_portal.db")))

# Units and valid choices
VALID_UNITS = {"Ward", "ICU 1", "ICU 2", "ER", "Day Care Surgery Unit", "Waiting List"}
ACTIVE_UNITS = VALID_UNITS - {"Waiting List"}
VALID_DISCHARGE_OUTCOMES = {"Improved", "Same", "LAMA", "Down referral", "Up referral", "Deteriorated", "Death"}
VALID_BIOPSY_STATUSES = {"No", "Yes", "Pending", "Delayed"}

# User validation
USERNAME_REGEX = r"^[A-Za-z0-9_.-]{3,50}$"
PASSWORD_MIN_LENGTH = 12
PASSWORD_HASH_METHOD = "pbkdf2:sha256"

# Bed capacity defaults
BED_CAPACITY_DEFAULTS = {
    'total_er_beds': '10',
    'total_icu_beds': '6',
    'total_ward_beds': '26',
    'total_daycare_beds': '10'
}

# Capacity keys mapping
CAPACITY_KEY_MAP = {
    "Ward": "total_ward_beds",
    "ICU 1": "total_icu_beds",
    "ICU 2": "total_icu_beds",
    "ER": "total_er_beds",
    "Day Care Surgery Unit": "total_daycare_beds",
}

# Flask app config
FLASK_CONFIG = {
    'SECRET_KEY': os.environ.get("YANET_SECRET_KEY") or secrets.token_hex(32),
    'SESSION_COOKIE_HTTPONLY': True,
    'SESSION_COOKIE_SAMESITE': "Lax",
    'SESSION_COOKIE_SECURE': os.environ.get("YANET_COOKIE_SECURE", "false").lower() == "true",
}
