"""Input validation functions."""
from datetime import datetime
import re
from config import USERNAME_REGEX, PASSWORD_MIN_LENGTH


def parse_date(value, field_name, *, required=True):
    """Parse and validate a date string in YYYY-MM-DD format."""
    if not value:
        if required:
            raise ValueError(f"{field_name} is required.")
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field_name} must use YYYY-MM-DD format.") from exc


def valid_choice(value, choices, field_name):
    """Validate that a value is in the set of allowed choices."""
    if value not in choices:
        raise ValueError(f"Invalid {field_name}.")
    return value


def required_text(value, field_name, max_length=500):
    """Validate required text field with optional max length."""
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{field_name} is required.")
    if len(value) > max_length:
        raise ValueError(f"{field_name} is too long.")
    return value


def validate_username(username):
    """Validate username format."""
    if not re.match(USERNAME_REGEX, username):
        raise ValueError("Username must be 3–50 characters and use alphanumeric characters, dot, dash, or underscore.")
    return username


def validate_password(password, field_name="Password"):
    """Validate password length."""
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"{field_name} must be at least {PASSWORD_MIN_LENGTH} characters.")
    return password


def validate_passwords_match(password, confirmation, field_name="Password"):
    """Validate that two passwords match."""
    if password != confirmation:
        raise ValueError(f"{field_name} confirmation does not match.")
    return password
