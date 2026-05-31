import os
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash, Response, send_from_directory
import re
import requests
from bs4 import BeautifulSoup
import datetime
from zoneinfo import ZoneInfo
import time
import logging
import logging_loki
from dotenv import load_dotenv
import json
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import csv
from io import StringIO
import secrets
import string
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate

load_dotenv()

app = Flask(__name__, static_folder='assets', static_url_path='/assets')
app.secret_key = os.environ.get('SECRET_KEY', 'default-secret-key')

# Grafana Loki Setup
logger = logging.getLogger("iamresponding-viewer")
logger.setLevel(logging.INFO)
loki_url = os.environ.get('GRAFANA_LOKI_URL')
loki_username = os.environ.get('GRAFANA_LOKI_USERNAME')
loki_password = os.environ.get('GRAFANA_LOKI_PASSWORD')

if loki_url and loki_username:
    handler = logging_loki.LokiHandler(
        url=loki_url,
        tags={"application": "iamresponding-viewer"},
        auth=(loki_username, loki_password),
        version="1",
    )
    logger.addHandler(handler)
else:
    logging.basicConfig(level=logging.INFO)
    logger.warning("Grafana Loki credentials not fully configured. Logging to console only.")

# User Registry Helper Functions
DATA_DIR = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
if DATA_DIR != os.path.dirname(os.path.abspath(__file__)):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception as e:
        logger.error(f"Failed to create DATA_DIR {DATA_DIR}: {e}")
USERS_FILE = os.path.join(DATA_DIR, 'users.json')

import base64

def encrypt_password(password, key):
    if not password:
        return ""
    key_bytes = key.encode('utf-8')
    pw_bytes = password.encode('utf-8')
    encrypted = bytearray(len(pw_bytes))
    for i in range(len(pw_bytes)):
        encrypted[i] = pw_bytes[i] ^ key_bytes[i % len(key_bytes)]
    return base64.b64encode(encrypted).decode('utf-8')

def decrypt_password(enc_password, key):
    if not enc_password:
        return ""
    try:
        key_bytes = key.encode('utf-8')
        enc_bytes = base64.b64decode(enc_password.encode('utf-8'))
        decrypted = bytearray(len(enc_bytes))
        for i in range(len(enc_bytes)):
            decrypted[i] = enc_bytes[i] ^ key_bytes[i % len(key_bytes)]
        return decrypted.decode('utf-8')
    except Exception:
        return ""

def load_users():
    if not os.path.exists(USERS_FILE):
        # Generate initial default users (Sidwell, Schur, Reese)
        default_users = {
            "sidwell_v31": {
                "last_name": "Sidwell",
                "victor_number": "V31",
                "password_hash": generate_password_hash("password123"),
                "is_default": True,
                "is_admin": True,
                "IAR_memberId": "",
                "IAR_Agency": "",
                "IAR_Username": "",
                "IAR_Password": "",
                "weekly_reminder_email": True
            },
            "schur_v22": {
                "last_name": "Schur",
                "victor_number": "V22",
                "password_hash": generate_password_hash("password123"),
                "is_default": True,
                "is_admin": True,
                "IAR_memberId": "",
                "IAR_Agency": "",
                "IAR_Username": "",
                "IAR_Password": "",
                "weekly_reminder_email": True
            },
            "reese_v2": {
                "last_name": "Reese",
                "victor_number": "V2",
                "password_hash": generate_password_hash("password123"),
                "is_default": True,
                "is_admin": True,
                "IAR_memberId": "",
                "IAR_Agency": "",
                "IAR_Username": "",
                "IAR_Password": "",
                "weekly_reminder_email": True
            }
        }
        try:
            with open(USERS_FILE, 'w') as f:
                json.dump(default_users, f, indent=4)
            logger.info("Generated default users.json file successfully.")
        except Exception as e:
            logger.error(f"Error creating default users file: {e}")
        return default_users
        
    try:
        with open(USERS_FILE, 'r') as f:
            users = json.load(f)
    except Exception as e:
        logger.error(f"Error loading users file: {e}")
        return {}

    # Migration check: Ensure Schur, Sidwell, and Reese are flagged as admins, and all users have IAR fields
    modified = False
    for key, profile in users.items():
        if key in ["sidwell_v31", "schur_v22", "reese_v2"]:
            if not profile.get('is_admin'):
                profile['is_admin'] = True
                modified = True
        
        for field in ["IAR_memberId", "IAR_Agency", "IAR_Username", "IAR_Password"]:
            if field not in profile:
                profile[field] = ""
                modified = True
                
        if 'weekly_reminder_email' not in profile:
            profile['weekly_reminder_email'] = True
            modified = True
                
    if modified:
        save_users(users)
        logger.info("Migrated user database in users.json to support IAR integration fields and preferences.")
        
    return users

def save_users(users):
    try:
        with open(USERS_FILE, 'w') as f:
            json.dump(users, f, indent=4)
        return True
    except Exception as e:
        logger.error(f"Error saving users file: {e}")
        return False

SETTINGS_FILE = os.path.join(DATA_DIR, 'settings.json')

def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        default_settings = {
            "allow_iar_status_change": True
        }
        try:
            with open(SETTINGS_FILE, 'w') as f:
                json.dump(default_settings, f, indent=4)
        except Exception as e:
            logger.error(f"Error creating settings.json: {e}")
        return default_settings
        
    try:
        with open(SETTINGS_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading settings.json: {e}")
        return {"allow_iar_status_change": True}

def save_settings(settings):
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings, f, indent=4)
        return True
    except Exception as e:
        logger.error(f"Error saving settings.json: {e}")
        return False
# Event Documents Configuration & Helpers
EVENT_DOCS_FILE = os.path.join(DATA_DIR, 'event_documents.json')
EVENT_DOCS_DIR = os.path.join(DATA_DIR, 'event_documents')

