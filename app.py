"""Main Flask application entry point."""
import os
import sys
import webbrowser
from pathlib import Path

from flask import Flask

from config import FLASK_CONFIG, DB_FILE
from database import init_db
from blueprints.auth import auth
from blueprints.patients import patients
from blueprints.admin import admin

# Handle PyInstaller's temporary path for static/template files
if getattr(sys, 'frozen', False):
    template_folder = os.path.join(sys._MEIPASS, 'templates')
    static_folder = os.path.join(sys._MEIPASS, 'static')
    app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)
else:
    app = Flask(__name__)

# Apply configuration
app.config.update(FLASK_CONFIG)

# Register blueprints
app.register_blueprint(auth)
app.register_blueprint(patients)
app.register_blueprint(admin)

# Initialize database
init_db()


if __name__ == '__main__':
    # If compiled into an executable, automatically open the native browser window
    if getattr(sys, 'frozen', False):
        webbrowser.open("http://127.0.0.1:5000/")
        app.run(debug=False, port=5000)
    else:
        app.run(debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true', port=5000)