def load_event_documents():
    if not os.path.exists(EVENT_DOCS_FILE):
        return {}
    try:
        with open(EVENT_DOCS_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading event documents file: {e}")
        return {}

def save_event_documents(docs):
    try:
        os.makedirs(os.path.dirname(EVENT_DOCS_FILE), exist_ok=True)
        with open(EVENT_DOCS_FILE, 'w') as f:
            json.dump(docs, f, indent=4)
        return True
    except Exception as e:
        logger.error(f"Error saving event documents file: {e}")
        return False

def check_user_is_attending(user, attendee_list):
    if not user:
        return False
    
    user_member_id = str(user.get('IAR_memberId', '')).strip()
    if not user_member_id:
        return False
        
    for attendee in attendee_list:
        member = attendee.get('member', {})
        member_id = member.get('memberId') or member.get('memberID') or member.get('id')
        if member_id is None:
            continue
            
        member_id_str = str(member_id).strip()
        response = attendee.get('response')
        
        # Only match if response == 1 (Attending/Yes)
        if response == 1 and member_id_str == user_member_id:
            return True
    return False

# Login Audit Tracking Helpers
LOGINS_FILE = os.path.join(DATA_DIR, 'logins.json')

def log_login_event(last_name, victor_number, ip_address, status):
    chicago_tz = ZoneInfo("America/Chicago")
    timestamp = datetime.datetime.now(chicago_tz).strftime('%Y-%m-%d %H:%M:%S')

    form_url = os.environ.get('LOGIN_FORM_URL')
    entry_time = os.environ.get('LOGIN_FORM_ENTRY_TIME')
    entry_user = os.environ.get('LOGIN_FORM_ENTRY_USER')
    entry_vn = os.environ.get('LOGIN_FORM_ENTRY_VN')
    entry_ip = os.environ.get('LOGIN_FORM_ENTRY_IP')
    entry_status = os.environ.get('LOGIN_FORM_ENTRY_STATUS')

    if form_url and entry_time and entry_user and entry_vn and entry_ip and entry_status:
        try:
            data = {
                entry_time: timestamp,
                entry_user: last_name,
                entry_vn: victor_number,
                entry_ip: ip_address,
                entry_status: status
            }
            post_url = form_url
            if not post_url.endswith('/formResponse') and '/viewform' in post_url:
                post_url = post_url.replace('/viewform', '/formResponse')
            elif not post_url.endswith('/formResponse') and not post_url.endswith('/formResponse/'):
                post_url = post_url.rstrip('/') + '/formResponse'
                
            response = requests.post(post_url, data=data, timeout=3)
            if response.status_code == 200:
                logger.info(f"Log event written to Google Form successfully for {last_name} ({status})")
                return True
            else:
                logger.error(f"Failed to post log to Google Form: HTTP {response.status_code}. Falling back to local file.")
        except Exception as e:
            logger.error(f"Error posting log to Google Form: {e}. Falling back to local file.")

    # Local file logging fallback
    try:
        logins = []
        if os.path.exists(LOGINS_FILE):
            with open(LOGINS_FILE, 'r') as f:
                logins = json.load(f)
        
        logins.append({
            "timestamp": timestamp,
            "last_name": last_name or "Unknown",
            "victor_number": victor_number or "Unknown",
            "ip_address": ip_address,
            "status": status
        })
        
        if len(logins) > 200:
            logins = logins[-200:]
            
        with open(LOGINS_FILE, 'w') as f:
            json.dump(logins, f, indent=4)
        logger.info(f"Log event written to local logins.json for {last_name} ({status})")
        return True
    except Exception as e:
        logger.error(f"Failed to write log to local logins.json: {e}")
        return False

def load_logins():
    sheet_url = os.environ.get('LOGIN_SHEET_URL')
    
    if sheet_url:
        try:
            if 'export?format=csv' not in sheet_url and '/edit' in sheet_url:
                sheet_url = sheet_url.split('/edit')[0] + '/export?format=csv'
            elif 'export?format=csv' not in sheet_url:
                sheet_url = sheet_url.rstrip('/') + '/export?format=csv'

            response = requests.get(sheet_url, timeout=5)
            if response.status_code == 200:
                csv_content = response.text
                csv_file = StringIO(csv_content)
                reader = csv.reader(csv_file)
                headers = next(reader, None)
                
                logins = []
                if headers:
                    idx_timestamp = -1
                    idx_name = -1
                    idx_vn = -1
                    idx_ip = -1
                    idx_status = -1
                    
                    for i, h in enumerate(headers):
                        h_lower = h.lower()
                        if 'time' in h_lower:
                            idx_timestamp = i
                        elif 'name' in h_lower or 'user' in h_lower:
                            idx_name = i
                        elif 'victor' in h_lower or 'vn' in h_lower or 'number' in h_lower:
                            idx_vn = i
                        elif 'ip' in h_lower or 'address' in h_lower:
                            idx_ip = i
                        elif 'status' in h_lower:
                            idx_status = i
                            
                    for row in reader:
                        if len(row) < len(headers):
                            continue
                        
                        logins.append({
                            "timestamp": row[idx_timestamp] if idx_timestamp != -1 and idx_timestamp < len(row) else row[0],
                            "last_name": row[idx_name] if idx_name != -1 and idx_name < len(row) else (row[1] if len(row) > 1 else ""),
                            "victor_number": row[idx_vn] if idx_vn != -1 and idx_vn < len(row) else (row[2] if len(row) > 2 else ""),
                            "ip_address": row[idx_ip] if idx_ip != -1 and idx_ip < len(row) else (row[3] if len(row) > 3 else ""),
                            "status": row[idx_status] if idx_status != -1 and idx_status < len(row) else (row[4] if len(row) > 4 else "Success")
                        })
                logger.info(f"Loaded {len(logins)} logins from Google Sheet CSV successfully.")
                return logins
            else:
                logger.error(f"Failed to fetch login logs from Google Sheet: HTTP {response.status_code}. Falling back to local file.")
        except Exception as e:
            logger.error(f"Error fetching login logs from Google Sheet: {e}. Falling back to local file.")

    if os.path.exists(LOGINS_FILE):
        try:
            with open(LOGINS_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading local logins.json: {e}")
            
    return []

def send_password_reset_email(to_email, last_name, victor_number, temp_password):
    SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
    SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
    SMTP_USER = os.environ.get('SMTP_USER')
    SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD')
    EMAIL_SENDER = os.environ.get('EMAIL_SENDER', SMTP_USER)
    
    if not all([SMTP_SERVER, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_SENDER]):
        logger.error("SMTP credentials not fully configured in environment.")
        return False
        
    msg = MIMEMultipart('alternative')
    msg['Subject'] = "IamResponding Event Viewer - Password Reset"
    msg['From'] = EMAIL_SENDER
    msg['To'] = to_email
    msg['Date'] = formatdate(localtime=True)
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #0f172a; color: #f8fafc; margin: 0; padding: 20px; }}
        .wrapper {{ max-width: 600px; margin: 0 auto; background-color: #1e293b; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.5); border: 1px solid #334155; }}
        .header {{ background-color: #1e3a8a; color: #ffffff; padding: 30px; text-align: center; border-bottom: 1px solid #334155; }}
        .header h2 {{ margin: 0; font-size: 24px; font-weight: 600; letter-spacing: 0.5px; }}
        .content {{ padding: 30px; line-height: 1.6; color: #e2e8f0; }}
        .temp-password {{ background-color: #0f172a; color: #3b82f6; font-family: monospace; font-size: 22px; font-weight: bold; padding: 12px 20px; border-radius: 6px; display: inline-block; letter-spacing: 2px; margin: 20px 0; border: 1px solid #334155; }}
        .footer {{ background-color: #0f172a; padding: 20px; text-align: center; font-size: 12px; color: #94a3b8; border-top: 1px solid #334155; }}
    </style>
    </head>
    <body>
    <div class="wrapper">
        <div class="header">
            <h2>Password Reset Request</h2>
        </div>
        <div class="content">
            <p>Hello {last_name} ({victor_number}),</p>
            <p>A password reset request was made for your account on the IamResponding Event Viewer.</p>
            <p>Your temporary password is:</p>
            <div style="text-align: center;">
                <span class="temp-password">{temp_password}</span>
            </div>
            <p>Please log in using this temporary password. Upon logging in, you will be prompted to choose a new secure password.</p>
            <p>If you did not request this, you can ignore this email or contact the administrator.</p>
        </div>
        <div class="footer">
            This is an automated message from the IamResponding Event Viewer.
        </div>
    </div>
    </body>
    </html>
    """
    
    msg.attach(MIMEText(html_content, 'html'))
    
    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(EMAIL_SENDER, [to_email], msg.as_string())
        server.quit()
        logger.info(f"Password reset email sent successfully to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send password reset email: {e}")
        return False

@app.before_request
def check_user_active():
    public_endpoints = ['login', 'forgot_password', 'static', 'logout']
    if not request.endpoint or request.endpoint in public_endpoints:
        return
        
    user_key = session.get('user_key')
    if user_key:
        users = load_users()
        user = users.get(user_key)
        if not user or not user.get('is_active', True):
            session.clear()
            logger.warning(f"Session invalidated for deactivated or deleted user: {user_key}")
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Account deactivated or deleted'}), 401
            return redirect(url_for('login'))
        session['is_admin'] = user.get('is_admin', False)

@app.after_request
def log_page_views(response):
    if request.endpoint and request.endpoint != 'static':
        user_key = session.get('user_key', 'anonymous')
        last_name = session.get('last_name', '')
        victor_number = session.get('victor_number', '')
        
        user_display = f"{last_name} ({victor_number})" if (last_name and victor_number) else (last_name or 'anonymous')
        
        logger.info(
            f"Page View: {user_display} accessed {request.method} {request.path} - Status {response.status_code}",
            extra={
                "tags": {
                    "event_type": "page_view",
                    "user": user_key,
                    "last_name": last_name or "anonymous",
                    "victor_number": victor_number or "none",
                    "endpoint": request.endpoint,
                    "status_code": str(response.status_code)
                }
            }
        )
    return response

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        last_name = request.form.get('last_name', '').strip()
        victor_number = request.form.get('victor_number', '').strip()
        password = request.form.get('password', '')

        users = load_users()
        user_key = f"{last_name.lower()}_{victor_number.lower()}"
        
        user = users.get(user_key)
        if user and check_password_hash(user['password_hash'], password):
            if not user.get('is_active', True):
                error = 'Account is deactivated. Please contact an administrator.'
                logger.warning(f"Deactivated user {user_key} attempted login")
            else:
                session['user_key'] = user_key
                session['last_name'] = user['last_name']
                session['victor_number'] = user['victor_number']
                session['is_admin'] = user.get('is_admin', False)
                
                ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
                if ip_address and ',' in ip_address:
                    ip_address = ip_address.split(',')[0].strip()
                    
                if user.get('is_default', True):
                    session['must_change_password'] = True
                    log_login_event(user['last_name'], user['victor_number'], ip_address, "Success (Reset Required)")
                    logger.info(f"User {user_key} logged in with default password, forcing change", extra={"tags": {"event_type": "app_telemetry", "action": "login_must_change"}})
                    return redirect(url_for('change_password'))
                else:
                    session['authenticated'] = True
                    log_login_event(user['last_name'], user['victor_number'], ip_address, "Success")
                    logger.info(f"User {user_key} logged in successfully", extra={"tags": {"event_type": "app_telemetry", "action": "login_success"}})
                    return redirect(url_for('index'))
        else:
            ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
            if ip_address and ',' in ip_address:
                ip_address = ip_address.split(',')[0].strip()
            log_login_event(last_name, victor_number, ip_address, "Failed")
            
            error = 'Invalid Last Name, Victor Number, or Password'
            logger.warning(f"Failed login attempt for {last_name} ({victor_number})", extra={"tags": {"event_type": "app_telemetry", "action": "login_failed"}})
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/change-password', methods=['GET', 'POST'])
def change_password():
    user_key = session.get('user_key')
    if not user_key:
        return redirect(url_for('login'))
        
    error = None
    users = load_users()
    current_user = users.get(user_key, {})
    email = current_user.get('email', '')
    
    iar_agency = current_user.get('IAR_Agency', '')
    iar_username = current_user.get('IAR_Username', '')
    
    if request.method == 'POST':
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')
        email = request.form.get('email', '').strip()
        iar_agency_input = request.form.get('iar_agency', '').strip()
        iar_username_input = request.form.get('iar_username', '').strip()
        iar_password_input = request.form.get('iar_password', '')
        
        is_first_login = bool(session.get('must_change_password'))
        
        if not email:
            error = 'Email address is required'
        elif '@' not in email or '.' not in email:
            error = 'Invalid email address'
        elif not new_password:
            error = 'Password cannot be empty'
        elif new_password != confirm_password:
            error = 'Passwords do not match'
        elif len(new_password) < 6:
            error = 'Password must be at least 6 characters long'
        elif new_password == 'password123':
            error = 'Please choose a password different from the default'
        elif is_first_login and not iar_agency_input:
            error = 'IamResponding Agency is required on first login'
        elif is_first_login and not iar_username_input:
            error = 'IamResponding Username is required on first login'
        elif is_first_login and not iar_password_input:
            error = 'IamResponding Password is required on first login'
        else:
            if user_key in users:
                users[user_key]['password_hash'] = generate_password_hash(new_password)
                users[user_key]['is_default'] = False
                users[user_key]['email'] = email
                
                # Save IamResponding credentials
                if iar_agency_input:
                    users[user_key]['IAR_Agency'] = iar_agency_input
                if iar_username_input:
                    users[user_key]['IAR_Username'] = iar_username_input
                if iar_password_input:
                    users[user_key]['IAR_Password'] = encrypt_password(iar_password_input, app.secret_key)
                    
                if save_users(users):
                    session.pop('must_change_password', None)
                    session['authenticated'] = True
                    logger.info(f"Password changed and IAR credentials saved successfully for {user_key}", extra={"tags": {"event_type": "app_telemetry", "action": "password_changed"}})
                    return redirect(url_for('index'))
                else:
                    error = 'Failed to save password. Please try again.'
            else:
                error = 'User not found in registry.'
                
        # Keep inputs for re-rendering on failure
        iar_agency = iar_agency_input
        iar_username = iar_username_input
                
    return render_template('change_password.html', error=error, email=email, iar_agency=iar_agency, iar_username=iar_username)

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    error = None
    success = None
    if request.method == 'POST':
        last_name = request.form.get('last_name', '').strip()
        victor_number = request.form.get('victor_number', '').strip()
        
        users = load_users()
        user_key = f"{last_name.lower()}_{victor_number.lower()}"
        
        user = users.get(user_key)
        if user:
            to_email = user.get('email')
            if to_email:
                alphabet = string.ascii_letters + string.digits
                temp_password = ''.join(secrets.choice(alphabet) for _ in range(8))
                
                user['password_hash'] = generate_password_hash(temp_password)
                user['is_default'] = True
                
                if save_users(users):
                    if send_password_reset_email(to_email, user['last_name'], user['victor_number'], temp_password):
                        success = f"A temporary password has been successfully emailed to {to_email}."
                        logger.info(f"Password reset triggered successfully for {user_key}", extra={"tags": {"event_type": "app_telemetry", "action": "forgot_password_success"}})
                    else:
                        error = "Failed to send the email. Please try again or contact the administrator."
                else:
                    error = "Failed to save the new password. Please try again."
            else:
                error = "No email address is registered for this account. Please contact an administrator to reset your password."
                logger.warning(f"Forgot password attempt for {user_key} failed: no email address registered", extra={"tags": {"event_type": "app_telemetry", "action": "forgot_password_no_email"}})
        else:
            error = "Invalid Last Name or Victor Number. Account not found."
            logger.warning(f"Forgot password attempt failed: account not found for {last_name} ({victor_number})", extra={"tags": {"event_type": "app_telemetry", "action": "forgot_password_not_found"}})
            
    return render_template('forgot_password.html', error=error, success=success)

def validate_email(email_str):
    email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(email_regex, email_str))

def validate_us_phone(phone_str):
    digits = re.sub(r'\D', '', phone_str)
    if len(digits) == 10:
        return True
    if len(digits) == 11 and digits.startswith('1'):
        return True
    return False

def format_us_phone(phone_str):
    digits = re.sub(r'\D', '', phone_str)
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    if len(digits) == 10:
        return f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"
    return phone_str

@app.route('/profile', methods=['GET', 'POST'])
def profile():
    if session.get('must_change_password'):
        return redirect(url_for('change_password'))
    if not session.get('authenticated'):
        return redirect(url_for('login'))
        
    user_key = session.get('user_key')
    users = load_users()
    user = users.get(user_key)
    
    if not user:
        return redirect(url_for('logout'))
        
    error = None
    success = None
    
    if request.args.get('error') == 'iar_failed':
        error = 'Failed to authenticate or fetch events from IamResponding. Please verify and update your IAR Agency, Username, and Password below.'
        
    if request.method == 'POST':
        first_name = request.form.get('first_name', '').strip()
        email = request.form.get('email', '').strip()
        cell_phone = request.form.get('cell_phone', '').strip()
        iar_agency_input = request.form.get('iar_agency', '').strip()
        iar_username_input = request.form.get('iar_username', '').strip()
        iar_password_input = request.form.get('iar_password', '')
        weekly_reminder_email = request.form.get('weekly_reminder_email') == 'yes'
        
        if not first_name:
            error = 'First name is required'
        elif not email:
            error = 'Email address is required'
        elif not validate_email(email):
            error = 'Invalid email address format'
        elif not cell_phone:
            error = 'Cell phone is required'
        elif not validate_us_phone(cell_phone):
            error = 'Invalid US phone number (must contain 10 digits)'
        else:
            user['first_name'] = first_name
            user['email'] = email
            user['cell_phone'] = format_us_phone(cell_phone)
            user['IAR_Agency'] = iar_agency_input
            user['IAR_Username'] = iar_username_input
            user['weekly_reminder_email'] = weekly_reminder_email
            if iar_password_input:
                user['IAR_Password'] = encrypt_password(iar_password_input, app.secret_key)
            
            if save_users(users):
                logger.info(f"Profile updated successfully for {user_key}", extra={"tags": {"event_type": "app_telemetry", "action": "profile_updated"}})
                flash('Profile updated successfully!', 'success')
                return redirect(url_for('index'))
            else:
                error = 'Failed to save profile. Please try again.'
                
    return render_template(
        'profile.html',
        last_name=user.get('last_name'),
        victor_number=user.get('victor_number'),
        first_name=user.get('first_name', ''),
        email=user.get('email', ''),
        cell_phone=user.get('cell_phone', ''),
        iar_member_id=user.get('IAR_memberId', ''),
        iar_agency=user.get('IAR_Agency', ''),
        iar_username=user.get('IAR_Username', ''),
        weekly_reminder_email=user.get('weekly_reminder_email', True),
        error=error,
        success=success
    )

@app.route('/')
def index():
    if session.get('must_change_password'):
        return redirect(url_for('change_password'))
    if not session.get('authenticated'):
        return redirect(url_for('login'))
    last_name = session.get('last_name', '')
    is_admin = session.get('is_admin', False)
    is_sidwell = last_name.lower() == 'sidwell'
    
    latest_newsletter = None
    try:
        newsletters = load_newsletters()
        if newsletters:
            latest_newsletter = newsletters[0]
    except Exception as e:
        logger.error(f"Error loading latest newsletter for homepage: {e}")
        
    return render_template('landing.html', is_admin=is_admin, is_sidwell=is_sidwell, latest_newsletter=latest_newsletter)

@app.route('/logins')
def view_logins():
    if session.get('must_change_password'):
        return redirect(url_for('change_password'))
    if not session.get('authenticated'):
        return redirect(url_for('login'))
        
    last_name = session.get('last_name', '')
    if last_name.lower() != 'sidwell':
        logger.warning(f"Unauthorized audit access attempt by {last_name}")
        return redirect(url_for('index'))
        
    logins = load_logins()
    
    # Sort logins descending by timestamp
    def get_timestamp(log):
        return log.get('timestamp', '')
        
    logins.sort(key=get_timestamp, reverse=True)
    
    return render_template('logins.html', logins=logins)

@app.route('/iar')
def iar_dashboard():
    if session.get('must_change_password'):
        return redirect(url_for('change_password'))
    if not session.get('authenticated'):
        return redirect(url_for('login'))
    return render_template('index.html', is_admin=False)

@app.route('/iar-admin')
def iar_admin_dashboard():
    if session.get('must_change_password'):
        return redirect(url_for('change_password'))
    if not session.get('authenticated'):
        return redirect(url_for('login'))
    last_name = session.get('last_name', '')
    is_admin = session.get('is_admin', False)
    if not is_admin:
        logger.warning(f"Unauthorized IAR Admin access attempt by {last_name}")
        return redirect(url_for('index'))
    return render_template('iar_admin.html', is_admin=True)

@app.route('/gsar')
def gsar_dashboard():
    if session.get('must_change_password'):
        return redirect(url_for('change_password'))
    if not session.get('authenticated'):
        return redirect(url_for('login'))
        
    def parse_date(date_str):
        for fmt in ('%m/%d/%Y', '%m/%d/%y', '%Y-%m-%d'):
            try:
                return datetime.datetime.strptime(date_str.strip(), fmt).date()
            except ValueError:
                continue
        return None

    victor_number = session.get('victor_number', '')
    last_name = session.get('last_name', '')
    user_vn = victor_number.upper().lstrip('V')
    
    # Check admin status
    is_admin = session.get('is_admin', False)
    
    if is_admin:
        target_vn = request.args.get('view_vn', user_vn).upper().lstrip('V')
    else:
        target_vn = user_vn
        
    today = datetime.date.today()
    three_years_ago = today - datetime.timedelta(days=3*365)
    
    sheet_url = os.environ.get('GSAR_SHEET_URL')
    if not sheet_url:
        sheet_url = "https://docs.google.com/spreadsheets/d/1vuYmvyLLW-xW5uMAjzd1hBAQ9kD2aQjeY5d5llzPeN0/export?format=csv"
        
    records = []
    total_hours = 0.0
    recent_hours = 0.0
    error_msg = None
    
    unique_users = {}
    
    try:
        response = requests.get(sheet_url, timeout=10)
        if response.status_code == 200:
            csv_content = response.text
            csv_file = StringIO(csv_content)
            reader = csv.DictReader(csv_file)
            
            for row in reader:
                # Clean Victor Number from row
                row_vn = row.get('Victor Number', '').strip().upper().lstrip('V')
                row_name = row.get('Full Name', '').strip()
                
                # If admin, populate unique users list
                if is_admin and row_vn and row_name:
                    if row_vn not in unique_users:
                        unique_users[row_vn] = {
                            'name': row_name,
                            'display_vn': f"V{row_vn}"
                        }
                        
                # Match against the viewed user
                if row_vn == target_vn:
                    hours_str = row.get('Hours', '0').strip()
                    try:
                        hours_val = float(hours_str)
                    except ValueError:
                        hours_val = 0.0
                        
                    total_hours += hours_val
                    
                    date_str = row.get('Date', '')
                    parsed_date = parse_date(date_str)
                    if parsed_date and three_years_ago <= parsed_date <= today:
                        recent_hours += hours_val
                    
                    records.append({
                        'timestamp': row.get('Timestamp', ''),
                        'name': row_name,
                        'date': row.get('Date', ''),
                        'hours': hours_str,
                        'location': row.get('Location', ''),
                        'title': row.get('Event Title', ''),
                        'instructor': row.get('Instructor', ''),
                        'description': row.get('Description', '')
                    })
        else:
            error_msg = f"Failed to fetch training log from Google Sheet (HTTP {response.status_code})"
    except Exception as e:
        logger.error(f"Error fetching GSAR continuing education sheet: {e}")
        error_msg = f"Failed to retrieve training log: {str(e)}"
        
    # Helper to sort records
    def get_record_date(r):
        d = parse_date(r['date'])
        return d if d else datetime.date.min

    records.sort(key=get_record_date, reverse=True)
    
    location_hours = {}
    for r in records:
        loc = r['location'].strip() or 'Unknown Location'
        loc = loc.title()
        try:
            h = float(r['hours'])
        except ValueError:
            h = 0.0
        location_hours[loc] = location_hours.get(loc, 0.0) + h
        
    breakdown = [{'location': k, 'hours': round(v, 2)} for k, v in location_hours.items()]
    breakdown.sort(key=lambda x: x['hours'], reverse=True)
    
    # Sort dropdown users alphabetically by name
    dropdown_users = sorted(
        [{'vn': k, 'name': v['name'], 'display_vn': v['display_vn']} for k, v in unique_users.items()],
        key=lambda u: u['name'].lower()
    )
    
    # Resolve the display name of the viewed person
    viewed_name = last_name
    viewed_vn = victor_number
    if is_admin:
        if target_vn in unique_users:
            viewed_name = unique_users[target_vn]['name']
            viewed_vn = unique_users[target_vn]['display_vn']
        elif target_vn == user_vn:
            viewed_name = f"{last_name}"
            viewed_vn = f"{victor_number}"
        else:
            viewed_name = "Unknown Volunteer"
            viewed_vn = f"V{target_vn}"
            
    return render_template(
        'gsar.html',
        records=records,
        total_hours=round(total_hours, 2),
        recent_hours=round(recent_hours, 2),
        course_count=len(records),
        breakdown=breakdown,
        error=error_msg,
        user_name=f"{last_name}, {victor_number}",
        is_admin=is_admin,
        dropdown_users=dropdown_users,
        selected_vn=target_vn,
        viewed_user_label=f"{viewed_name} ({viewed_vn})"
    )

@app.route('/gsar-lead')
def gsar_team_lead():
    if session.get('must_change_password'):
        return redirect(url_for('change_password'))
    if not session.get('authenticated'):
        return redirect(url_for('login'))
        
    last_name = session.get('last_name', '')
    victor_number = session.get('victor_number', '')
    is_admin = session.get('is_admin', False)
    if not is_admin:
        logger.warning(f"Unauthorized access attempt to /gsar-lead by {last_name} ({victor_number})", extra={"tags": {"event_type": "app_telemetry", "action": "unauthorized_lead_access"}})
        return redirect(url_for('index'))
        
    sheet_url = os.environ.get('GSAR_SHEET_URL')
    if not sheet_url:
        sheet_url = "https://docs.google.com/spreadsheets/d/1vuYmvyLLW-xW5uMAjzd1hBAQ9kD2aQjeY5d5llzPeN0/export?format=csv"
        
    def parse_date(date_str):
        for fmt in ('%m/%d/%Y', '%m/%d/%y', '%Y-%m-%d'):
            try:
                return datetime.datetime.strptime(date_str.strip(), fmt).date()
            except ValueError:
                continue
        return None

    today = datetime.date.today()
    three_years_ago = today - datetime.timedelta(days=3*365)
    
    people = {}
    error_msg = None
    
    try:
        response = requests.get(sheet_url, timeout=10)
        if response.status_code == 200:
            csv_content = response.text
            csv_file = StringIO(csv_content)
            reader = csv.DictReader(csv_file)
            
            for row in reader:
                row_vn = row.get('Victor Number', '').strip().upper()
                if row_vn and not row_vn.startswith('V'):
                    row_vn = f"V{row_vn}"
                
                row_name = row.get('Full Name', '').strip()
                if not row_name:
                    continue
                
                row_name = row_name.title()
                
                hours_str = row.get('Hours', '0').strip()
                try:
                    hours_val = float(hours_str)
                except ValueError:
                    hours_val = 0.0
                
                date_str = row.get('Date', '')
                parsed_date = parse_date(date_str)
                is_recent = False
                if parsed_date and three_years_ago <= parsed_date <= today:
                    is_recent = True
                
                person_key = row_vn if row_vn else row_name
                
                if person_key not in people:
                    people[person_key] = {
                        'name': row_name,
                        'victor_number': row_vn or 'N/A',
                        'total_hours': 0.0,
                        'recent_hours': 0.0
                    }
                
                people[person_key]['total_hours'] += hours_val
                if is_recent:
                    people[person_key]['recent_hours'] += hours_val
            
            for p in people.values():
                p['total_hours'] = round(p['total_hours'], 2)
                p['recent_hours'] = round(p['recent_hours'], 2)
        else:
            error_msg = f"Failed to fetch training log from Google Sheet (HTTP {response.status_code})"
    except Exception as e:
        logger.error(f"Error fetching GSAR data for Team Lead: {e}")
        error_msg = f"Failed to retrieve training log: {str(e)}"
        
    sorted_people = sorted(people.values(), key=lambda x: x['name'].lower())
    
    return render_template(
        'gsar_lead.html',
        people=sorted_people,
        error=error_msg,
        user_name=f"{last_name}, {victor_number}"
    )

@app.route('/gsar-lead/export-summary')
def gsar_lead_export_summary():
    if session.get('must_change_password'):
        return redirect(url_for('change_password'))
    if not session.get('authenticated'):
        return redirect(url_for('login'))
        
    last_name = session.get('last_name', '')
    victor_number = session.get('victor_number', '')
    is_admin = session.get('is_admin', False)
    if not is_admin:
        logger.warning(f"Unauthorized access attempt to export summary by {last_name} ({victor_number})")
        return redirect(url_for('index'))
        
    sheet_url = os.environ.get('GSAR_SHEET_URL')
    if not sheet_url:
        sheet_url = "https://docs.google.com/spreadsheets/d/1vuYmvyLLW-xW5uMAjzd1hBAQ9kD2aQjeY5d5llzPeN0/export?format=csv"
        
    def parse_date(date_str):
        for fmt in ('%m/%d/%Y', '%m/%d/%y', '%Y-%m-%d'):
            try:
                return datetime.datetime.strptime(date_str.strip(), fmt).date()
            except ValueError:
                continue
        return None

    today = datetime.date.today()
    three_years_ago = today - datetime.timedelta(days=3*365)
    
    people = {}
    
    try:
        response = requests.get(sheet_url, timeout=10)
        if response.status_code == 200:
            csv_content = response.text
            csv_file = StringIO(csv_content)
            reader = csv.DictReader(csv_file)
            
            for row in reader:
                row_vn = row.get('Victor Number', '').strip().upper()
                if row_vn and not row_vn.startswith('V'):
                    row_vn = f"V{row_vn}"
                
                row_name = row.get('Full Name', '').strip()
                if not row_name:
                    continue
                
                row_name = row_name.title()
                
                hours_str = row.get('Hours', '0').strip()
                try:
                    hours_val = float(hours_str)
                except ValueError:
                    hours_val = 0.0
                
                date_str = row.get('Date', '')
                parsed_date = parse_date(date_str)
                is_recent = False
                if parsed_date and three_years_ago <= parsed_date <= today:
                    is_recent = True
                
                person_key = row_vn if row_vn else row_name
                
                if person_key not in people:
                    people[person_key] = {
                        'name': row_name,
                        'victor_number': row_vn or 'N/A',
                        'total_hours': 0.0,
                        'recent_hours': 0.0
                    }
                
                people[person_key]['total_hours'] += hours_val
                if is_recent:
                    people[person_key]['recent_hours'] += hours_val
            
            for p in people.values():
                p['total_hours'] = round(p['total_hours'], 2)
                p['recent_hours'] = round(p['recent_hours'], 2)
        else:
            return f"Failed to fetch data from Google Sheet: HTTP {response.status_code}", 500
    except Exception as e:
        logger.error(f"Error compiling export summary: {e}")
        return f"Error compiling export: {str(e)}", 500
        
    sorted_people = sorted(people.values(), key=lambda x: x['name'].lower())
    
    # Generate CSV in memory
    si = StringIO()
    writer = csv.writer(si)
    writer.writerow(['Volunteer Name', 'Victor Number', 'Hours (Last 3 Years)', 'Total Hours'])
    for p in sorted_people:
        writer.writerow([p['name'], p['victor_number'], p['recent_hours'], p['total_hours']])
        
    output = si.getvalue()
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=gsar_volunteer_hours_summary.csv"}
    )

@app.route('/gsar-lead/export-raw')
def gsar_lead_export_raw():
    if session.get('must_change_password'):
        return redirect(url_for('change_password'))
    if not session.get('authenticated'):
        return redirect(url_for('login'))
        
    last_name = session.get('last_name', '')
    victor_number = session.get('victor_number', '')
    is_admin = session.get('is_admin', False)
    if not is_admin:
        logger.warning(f"Unauthorized access attempt to export raw data by {last_name} ({victor_number})")
        return redirect(url_for('index'))
        
    sheet_url = os.environ.get('GSAR_SHEET_URL')
    if not sheet_url:
        sheet_url = "https://docs.google.com/spreadsheets/d/1vuYmvyLLW-xW5uMAjzd1hBAQ9kD2aQjeY5d5llzPeN0/export?format=csv"
        
    try:
        response = requests.get(sheet_url, timeout=10)
        if response.status_code == 200:
            csv_content = response.text
            return Response(
                csv_content,
                mimetype="text/csv",
                headers={"Content-disposition": "attachment; filename=gsar_raw_training_logs.csv"}
            )
        else:
            return f"Failed to fetch data from Google Sheet: HTTP {response.status_code}", 500
    except Exception as e:
        logger.error(f"Error fetching raw export: {e}")
        return f"Error fetching raw export: {str(e)}", 500

def login_to_iar(agency, username, password, urls_called=None):
    if urls_called is None:
        urls_called = []
        
    req_session = requests.Session()
    req_session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'
    })
    
    login_url = "https://auth.iamresponding.com/login/member"
    urls_called.append(f"GET {login_url}")
    get_response = req_session.get(login_url, timeout=10)
    get_response.raise_for_status()

    soup = BeautifulSoup(get_response.text, 'html.parser')
    token_input = soup.find('input', {'name': '__RequestVerificationToken'})
    if not token_input:
        raise Exception("Could not find RequestVerificationToken on login page.")
        
    token = token_input.get('value')
    login_data = {
        'Input.Agency': agency,
        'Input.Username': username,
        'Input.Password': password,
        'Input.button': 'login',
        '__RequestVerificationToken': token
    }
    
    urls_called.append(f"POST {login_url}")
    post_response = req_session.post(login_url, data=login_data, timeout=10)
    post_response.raise_for_status()
    return req_session

@app.route('/api/events', methods=['POST'])
def get_events():
    if session.get('must_change_password'):
        return jsonify({'error': 'Password change required.'}), 403
    if not session.get('authenticated'):
        logger.warning("Unauthorized access attempt to /api/events", extra={"tags": {"event_type": "app_telemetry"}})
        return jsonify({'error': 'Unauthorized. Please login again.'}), 401

    data = request.json or {}
    days = data.get('days', 10)

    users = load_users()
    user_key = session.get('user_key')
    user = users.get(user_key) if user_key else None
    
    agency = ""
    username = ""
    password = ""
    if user:
        agency = user.get('IAR_Agency', '')
        username = user.get('IAR_Username', '')
        enc_password = user.get('IAR_Password', '')
        password = decrypt_password(enc_password, app.secret_key)

    if not all([agency, username, password]):
        logger.error("Scrape failed: Missing credentials", extra={"tags": {"event_type": "app_telemetry", "action": "scrape_error"}})
        return jsonify({'error': 'Please update your IAR credentials in your profile.'}), 400

    logger.info(f"Starting scrape for agency: {agency}, days: {days}", extra={"tags": {"event_type": "app_telemetry", "action": "scrape_started"}})
    scrape_start_time = time.time()
    urls_called = []

    try:
        req_session = login_to_iar(agency, username, password, urls_called)

        # Step 3: Fetch the Event List
        event_list_url = f"https://coordinator.iamresponding.com/api/EventList?days={days}"
        urls_called.append(f"GET {event_list_url}")
        event_list_response = req_session.get(event_list_url, timeout=10)
        
        if event_list_response.status_code != 200:
            logger.error(f"Scrape failed: Event list status code {event_list_response.status_code}", extra={"tags": {"event_type": "app_telemetry", "action": "scrape_error"}})
            return jsonify({'error': 'Please update your IAR credentials in your profile.', 'status_code': event_list_response.status_code}), 401

        try:
            events = event_list_response.json()
        except Exception as e:
             logger.error("Scrape failed: Failed to parse event list JSON", extra={"tags": {"event_type": "app_telemetry", "action": "scrape_error"}})
             return jsonify({'error': 'Please update your IAR credentials in your profile.', 'details': str(e)}), 401

        # Step 4: Fetch details for each event
        detailed_events = []
        attending_ids = []
        
        # Load event documents registry and user details for permission checking
        event_docs = load_event_documents()
        users = load_users()
        current_user = users.get(session.get('user_key'))
        last_name = session.get('last_name', '')
        is_admin = session.get('is_admin', False)
        is_admin_view = data.get('is_admin_view', False) and is_admin
        
        for event in events:
            event_id = event.get('id')
            event_start_str = event.get('eventStart', '')
            
            try:
                dt_utc = datetime.datetime.strptime(event_start_str[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=datetime.timezone.utc)
                dt_central = dt_utc.astimezone(ZoneInfo("America/Chicago"))
                central_start_str = dt_central.strftime('%Y-%m-%dT%H:%M:%S')
            except Exception:
                central_start_str = event_start_str

            if not event_id:
                continue
                
            detail_url = f"https://coordinator.iamresponding.com/api/EventDetail?eventID={event_id}&recurrenceStartDate={central_start_str}"
            urls_called.append(f"GET {detail_url}")
            detail_response = req_session.get(detail_url, timeout=10)
            
            if detail_response.status_code == 200:
                try:
                    detail_data = detail_response.json()
                    
                    # Extract the required fields
                    subject = detail_data.get('subject', '')
                    event_start = detail_data.get('eventStart', '')
                    event_end = detail_data.get('eventEnd', '')
                    description = detail_data.get('description', '')
                    
                    # Filter attendees where response == 1 (attending)
                    all_attendees = detail_data.get('eventAttendees', [])
                    attending = [a for a in all_attendees if a.get('response') == 1]
                    
                    # Find logged in user's response status (0=not sure, 1=attending, 2=not attending)
                    user_member_id = str(current_user.get('IAR_memberId', '')).strip() if current_user else ""
                    user_response = 0  # default to "not sure" (0)
                    for a in all_attendees:
                        member = a.get('member', {})
                        m_id = str(member.get('memberId') or member.get('memberID') or member.get('id', '')).strip()
                        if user_member_id and m_id == user_member_id:
                            # Use response if it's not None, otherwise default to 0
                            user_response = a.get('response')
                            if user_response is None:
                                user_response = 0
                            break

                    # Check documents permission
                    is_attending = check_user_is_attending(current_user, all_attendees)
                    if is_attending:
                        attending_ids.append(str(event_id))
                    allowed_docs = []
                    if is_admin_view or is_attending:
                        allowed_docs = event_docs.get(str(event_id), [])
                    
                    detailed_events.append({
                        'id': event_id,
                        'subject': subject,
                        'eventStart': event_start,
                        'eventEnd': event_end,
                        'description': description,
                        'attendees': attending,
                        'documents': allowed_docs,
                        'user_response': user_response,
                        'recurrence_date': central_start_str
                    })
                    
                    # Business Data Logging
                    logger.info(
                        f"Fetched Event: {subject} ({len(attending)} attending)",
                        extra={"tags": {
                            "event_type": "business_data",
                            "action": "event_fetched",
                            "iam_event_id": str(event_id),
                            "iam_subject": subject,
                            "iam_attendees_count": str(len(attending))
                        }}
                    )
                except Exception as e:
                    # Skip if JSON parsing fails for a specific event
                    continue

        # Sort events by start date if it exists
        detailed_events.sort(key=lambda x: x.get('eventStart', ''))

        scrape_duration = time.time() - scrape_start_time
        logger.info(f"Scrape completed successfully in {scrape_duration:.2f}s. Fetched {len(detailed_events)} events.", extra={"tags": {"event_type": "app_telemetry", "action": "scrape_completed", "duration_seconds": str(round(scrape_duration, 2)), "events_count": str(len(detailed_events))}})

        session['attending_event_ids'] = attending_ids
        settings = load_settings()
        return jsonify({
            'events': detailed_events,
            'urls': urls_called,
            'allow_iar_status_change': settings.get('allow_iar_status_change', True)
        })

    except requests.exceptions.RequestException as e:
        logger.error(f"Scrape failed: Network request error - {str(e)}", extra={"tags": {"event_type": "app_telemetry", "action": "scrape_error"}})
        return jsonify({'error': 'Network request failed', 'details': str(e)}), 500

@app.route('/api/event/respond', methods=['POST'])
def respond_to_event():
    if session.get('must_change_password'):
        return jsonify({'error': 'Password change required.'}), 403
    if not session.get('authenticated'):
        return jsonify({'error': 'Unauthorized. Please login again.'}), 401
        
    settings = load_settings()
    if not settings.get('allow_iar_status_change', True):
        return jsonify({'error': 'Event response changes are currently disabled by the administrator.'}), 403

    data = request.json or {}
    event_id = data.get('eventId')
    recurrence_date = data.get('recurrenceDate')
    new_response = data.get('response')

    if event_id is None or recurrence_date is None or new_response is None:
        return jsonify({'error': 'Missing required fields: eventId, recurrenceDate, response'}), 400

    try:
        new_response = int(new_response)
        if new_response not in (0, 1, 2):
            return jsonify({'error': 'Invalid response value. Must be 0, 1, or 2.'}), 400
    except ValueError:
        return jsonify({'error': 'Response must be an integer.'}), 400

    users = load_users()
    user_key = session.get('user_key')
    user = users.get(user_key) if user_key else None
    
    if not user:
        return jsonify({'error': 'User session not found.'}), 404

    agency = user.get('IAR_Agency', '')
    username = user.get('IAR_Username', '')
    enc_password = user.get('IAR_Password', '')
    password = decrypt_password(enc_password, app.secret_key) if enc_password else ''

    if not all([agency, username, password]):
        return jsonify({'error': 'Please update your IAR credentials in your profile.'}), 400

    try:
        # Step 1: Login to IamResponding
        req_session = login_to_iar(agency, username, password)

        # Step 2: Fetch the specific EventDetail to get the current attendee list and find user's record
        detail_url = f"https://coordinator.iamresponding.com/api/EventDetail?eventID={event_id}&recurrenceStartDate={recurrence_date}"
        detail_response = req_session.get(detail_url, timeout=10)
        
        if detail_response.status_code != 200:
            return jsonify({'error': f'Failed to fetch event details: {detail_response.status_code}'}), 500

        detail_data = detail_response.json()
        all_attendees = detail_data.get('eventAttendees', [])
        
        user_member_id = str(user.get('IAR_memberId', '')).strip()
        
        # Step 3: Find user's attendee record
        target_attendee = None
        for a in all_attendees:
            member = a.get('member', {})
            m_id = str(member.get('memberId') or member.get('memberID') or member.get('id', '')).strip()
            if user_member_id and m_id == user_member_id:
                target_attendee = a
                break

        # Step 4: Construct the payload explicitly with all required top-level keys
        payload = {
            "eventId": 0,
            "response": new_response,
            "member": target_attendee.get('member') if target_attendee else {
                "memberId": int(user_member_id) if user_member_id.isdigit() else 0,
                "name": user.get('first_name', ''),
                "lastName": f"{user.get('last_name', '')} {user.get('victor_number', '')}".strip(),
                "memberEmail": user.get('email', ''),
                "secondaryEmail": "",
                "textMemberAddress": f"{user.get('cell_phone', '').replace('-', '')}@txt.att.net" if user.get('cell_phone') else "",
                "smsPhoneNumber": None,
                "isSmsAgreed": False
            },
            "recurrenceDate": recurrence_date,
            "id": event_id,
            "updateAll": False
        }

        # Step 5: Send PUT request to EventAttendee
        put_url = "https://coordinator.iamresponding.com/api/EventAttendee"
        put_response = req_session.put(put_url, json=payload, timeout=10)
        
        if put_response.status_code not in (200, 204):
            logger.error(f"Failed to update event response on IAR: {put_response.status_code} - {put_response.text}")
            return jsonify({'error': f'Failed to update status on IamResponding: {put_response.status_code}'}), 500

        # Step 6: Fetch updated details for this specific event to return to frontend
        detail_response = req_session.get(detail_url, timeout=10)
        if detail_response.status_code == 200:
            detail_data = detail_response.json()
            subject = detail_data.get('subject', '')
            event_start = detail_data.get('eventStart', '')
            event_end = detail_data.get('eventEnd', '')
            description = detail_data.get('description', '')
            
            all_attendees = detail_data.get('eventAttendees', [])
            attending = [a for a in all_attendees if a.get('response') == 1]
            
            # Find user's new response status
            user_response = 0
            for a in all_attendees:
                member = a.get('member', {})
                m_id = str(member.get('memberId') or member.get('memberID') or member.get('id', '')).strip()
                if user_member_id and m_id == user_member_id:
                    user_response = a.get('response')
                    if user_response is None:
                        user_response = 0
                    break
            
            # Check documents permission
            event_docs = load_event_documents()
            is_attending = check_user_is_attending(user, all_attendees)
            is_admin = session.get('is_admin', False)
            allowed_docs = []
            if is_admin or is_attending:
                allowed_docs = event_docs.get(str(event_id), [])
                
            # If the user is attending, ensure this event ID is in their session list
            attending_event_ids = session.get('attending_event_ids', [])
            if is_attending:
                if str(event_id) not in attending_event_ids:
                    attending_event_ids.append(str(event_id))
            else:
                if str(event_id) in attending_event_ids:
                    try:
                        attending_event_ids.remove(str(event_id))
                    except ValueError:
                        pass
            session['attending_event_ids'] = attending_event_ids

            updated_event = {
                'id': event_id,
                'subject': subject,
                'eventStart': event_start,
                'eventEnd': event_end,
                'description': description,
                'attendees': attending,
                'documents': allowed_docs,
                'user_response': user_response,
                'recurrence_date': recurrence_date
            }
            logger.info(f"Successfully updated response to {new_response} for user {user_key} on event {event_id}", extra={"tags": {"event_type": "app_telemetry", "action": "respond_success"}})
            return jsonify({'success': True, 'event': updated_event})
        else:
            logger.info(f"Successfully updated response to {new_response} for user {user_key} on event {event_id} (could not fetch detail)", extra={"tags": {"event_type": "app_telemetry", "action": "respond_success"}})
            return jsonify({'success': True, 'message': 'Status updated, but failed to fetch event details.'})

    except Exception as e:
        logger.error(f"Error responding to event: {str(e)}")
        return jsonify({'error': str(e)}), 500

def load_newsletters():
    sheet_url = os.environ.get('NEWSLETTER_SHEET_URL')
    if not sheet_url:
        logger.error("NEWSLETTER_SHEET_URL not set in environment.")
        return []
        
    if 'export?format=csv' not in sheet_url:
        if '/edit' in sheet_url:
            sheet_url = sheet_url.split('/edit')[0] + '/export?format=csv'
        else:
            sheet_url = sheet_url.rstrip('/') + '/export?format=csv'
            
    try:
        response = requests.get(sheet_url, timeout=10)
        if response.status_code == 200:
            csv_content = response.text
            csv_file = StringIO(csv_content)
            reader = csv.DictReader(csv_file)
            
            newsletters = []
            onedrive_folder_url = "https://1drv.ms/f/c/8a32438cf94d8484/IgCEhE35jEMyIICK1NYCAAAAAXlioXW5oIRl7PISWmk2bc8?e=PdeCdi"
            
            for row in reader:
                row_cleaned = {k.strip().lower(): v.strip() for k, v in row.items() if k}
                date_val = row_cleaned.get('date', '')
                url_val = row_cleaned.get('url', '')
                
                title_val = row_cleaned.get('title', '')
                if not title_val:
                    name_val = row_cleaned.get('name', '')
                    if name_val:
                        if name_val.lower().endswith('.pdf'):
                            title_val = os.path.splitext(name_val)[0].replace('_', ' ').replace('-', ' ').strip()
                        else:
                            title_val = name_val
                    else:
                        title_val = f"{date_val} Newsletter"
                
                if date_val:
                    if url_val.startswith('http://') or url_val.startswith('https://'):
                        link = url_val
                    elif url_val:
                        local_path = os.path.join(app.static_folder, 'newsletters', url_val)
                        if os.path.exists(local_path):
                            link = f"/assets/newsletters/{url_val}"
                        else:
                            link = onedrive_folder_url
                    else:
                        link = onedrive_folder_url
                        
                    newsletters.append({
                        'date': date_val,
                        'title': title_val,
                        'url': link
                    })
            
            def parse_newsletter_date(n):
                d_str = n['date'].strip()
                for fmt in ('%m/%d/%Y', '%m/%d/%y', '%Y-%m-%d', '%Y-%m', '%m-%Y', '%B %Y', '%b %Y'):
                    try:
                        return datetime.datetime.strptime(d_str, fmt).date()
                    except ValueError:
                        continue
                return datetime.date.min
                
            newsletters.sort(key=parse_newsletter_date, reverse=True)
            logger.info(f"Successfully loaded {len(newsletters)} newsletters from Google Sheet.")
            return newsletters
        else:
            logger.error(f"Failed to fetch newsletters from Google Sheet: HTTP {response.status_code}")
    except Exception as e:
        logger.error(f"Error fetching newsletters from Google Sheet: {e}")
        
    return []

@app.route('/newsletters')
def newsletters_dashboard():
    if session.get('must_change_password'):
        return redirect(url_for('change_password'))
    if not session.get('authenticated'):
        return redirect(url_for('login'))
        
    last_name = session.get('last_name', '')
    victor_number = session.get('victor_number', '')
    
    newsletters_list = load_newsletters()
    onedrive_folder_url = "https://1drv.ms/f/c/8a32438cf94d8484/IgCEhE35jEMyIICK1NYCAAAAAXlioXW5oIRl7PISWmk2bc8?e=PdeCdi"
    
    return render_template(
        'newsletters.html',
        newsletters=newsletters_list,
        onedrive_url=onedrive_folder_url,
        user_name=f"{last_name}, {victor_number}"
    )

@app.route('/admin/upload-document', methods=['POST'])
def upload_document():
    if not session.get('authenticated'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    last_name = session.get('last_name', '')
    is_admin = session.get('is_admin', False)
    if not is_admin:
        return jsonify({'error': 'Admin access required'}), 403
        
    event_id = request.form.get('event_id')
    if not event_id:
        return jsonify({'error': 'Missing event_id'}), 400
        
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
        
    if file:
        filename = secure_filename(file.filename)
        
        # Save file to DATA_DIR/event_documents/<event_id>/
        dest_dir = os.path.join(EVENT_DOCS_DIR, str(event_id))
        os.makedirs(dest_dir, exist_ok=True)
        file.save(os.path.join(dest_dir, filename))
        
        # Calculate human-readable file size
        file_size_bytes = os.path.getsize(os.path.join(dest_dir, filename))
        if file_size_bytes < 1024:
            file_size = f"{file_size_bytes} B"
        elif file_size_bytes < 1024 * 1024:
            file_size = f"{file_size_bytes / 1024:.1f} KB"
        else:
            file_size = f"{file_size_bytes / (1024 * 1024):.1f} MB"
            
        # Update registry
        event_docs = load_event_documents()
        event_id_str = str(event_id)
        if event_id_str not in event_docs:
            event_docs[event_id_str] = []
            
        # Remove any existing record of the same filename in this event
        event_docs[event_id_str] = [d for d in event_docs[event_id_str] if d['filename'] != filename]
        
        chicago_tz = ZoneInfo("America/Chicago")
        uploaded_at = datetime.datetime.now(chicago_tz).strftime('%Y-%m-%d %H:%M:%S')
        
        event_docs[event_id_str].append({
            'filename': filename,
            'uploaded_by': last_name,
            'uploaded_at': uploaded_at,
            'file_size': file_size
        })
        
        save_event_documents(event_docs)
        
        logger.info(f"Admin {last_name} uploaded file {filename} for event {event_id}", extra={"tags": {"event_type": "app_telemetry", "action": "document_uploaded"}})
        return jsonify({'success': True, 'filename': filename, 'file_size': file_size})
        
    return jsonify({'error': 'File upload failed'}), 500

@app.route('/admin/delete-document', methods=['POST'])
def delete_document():
    if not session.get('authenticated'):
        return jsonify({'error': 'Unauthorized'}), 401
        
    last_name = session.get('last_name', '')
    is_admin = session.get('is_admin', False)
    if not is_admin:
        return jsonify({'error': 'Admin access required'}), 403
        
    data = request.json or {}
    event_id = data.get('event_id')
    filename = data.get('filename')
    
    if not event_id or not filename:
        return jsonify({'error': 'Missing parameters'}), 400
        
    event_id_str = str(event_id)
    filename = secure_filename(filename)
    
    # Remove from disk
    file_path = os.path.join(EVENT_DOCS_DIR, event_id_str, filename)
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
            # Remove directory if empty
            dest_dir = os.path.dirname(file_path)
            if os.path.exists(dest_dir) and not os.listdir(dest_dir):
                os.rmdir(dest_dir)
        except Exception as e:
            logger.error(f"Error removing file {file_path}: {e}")
            return jsonify({'error': 'Failed to delete file from disk'}), 500
            
    # Remove from registry
    event_docs = load_event_documents()
    if event_id_str in event_docs:
        event_docs[event_id_str] = [d for d in event_docs[event_id_str] if d['filename'] != filename]
        if not event_docs[event_id_str]:
            del event_docs[event_id_str]
        save_event_documents(event_docs)
        
    logger.info(f"Admin {last_name} deleted file {filename} for event {event_id}", extra={"tags": {"event_type": "app_telemetry", "action": "document_deleted"}})
    return jsonify({'success': True})

@app.route('/api/event-document/<event_id>/<filename>')
def get_event_document(event_id, filename):
    if not session.get('authenticated'):
        return "Unauthorized. Please log in first.", 401
        
    last_name = session.get('last_name', '')
    is_admin = session.get('is_admin', False)
    
    event_id_str = str(event_id)
    filename = secure_filename(filename)
    
    event_docs = load_event_documents()
    docs = event_docs.get(event_id_str, [])
    if not any(d['filename'] == filename for d in docs):
        return "Document not found in registry", 404
        
    if is_admin or event_id_str in session.get('attending_event_ids', []):
        file_path = os.path.join(EVENT_DOCS_DIR, event_id_str)
        if not os.path.exists(os.path.join(file_path, filename)):
            return "Document not found on disk", 404
        return send_from_directory(file_path, filename)
        
    return "Access Denied. You must be signed up for this event to view its documents.", 403

def is_session_admin():
    return session.get('is_admin', False)

@app.route('/admin/users')
def user_admin_dashboard():
    if session.get('must_change_password'):
        return redirect(url_for('change_password'))
    if not session.get('authenticated'):
        return redirect(url_for('login'))
    if not is_session_admin():
        logger.warning(f"Unauthorized User Admin access attempt by {session.get('last_name')}")
        return redirect(url_for('index'))
    return render_template('user_admin.html')

@app.route('/api/admin/users', methods=['GET'])
def get_admin_users():
    if not session.get('authenticated') or not is_session_admin():
        return jsonify({'error': 'Unauthorized'}), 401
    users = load_users()
    user_list = []
    for key, profile in users.items():
        profile_copy = profile.copy()
        profile_copy['user_key'] = key
        profile_copy.pop('password_hash', None)
        profile_copy.pop('IAR_Password', None)
        profile_copy['is_active'] = profile_copy.get('is_active', True)
        profile_copy['is_admin'] = profile_copy.get('is_admin', False)
        profile_copy['weekly_reminder_email'] = profile_copy.get('weekly_reminder_email', True)
        user_list.append(profile_copy)
    return jsonify(user_list)

@app.route('/api/admin/users', methods=['POST'])
def create_admin_user():
    if not session.get('authenticated') or not is_session_admin():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    first_name = data.get('first_name', '').strip()
    last_name = data.get('last_name', '').strip()
    victor_number = data.get('victor_number', '').strip()
    email = data.get('email', '').strip()
    cell_phone = data.get('cell_phone', '').strip()
    iar_first_name = data.get('iar_first_name', '').strip()
    iar_last_name = data.get('iar_last_name', '').strip()
    iar_member_id = data.get('IAR_memberId', '').strip()
    iar_agency = data.get('IAR_Agency', '').strip()
    iar_username = data.get('IAR_Username', '').strip()
    is_admin = bool(data.get('is_admin', False))
    weekly_reminder_email = bool(data.get('weekly_reminder_email', True))
    
    if not last_name or not victor_number:
        return jsonify({'error': 'Last Name and Victor Number are required.'}), 400
        
    user_key = f"{last_name.lower()}_{victor_number.lower()}"
    users = load_users()
    if user_key in users:
        return jsonify({'error': 'A user with this Last Name and Victor Number already exists.'}), 400
        
    users[user_key] = {
        'first_name': first_name,
        'last_name': last_name,
        'victor_number': victor_number,
        'email': email,
        'cell_phone': cell_phone,
        'iar_first_name': iar_first_name or None,
        'iar_last_name': iar_last_name or None,
        'IAR_memberId': iar_member_id,
        'IAR_Agency': iar_agency,
        'IAR_Username': iar_username,
        'IAR_Password': '',
        'is_active': True,
        'is_default': True,
        'is_admin': is_admin,
        'weekly_reminder_email': weekly_reminder_email,
        'password_hash': generate_password_hash("password123")
    }
    save_users(users)
    logger.info(f"Admin {session.get('last_name')} created user {user_key}")
    return jsonify({'success': True, 'user_key': user_key})

@app.route('/api/admin/users', methods=['PUT'])
def update_admin_user():
    if not session.get('authenticated') or not is_session_admin():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    old_key = data.get('user_key')
    if not old_key:
        return jsonify({'error': 'Missing user_key'}), 400
        
    users = load_users()
    if old_key not in users:
        return jsonify({'error': 'User not found.'}), 404
        
    first_name = data.get('first_name', '').strip()
    last_name = data.get('last_name', '').strip()
    victor_number = data.get('victor_number', '').strip()
    email = data.get('email', '').strip()
    cell_phone = data.get('cell_phone', '').strip()
    iar_first_name = data.get('iar_first_name', '').strip()
    iar_last_name = data.get('iar_last_name', '').strip()
    iar_member_id = data.get('IAR_memberId', '').strip()
    iar_agency = data.get('IAR_Agency', '').strip()
    iar_username = data.get('IAR_Username', '').strip()
    is_active = data.get('is_active', True)
    is_admin = bool(data.get('is_admin', False))
    weekly_reminder_email = bool(data.get('weekly_reminder_email', True))
    
    if not last_name or not victor_number:
        return jsonify({'error': 'Last Name and Victor Number are required.'}), 400
        
    # Check self-deactivation restriction
    if old_key == session.get('user_key') and not is_active:
        return jsonify({'error': 'You cannot deactivate your own account.'}), 400
        
    # Check self-demotion restriction
    if old_key == session.get('user_key') and not is_admin:
        return jsonify({'error': 'You cannot remove your own administrator privileges.'}), 400
        
    new_key = f"{last_name.lower()}_{victor_number.lower()}"
    if new_key != old_key and new_key in users:
        return jsonify({'error': 'A user with this Last Name and Victor Number already exists.'}), 400
        
    profile = users[old_key]
    
    profile['first_name'] = first_name
    profile['last_name'] = last_name
    profile['victor_number'] = victor_number
    profile['email'] = email
    profile['cell_phone'] = cell_phone
    profile['iar_first_name'] = iar_first_name or None
    profile['iar_last_name'] = iar_last_name or None
    profile['IAR_memberId'] = iar_member_id
    profile['IAR_Agency'] = iar_agency
    profile['IAR_Username'] = iar_username
    profile['is_active'] = is_active
    profile['is_admin'] = is_admin
    profile['weekly_reminder_email'] = weekly_reminder_email
    
    if new_key != old_key:
        users[new_key] = profile
        del users[old_key]
        if old_key == session.get('user_key'):
            session['user_key'] = new_key
            session['last_name'] = last_name
            session['victor_number'] = victor_number
            session['is_admin'] = is_admin
            logger.info(f"Admin renamed themselves from {old_key} to {new_key}")
    else:
        users[old_key] = profile
        if old_key == session.get('user_key'):
            session['is_admin'] = is_admin
        
    save_users(users)
    logger.info(f"Admin {session.get('last_name')} updated user {new_key}")
    return jsonify({'success': True})

@app.route('/api/admin/users', methods=['DELETE'])
def delete_admin_user():
    if not session.get('authenticated') or not is_session_admin():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    user_key = data.get('user_key')
    if not user_key:
        return jsonify({'error': 'Missing user_key'}), 400
        
    if user_key == session.get('user_key'):
        return jsonify({'error': 'You cannot delete your own account.'}), 400
        
    users = load_users()
    if user_key not in users:
        return jsonify({'error': 'User not found.'}), 404
        
    del users[user_key]
    save_users(users)
    logger.info(f"Admin {session.get('last_name')} deleted user {user_key}")
    return jsonify({'success': True})

@app.route('/api/admin/users/reset-password', methods=['POST'])
def reset_admin_user_password():
    if not session.get('authenticated') or not is_session_admin():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    user_key = data.get('user_key')
    if not user_key:
        return jsonify({'error': 'Missing user_key'}), 400
        
    users = load_users()
    if user_key not in users:
        return jsonify({'error': 'User not found.'}), 404
        
    users[user_key]['password_hash'] = generate_password_hash("password123")
    users[user_key]['is_default'] = True
    save_users(users)
    logger.info(f"Admin {session.get('last_name')} reset password for user {user_key}")
    return jsonify({'success': True})

@app.route('/admin/settings', methods=['GET', 'POST'])
def admin_settings():
    if session.get('must_change_password'):
        return redirect(url_for('change_password'))
    if not session.get('authenticated'):
        return redirect(url_for('login'))
    if not is_session_admin():
        logger.warning(f"Unauthorized System Settings access attempt by {session.get('last_name')}")
        return redirect(url_for('index'))
        
    error = None
    success = None
    settings = load_settings()
    
    if request.method == 'POST':
        allow_iar_status_change = request.form.get('allow_iar_status_change') == 'yes'
        settings['allow_iar_status_change'] = allow_iar_status_change
        if save_settings(settings):
            success = "Settings saved successfully!"
            logger.info(f"System settings updated by admin {session.get('last_name')}")
        else:
            error = "Failed to save settings. Please try again."
            
    return render_template(
        'settings.html',
        allow_iar_status_change=settings.get('allow_iar_status_change', True),
        error=error,
        success=success
    )

if __name__ == '__main__':
    app.run(debug=True, port=5000)
