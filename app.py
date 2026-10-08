import os
import sqlite3
import subprocess
import datetime
import random
import shutil
import sys
import time
import atexit
from io import BytesIO

import psycopg2
from psycopg2 import OperationalError as PostgreSQLOperationalError
from psycopg2.extras import DictCursor

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from flask import (
    Flask, request, redirect, url_for, session, 
    render_template_string, send_from_directory, jsonify, send_file
)
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "imana_free_interest_microfinance_secret_key_2026")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
BACKUP_FOLDER = os.path.join(BASE_DIR, 'backups')

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'pdf'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['BACKUP_FOLDER'] = BACKUP_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(BACKUP_FOLDER, exist_ok=True)

NOTIFICATIONS = []

# List of Supported Ethiopian Banks for RTGS Transfers
ETHIOPIAN_BANKS = [
    "Commercial Bank of Ethiopia (CBE)",
    "Cooperative Bank of Oromia (CBO)",
    "Awash Bank",
    "Dashen Bank",
    "Bank of Abyssinia",
    "Hibret Bank",
    "Nib International Bank",
    "United Bank",
    "Zemen Bank",
    "Wegagen Bank",
    "Oromia Bank",
    "Amhara Bank",
    "Global Bank Ethiopia",
    "Hijra Bank (Islamic)",
    "ZamZam Bank (Islamic)",
    "Siinqee Bank",
    "Gadaa Bank"
]

def compress_and_save_image(file_storage, target_filename, max_size=(300, 300), quality=35):
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], target_filename)
    filename = file_storage.filename.lower()
    
    if filename.endswith('.pdf') or not HAS_PIL:
        file_storage.save(filepath)
        return target_filename

    try:
        image = Image.open(file_storage)
        if image.mode in ("RGBA", "P"):
            image = image.convert("RGB")
        
        image.thumbnail(max_size, Image.Resampling.LANCZOS)
        image.save(filepath, "JPEG", optimize=True, quality=quality)
        return target_filename
    except Exception as e:
        print(f"Image compression error: {e}")
        file_storage.save(filepath)
        return target_filename

def _translate_sql_placeholders(sql):
    """Keep SQLite style ? placeholders compatible with PostgreSQL (%s)."""
    return sql.replace("?", "%s")

class CompatiblePostgresCursor(DictCursor):
    """PostgreSQL cursor supporting legacy ? parameter replacement."""
    def execute(self, query, vars=None):
        return super().execute(_translate_sql_placeholders(query), vars)

    def executemany(self, query, vars_list):
        return super().executemany(_translate_sql_placeholders(query), vars_list)

def get_db_connection(max_retries=10, delay=0.5):
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is not set")

    for attempt in range(max_retries):
        try:
            conn = psycopg2.connect(
                database_url,
                connect_timeout=15,
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=5,
                cursor_factory=CompatiblePostgresCursor,
            )
            return conn
        except PostgreSQLOperationalError as e:
            if attempt < max_retries - 1:
                time.sleep(delay)
            else:
                raise e

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_commission(amount):
    if 1000 <= amount <= 3000:
        return 50.0
    elif 3001 <= amount <= 5000:
        return 80.0
    elif 5001 <= amount <= 10000:
        return 100.0
    elif 10001 <= amount <= 20000:
        return 200.0
    elif 20001 <= amount <= 40000:
        return 400.0
    elif amount > 40001:
        return 500.0
    return 0.0

def send_sms_alert(phone_number, message):
    print(f"📱 [SMS SENT TO {phone_number}]: {message}")

def add_notification(message):
    now = datetime.datetime.now().strftime("%H:%M:%S")
    NOTIFICATIONS.insert(0, f"[{now}] {message}")
    if len(NOTIFICATIONS) > 20:
        NOTIFICATIONS.pop()

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # System Staff Users Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            role TEXT NOT NULL,
            status TEXT DEFAULT 'ACTIVE'
        )
    """)

    cursor.execute("SELECT COUNT(*) FROM users")
    if cursor.fetchone()[0] == 0:
        default_users = [
            ('ceo', 'ceo999', 'CEO', 'ACTIVE'),
            ('manager1', 'manager123', 'MANAGER', 'ACTIVE'),
            ('maker1', 'maker123', 'MAKER', 'ACTIVE'),
            ('auditor1', 'auditor123', 'AUDITOR', 'ACTIVE'),
            ('officer1', 'officer123', 'LOAN_OFFICER', 'ACTIVE'),
            ('ext_agent1', 'agent123', 'EXTERNAL_AGENT', 'ACTIVE')
        ]
        cursor.executemany("INSERT INTO users VALUES (?, ?, ?, ?)", default_users)

    # Customers Table (with Mobile PIN field)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            customer_id TEXT PRIMARY KEY,
            full_name TEXT,
            phone TEXT,
            gender TEXT DEFAULT 'Dhiira',
            account_type TEXT DEFAULT 'WADIA',
            photo_path TEXT,
            signature_path TEXT,
            national_id_path TEXT DEFAULT '',
            balance REAL DEFAULT 0.0,
            status TEXT DEFAULT 'PENDING_APPROVAL',
            freeze_status TEXT DEFAULT 'UNFROZEN',
            freeze_reason TEXT DEFAULT '',
            mobile_pin TEXT DEFAULT '1234',
            mobile_status TEXT DEFAULT 'INACTIVE',
            created_at TEXT
        )
    """)

    # Transactions Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            txn_id TEXT PRIMARY KEY,
            txn_type TEXT,
            customer_id TEXT,
            customer_name TEXT,
            target_account TEXT,
            amount REAL,
            commission REAL DEFAULT 0.0,
            bank_name TEXT,
            ft_reference TEXT,
            status TEXT DEFAULT 'PENDING_MANAGER',
            created_by TEXT,
            timestamp TEXT,
            audited_status TEXT DEFAULT 'OPEN',
            reason TEXT DEFAULT ''
        )
    """)

    # Reversals Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reversals (
            reversal_id TEXT PRIMARY KEY,
            txn_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            manager_approved INTEGER DEFAULT 0,
            ceo_approved INTEGER DEFAULT 0,
            status TEXT DEFAULT 'PENDING_APPROVAL',
            timestamp TEXT
        )
    """)

    # Islamic Financing Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS islamic_financing (
            loan_id TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL,
            customer_name TEXT,
            financing_type TEXT NOT NULL,
            principal_amount REAL NOT NULL,
            profit_margin REAL DEFAULT 0.0,
            total_repayment REAL NOT NULL,
            tenure_months INTEGER,
            monthly_installment REAL,
            status TEXT DEFAULT 'PENDING_MANAGER',
            manager_approved INTEGER DEFAULT 0,
            ceo_approved INTEGER DEFAULT 0,
            agent_notes TEXT,
            created_by TEXT,
            timestamp TEXT
        )
    """)

    cursor.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS reason TEXT DEFAULT ''")
    cursor.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS mobile_pin TEXT DEFAULT '1234'")
    cursor.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS mobile_status TEXT DEFAULT 'ACTIVE'")

    conn.commit()
    conn.close()

try:
    init_db()
except Exception as e:
    print(f"⚠️ [DB INIT WARNING]: {e}")

def get_bank_capital():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(amount) FROM transactions WHERE status='APPROVED' AND txn_type IN ('DEPOSIT', 'MOBILE_TOPUP_REC')")
    total_deposit = cursor.fetchone()[0] or 0.0
    
    cursor.execute("SELECT SUM(amount) FROM transactions WHERE status='APPROVED' AND txn_type IN ('WITHDRAWAL', 'T24_TRANSFER', 'RTGS_TRANSFER', 'WALLET_TOPUP', 'AIRTIME_TOPUP')")
    total_withdraw = cursor.fetchone()[0] or 0.0
    
    cursor.execute("SELECT SUM(balance) FROM customers WHERE status='ACTIVE'")
    total_cust_balance = cursor.fetchone()[0] or 0.0

    cursor.execute("SELECT SUM(commission) FROM transactions WHERE status='APPROVED'")
    total_commission = cursor.fetchone()[0] or 0.0

    cursor.execute("SELECT SUM(balance) FROM customers WHERE status='ACTIVE' AND account_type='MUDARABA'")
    total_mudaraba_deposits = cursor.fetchone()[0] or 0.0

    mudaraba_gross_profit = total_mudaraba_deposits * 0.10
    mudaraba_ceo_share = mudaraba_gross_profit * 0.50
    mudaraba_customer_share = mudaraba_gross_profit * 0.50
    
    net_capital = total_deposit - total_withdraw + total_commission
    conn.close()
    return max(0.0, net_capital), total_deposit, total_withdraw, total_cust_balance, total_commission, total_mudaraba_deposits, mudaraba_gross_profit, mudaraba_ceo_share, mudaraba_customer_share

HTML_LAYOUT = """
<!DOCTYPE html>
<html lang="om">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Imana Free Interest Microfinance</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        body { background-color: #f1f5f9; padding-bottom: 80px; color: #0f172a; }
        nav { background: linear-gradient(135deg, #065f46, #047857); color: white; padding: 12px 16px; position: sticky; top: 0; z-index: 50; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }
        .logo-container { display: flex; align-items: center; gap: 10px; }
        .logo-svg { width: 32px; height: 32px; fill: #fbbf24; }
        nav h1 { font-size: 15px; font-weight: 800; letter-spacing: 0.3px; color: #ffffff; }
        .role-badge { background: #0284c7; padding: 3px 8px; border-radius: 4px; font-weight: 600; font-size: 11px; }
        .container { max-width: 600px; margin: 0 auto; padding: 16px; }
        .notification-bar { background: #fef3c7; color: #92400e; padding: 8px 12px; border-radius: 8px; font-size: 11px; margin-bottom: 12px; font-weight: bold; border: 1px solid #fde68a; }
        .card-net { background: linear-gradient(135deg, #064e3b, #047857); color: white; border-radius: 16px; padding: 20px; box-shadow: 0 10px 15px -3px rgba(6,78,59,0.3); margin-bottom: 20px; }
        .card-ceo-profit { background: linear-gradient(135deg, #4c1d95, #6b21a8); color: white; border-radius: 16px; padding: 20px; box-shadow: 0 10px 15px -3px rgba(76,29,149,0.3); margin-bottom: 20px; }
        .card-mobile-header { background: linear-gradient(135deg, #1e3a8a, #2563eb); color: white; border-radius: 16px; padding: 20px; box-shadow: 0 10px 15px -3px rgba(37,99,235,0.3); margin-bottom: 20px; }
        .net-title { font-size: 12px; opacity: 0.9; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
        .net-amount { font-size: 30px; font-weight: 800; color: #fbbf24; }
        .net-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 16px; border-top: 1px solid rgba(255,255,255,0.2); font-size: 12px; padding-top: 12px; }
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
        .grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }
        .btn-card { background: white; padding: 16px; border-radius: 12px; border: 1px solid #e2e8f0; display: flex; flex-direction: column; align-items: center; text-decoration: none; color: #334155; font-weight: bold; font-size: 13px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.05); transition: 0.2s; }
        .btn-card:active { transform: scale(0.97); }
        .btn-card span.icon { font-size: 26px; margin-bottom: 8px; }
        .btn-card-ceo { background: #faf5ff; border-color: #e9d5ff; color: #581c87; }
        .btn-card-auditor { background: #fff7ed; border-color: #ffedd5; color: #c2410c; }
        .btn-card-mobile { background: #eff6ff; border-color: #bfdbfe; color: #1d4ed8; }
        .bottom-nav { position: fixed; bottom: 0; left: 0; right: 0; background: white; border-top: 1px solid #e2e8f0; display: flex; justify-content: space-around; padding: 10px 0; z-index: 50; }
        .bottom-nav a { text-align: center; color: #64748b; text-decoration: none; font-size: 11px; flex: 1; font-weight: 500; }
        .bottom-nav a span.icon { display: block; font-size: 18px; margin-bottom: 2px; }
        .box { background: white; padding: 20px; border-radius: 12px; border: 1px solid #e2e8f0; box-shadow: 0 1px 3px rgba(0,0,0,0.05); margin-bottom: 16px; }
        .form-group { margin-bottom: 12px; position: relative; }
        .form-group label { display: block; font-size: 12px; font-weight: bold; color: #475569; margin-bottom: 4px; }
        .input-field { width: 100%; padding: 11px; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 14px; outline: none; }
        .input-field:focus { border-color: #047857; }
        .btn-submit { width: 100%; background: #047857; color: white; border: none; padding: 12px; border-radius: 8px; font-weight: bold; font-size: 14px; cursor: pointer; }
        .badge { padding: 3px 8px; border-radius: 4px; font-size: 10px; font-weight: bold; display: inline-block; }
        .badge-pending { background: #fef3c7; color: #92400e; }
        .badge-active { background: #dcfce7; color: #166534; }
        .badge-danger { background: #fee2e2; color: #991b1b; }
        .item-card { background: white; border-radius: 12px; padding: 14px; margin-bottom: 12px; border: 1px solid #e2e8f0; }
        .btn-action { padding: 8px 14px; border-radius: 6px; color: white; text-decoration: none; font-size: 12px; font-weight: bold; display: inline-block; border:none; cursor:pointer; }
        .btn-blue { background: #2563eb; }
        .btn-green { background: #16a34a; }
        .btn-red { background: #dc2626; }
        .btn-purple { background: #7c3aed; }
        .pwd-toggle { position: absolute; right: 10px; top: 32px; cursor: pointer; user-select: none; font-size: 14px; }
        @media print {
            .bottom-nav, nav, .no-print { display: none !important; }
            body { padding-bottom: 0; background: white; }
            .box { border: none; box-shadow: none; }
        }
    </style>
</head>
<body>
    <nav class="no-print">
        <div class="logo-container">
            <svg class="logo-svg" viewBox="0 0 24 24">
                <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>
            </svg>
            <h1>Imana Free Interest Microfinance</h1>
        </div>
        {% if session.get('role') %}
            <div style="font-size:12px;">
                <span style="margin-right:4px;"><b>{{ session['username'] }}</b></span>
                <span class="role-badge">{{ session['role'] }}</span>
                <a href="/change_password" style="color: #fde047; margin-left:8px; text-decoration:none;">🔑 Password</a>
                <a href="/logout" style="color: #fca5a5; margin-left:8px; text-decoration:none;">Logout</a>
            </div>
        {% elif session.get('mobile_cust_id') %}
            <div style="font-size:12px;">
                <span style="margin-right:4px;">📱 <b>{{ session['mobile_name'] }}</b></span>
                <a href="/mobile_change_pin" style="color: #fde047; margin-left:8px; text-decoration:none;">🔑 PIN Jijjiiri</a>
                <a href="/mobile_logout" style="color: #fca5a5; margin-left:8px; text-decoration:none;">Logout</a>
            </div>
        {% else %}
            <a href="/mobile_login" style="color: #fbbf24; font-size: 12px; font-weight: bold; text-decoration: none;">📱 Mobile Banking</a>
        {% endif %}
    </nav>

    <div class="container">
        {% if notifications %}
            <div class="notification-bar no-print">
                🔔 NOTIFICATION: {{ notifications[0] }}
            </div>
        {% endif %}
        {% block content %}{% endblock %}
    </div>

    {% if session.get('role') %}
    <div class="bottom-nav no-print">
        <a href="/"><span class="icon">🏠</span>Dashboard</a>
        {% if session['role'] == 'MAKER' %}
            <a href="/register"><span class="icon">👤</span>Galmee</a>
            <a href="/transaction"><span class="icon">💸</span>Kaffaltii</a>
            <a href="/maker_receipts"><span class="icon">🧾</span>Nagahee</a>
        {% endif %}
        {% if session['role'] == 'MANAGER' %}
            <a href="/pending"><span class="icon">📋</span>Manager Appr</a>
            <a href="/reversals_list"><span class="icon">🔄</span>Reversals</a>
            <a href="/print_receipt_search"><span class="icon">🖨️</span>Nagahee</a>
        {% endif %}
        {% if session['role'] == 'AUDITOR' %}
            <a href="/pending"><span class="icon">📋</span>Auditor View</a>
            <a href="/auditor_reversal_request"><span class="icon">⚠️</span>Reversal</a>
            <a href="/print_receipt_search"><span class="icon">🖨️</span>Nagahee</a>
        {% endif %}
        {% if session['role'] == 'CEO' %}
            <a href="/reversals_list" style="color: #581c87;"><span class="icon">🔄</span>Reversal CEO</a>
            <a href="/ceo_commission" style="color: #581c87;"><span class="icon">💰</span>Commission</a>
            <a href="/manage_users" style="color: #6b21a8;"><span class="icon">⚙️</span>Hojjattoota</a>
        {% endif %}
    </div>
    {% elif session.get('mobile_cust_id') %}
    <div class="bottom-nav no-print">
        <a href="/mobile_dashboard"><span class="icon">📱</span>Dasshbordii</a>
        <a href="/mobile_transfer"><span class="icon">🔄</span>Transfer</a>
        <a href="/mobile_rtgs"><span class="icon">🏛️</span>RTGS Bank</a>
        <a href="/mobile_topup"><span class="icon">⚡</span>Wallet/Topup</a>
        <a href="/mobile_statement"><span class="icon">📜</span>Statement</a>
    </div>
    {% endif %}

    <script>
    function togglePasswordVisibility(inputId, toggleIconId) {
        var input = document.getElementById(inputId);
        var icon = document.getElementById(toggleIconId);
        if (input.type === "password") {
            input.type = "text";
            icon.textContent = "🙈";
        } else {
            input.type = "password";
            icon.textContent = "👁️";
        }
    }
    </script>
</body>
</html>
"""

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/api/get_customer/<cust_id>')
def api_get_customer(cust_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT customer_id, full_name, phone, photo_path, signature_path, freeze_status, freeze_reason, balance FROM customers WHERE customer_id = ?", (cust_id,))
    cust = cursor.fetchone()
    conn.close()
    
    if cust:
        return jsonify({
            'success': True,
            'customer_id': cust['customer_id'],
            'full_name': cust['full_name'],
            'phone': cust['phone'],
            'photo_path': cust['photo_path'],
            'signature_path': cust['signature_path'],
            'freeze_status': cust['freeze_status'],
            'freeze_reason': cust['freeze_reason'],
            'balance': cust['balance']
        })
    return jsonify({'success': False, 'message': 'Maammilli hin argamne'})

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT username, role, status FROM users WHERE username = ? AND password = ?", (username, password))
        user = cursor.fetchone()
        conn.close()

        if user:
            if user['status'] == 'BLOCKED':
                error = "🚫 Akkaawunttii keessan UGGURAMEERA! CEO qunnamaa."
            else:
                session.clear()
                session['username'] = user['username']
                session['role'] = user['role']
                return redirect('/')
        else:
            error = "Username ykn Password dogoggoraa!"

    err_html = f"<p style='color:red; font-size:12px; text-align:center; margin-bottom:12px;'>{error}</p>" if error else ""
    content = f"""
    <div class="box" style="margin-top: 30px; text-align: center;">
        <div style="font-size: 40px; margin-bottom: 10px;">🏦</div>
        <h2 style="font-size: 17px; margin-bottom: 4px; color:#065f46;">Imana Free Interest Microfinance</h2>
        <p style="font-size: 12px; color: #64748b; margin-bottom: 16px;">Seensa Systema (Staff Login)</p>
        {err_html}
        <form method="POST">
            <div class="form-group" style="text-align:left;">
                <label>Username</label>
                <input type="text" name="username" placeholder="Fkn: ceo, manager1, maker1" class="input-field" required>
            </div>
            <div class="form-group" style="text-align:left;">
                <label>Password</label>
                <input type="password" id="login_password" name="password" placeholder="Password" class="input-field" required>
                <span id="login_pwd_toggle" class="pwd-toggle" onclick="togglePasswordVisibility('login_password', 'login_pwd_toggle')">👁️</span>
            </div>
            <button type="submit" class="btn-submit">Seeni (Staff Login)</button>
        </form>
        <div style="margin-top:20px; border-top:1px solid #e2e8f0; padding-top:15px;">
            <a href="/mobile_login" style="color:#2563eb; text-decoration:none; font-weight:bold; font-size:13px;">📲 Seensa Mobile Banking Maammilaa</a>
        </div>
    </div>
    """
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", content), notifications=NOTIFICATIONS)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

# ==============================================================================
# 📱 MOBILE BANKING MODULE (TRANSFER, STATEMENT, BALANCE, RTGS, WALLET & TOPUP)
# ==============================================================================

@app.route('/mobile_login', methods=['GET', 'POST'])
def mobile_login():
    msg = None
    if request.method == 'POST':
        cust_id = request.form.get('customer_id', '').strip()
        pin = request.form.get('mobile_pin', '').strip()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT customer_id, full_name, phone, balance, status, freeze_status, mobile_pin, mobile_status FROM customers WHERE customer_id = ?", (cust_id,))
        cust = cursor.fetchone()
        conn.close()

        if cust:
            if cust['status'] != 'ACTIVE':
                msg = "❌ Akkaawunttiin keessan mirkanaa'uu ykn sassaabamuu qaba. Baankii qunnamaa."
            elif cust['mobile_status'] == 'PENDING_APPROVAL':
                msg = "⏱️ Gaaffiin Mobile Banking keessan Mirkaneessa Manager eegaa jira (Pending Manager Approval)!"
            elif cust['mobile_status'] != 'ACTIVE':
                msg = "❌ Tajaajilli Mobile Banking akkaawuntii kanaaf hin banamne! Maker/Baankii qunnamaa."
            elif cust['freeze_status'] == 'FROZEN':
                msg = "🚫 Akkaawunttiin keessan uggurameera (Frozen)."
            elif cust['mobile_pin'] != pin:
                msg = "❌ PIN Mobile Banking dogoggoraa!"
            else:
                session.clear()
                session['mobile_cust_id'] = cust['customer_id']
                session['mobile_name'] = cust['full_name']
                session['mobile_phone'] = cust['phone']
                return redirect('/mobile_dashboard')
        else:
            msg = "❌ Lakkoofsa Akkaawuntii argachuu hin dandeenye!"

    content = f"""
    <div class="box" style="margin-top:20px; text-align:center;">
        <div style="font-size:45px; margin-bottom:10px;">📱</div>
        <h2 style="font-size:18px; color:#1e3a8a; margin-bottom:4px;">Mobile Banking Seensa</h2>
        <p style="font-size:12px; color:#64748b; margin-bottom:16px;">Imana Free Interest Microfinance Mobile Services</p>
        {f"<p style='background:#fee2e2; color:#991b1b; padding:10px; border-radius:8px; font-size:12px; margin-bottom:12px; font-weight:bold;'>{msg}</p>" if msg else ""}
        <form method="POST">
            <div class="form-group" style="text-align:left;">
                <label>Lakkoofsa Akkaawuntii (Account No)</label>
                <input type="text" name="customer_id" placeholder="Fkn: 100099008800" class="input-field" required>
            </div>
            <div class="form-group" style="text-align:left;">
                <label>PIN Passcode Mobile Banking (Default: 1234)</label>
                <input type="password" name="mobile_pin" maxlength="6" placeholder="****" class="input-field" required>
            </div>
            <button type="submit" class="btn-submit" style="background:#1d4ed8;">📲 Seeni Mobile Banking</button>
        </form>
    </div>
    """
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", content), notifications=NOTIFICATIONS)

@app.route('/mobile_logout')
def mobile_logout():
    session.clear()
    return redirect('/mobile_login')

@app.route('/mobile_dashboard')
def mobile_dashboard():
    if 'mobile_cust_id' not in session:
        return redirect('/mobile_login')

    cust_id = session['mobile_cust_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT customer_id, full_name, phone, balance, account_type FROM customers WHERE customer_id = ?", (cust_id,))
    cust = cursor.fetchone()

    cursor.execute("SELECT txn_id, txn_type, amount, bank_name, status, timestamp, ft_reference FROM transactions WHERE customer_id = ? ORDER BY timestamp DESC LIMIT 5", (cust_id,))
    recent_txns = cursor.fetchall()
    conn.close()

    rows_html = ""
    for t in recent_txns:
        color = "#16a34a" if t['txn_type'] in ['DEPOSIT', 'MOBILE_TOPUP_REC'] else "#dc2626"
        sign = "+" if t['txn_type'] in ['DEPOSIT', 'MOBILE_TOPUP_REC'] else "-"
        rows_html += f"""
        <div style="display:flex; justify-shadow:space-between; justify-content:space-between; align-items:center; padding:10px 0; border-bottom:1px solid #f1f5f9; font-size:12px;">
            <div>
                <div style="font-weight:bold; color:#1e293b;">{t['txn_type']}</div>
                <div style="font-size:10px; color:#64748b;">{t['timestamp']} | Ref: {t['ft_reference']}</div>
            </div>
            <div style="text-align:right;">
                <div style="font-weight:800; color:{color};">{sign}{t['amount']:,.2f} Birr</div>
                <span class="badge badge-active" style="font-size:9px;">{t['status']}</span>
            </div>
        </div>
        """

    content = f"""
    <div class="card-mobile-header">
        <div class="net-title">Baga Nagaan Dhuftan 👋</div>
        <div style="font-size:20px; font-weight:bold;">{cust['full_name']}</div>
        <div style="font-size:11px; opacity:0.9; margin-top:2px;">Acc: <b>{cust['customer_id']}</b> ({cust['account_type']})</div>
        <div style="margin-top:16px; border-top:1px solid rgba(255,255,255,0.2); padding-top:12px;">
            <div class="net-title">Haafeeka Herregaa (Current Balance)</div>
            <div class="net-amount">{cust['balance']:,.2f} Birr</div>
        </div>
    </div>

    <h3 style="font-size:14px; margin-bottom:10px; color:#334155;">⚡ Tajaajila Mobile Banking</h3>
    <div class="grid-2" style="margin-bottom:20px;">
        <a href="/mobile_transfer" class="btn-card btn-card-mobile"><span class="icon">🔄</span><span>Imana Bank Transfer</span></a>
        <a href="/mobile_rtgs" class="btn-card btn-card-mobile"><span class="icon">🏛️</span><span>Other Bank (RTGS)</span></a>
        <a href="/mobile_topup" class="btn-card btn-card-mobile"><span class="icon">⚡</span><span>Wallet & Airtime</span></a>
        <a href="/mobile_statement" class="btn-card btn-card-mobile"><span class="icon">📜</span><span>Bank Statement</span></a>
        <a href="/mobile_change_pin" class="btn-card btn-card-mobile"><span class="icon">🔑</span><span>PIN Passcode Jijjiiri</span></a>
    </div>

    <div class="box">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
            <h3 style="font-size:13px; color:#475569;">📊 Soochii Dhiyootti Hojjataman</h3>
            <a href="/mobile_statement" style="font-size:11px; color:#2563eb; font-weight:bold; text-decoration:none;">Hunda Ilaali &rarr;</a>
        </div>
        {rows_html if rows_html else '<p style="font-size:12px; color:#94a3b8; text-align:center;">Sochiin transaction keessan argame hin jiru.</p>'}
    </div>
    """
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", content), notifications=NOTIFICATIONS)

@app.route('/mobile_transfer', methods=['GET', 'POST'])
def mobile_transfer():
    if 'mobile_cust_id' not in session:
        return redirect('/mobile_login')

    msg = None
    sender_id = session['mobile_cust_id']

    if request.method == 'POST':
        target_acc = request.form.get('target_account', '').strip()
        amount = float(request.form.get('amount', 0.0))
        pin = request.form.get('pin', '').strip()
        reason = request.form.get('reason', 'Mobile Internal Transfer')

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT customer_id, balance, mobile_pin, freeze_status, full_name FROM customers WHERE customer_id = ?", (sender_id,))
        sender = cursor.fetchone()

        cursor.execute("SELECT customer_id, full_name, status, freeze_status FROM customers WHERE customer_id = ?", (target_acc,))
        receiver = cursor.fetchone()

        if not sender or sender['mobile_pin'] != pin:
            msg = "❌ PIN Mobile Banking dogoggoraa!"
        elif sender['freeze_status'] == 'FROZEN':
            msg = "🚫 Akkaawunttiin keessan uggurameera."
        elif not receiver or receiver['status'] != 'ACTIVE':
            msg = "❌ Akkaawunttiin simataa (Receiver Account) hin jiru ykn active miti!"
        elif receiver['freeze_status'] == 'FROZEN':
            msg = "❌ Akkaawunttiin simataa uggurameera!"
        elif target_acc == sender_id:
            msg = "❌ Akkaawuntii keessan irratti erguu hin dandeessan!"
        elif amount <= 0:
            msg = "❌ Hammi maallaqaa ziiroo ol ta'uu qaba!"
        elif sender['balance'] < amount:
            msg = "❌ Haafeeka gahaa hin qabdan (Insufficient Balance)!"
        else:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ft_ref = f"FT{datetime.datetime.now().strftime('%y%j')}{random.randint(10000, 99999)}"

            # Deduct Sender
            cursor.execute("UPDATE customers SET balance = balance - ? WHERE customer_id = ?", (amount, sender_id))
            # Add Receiver
            cursor.execute("UPDATE customers SET balance = balance + ? WHERE customer_id = ?", (amount, target_acc))

            # Record Txn Sender
            cursor.execute("""
                INSERT INTO transactions (txn_id, txn_type, customer_id, customer_name, target_account, amount, commission, bank_name, ft_reference, status, created_by, timestamp, reason)
                VALUES (?, 'T24_TRANSFER', ?, ?, ?, ?, 0.0, 'Imana Microfinance', ?, 'APPROVED', 'MOBILE_APP', ?, ?)
            """, (f"TXN-MOB-{random.randint(100000,999999)}", sender_id, sender['full_name'], target_acc, amount, ft_ref, now, reason))

            # Record Txn Receiver
            cursor.execute("""
                INSERT INTO transactions (txn_id, txn_type, customer_id, customer_name, target_account, amount, commission, bank_name, ft_reference, status, created_by, timestamp, reason)
                VALUES (?, 'MOBILE_TOPUP_REC', ?, ?, ?, ?, 0.0, 'Imana Microfinance', ?, 'APPROVED', 'MOBILE_APP', ?, ?)
            """, (f"TXN-MOB-REC-{random.randint(100000,999999)}", target_acc, receiver['full_name'], sender_id, amount, ft_ref, now, f"Gara: {sender['full_name']} irraa"))

            conn.commit()
            conn.close()

            add_notification(f"Mobile Transfer: {sender['full_name']} transfer {amount} Birr to {receiver['full_name']}.")
            return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", f"""
                <div class="box" style="text-align:center;">
                    <div style="font-size:50px;">✅</div>
                    <h2 style="color:#166534; font-size:18px;">Dabarsii Milkaa'inaa!</h2>
                    <p style="font-size:13px; color:#475569; margin-top:8px;">Hammamu: <b>{amount:,.2f} Birr</b></p>
                    <p style="font-size:12px; color:#64748b;">Simataa: <b>{receiver['full_name']}</b> ({target_acc})</p>
                    <p style="font-size:11px; color:#94a3b8; margin-top:4px;">Ref Number: <b>{ft_ref}</b></p>
                    <a href="/mobile_dashboard" class="btn-submit" style="display:block; margin-top:16px; text-decoration:none; background:#2563eb;">Gara Dasshbordii</a>
                </div>
            """), notifications=NOTIFICATIONS)

        conn.close()

    content = f"""
    <div class="box">
        <h2 style="font-size:16px; color:#1e3a8a; margin-bottom:12px;">🔄 Transfer (Keessoo Imana Bank)</h2>
        {f"<p style='background:#fee2e2; color:#991b1b; padding:10px; border-radius:8px; font-size:12px; margin-bottom:12px; font-weight:bold;'>{msg}</p>" if msg else ""}
        <form method="POST">
            <div class="form-group">
                <label>Lakkoofsa Akkaawuntii Simataa (Receiver Account)</label>
                <input type="text" name="target_account" placeholder="Fkn: 100099008800" class="input-field" required>
            </div>
            <div class="form-group">
                <label>Hamma Maallaqaa (Birr)</label>
                <input type="number" step="0.01" min="1" name="amount" placeholder="0.00" class="input-field" required>
            </div>
            <div class="form-group">
                <label>Sababa Transfer (Reason / Remark)</label>
                <input type="text" name="reason" placeholder="Kaffaltii tajaajilaa..." class="input-field">
            </div>
            <div class="form-group">
                <label>Passcode PIN Mobile Banking</label>
                <input type="password" name="pin" maxlength="6" placeholder="****" class="input-field" required>
            </div>
            <button type="submit" class="btn-submit" style="background:#2563eb;">🚀 Dabarsi (Transfer)</button>
        </form>
    </div>
    """
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", content), notifications=NOTIFICATIONS)

@app.route('/mobile_rtgs', methods=['GET', 'POST'])
def mobile_rtgs():
    if 'mobile_cust_id' not in session:
        return redirect('/mobile_login')

    msg = None
    sender_id = session['mobile_cust_id']

    if request.method == 'POST':
        selected_bank = request.form.get('bank_name')
        target_acc = request.form.get('target_account', '').strip()
        target_name = request.form.get('target_name', '').strip()
        amount = float(request.form.get('amount', 0.0))
        pin = request.form.get('pin', '').strip()
        rtgs_fee = 10.0 # Standard RTGS Service Commission

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT customer_id, balance, mobile_pin, freeze_status, full_name FROM customers WHERE customer_id = ?", (sender_id,))
        sender = cursor.fetchone()

        total_deduction = amount + rtgs_fee

        if not sender or sender['mobile_pin'] != pin:
            msg = "❌ PIN Mobile Banking dogoggoraa!"
        elif sender['freeze_status'] == 'FROZEN':
            msg = "🚫 Akkaawunttiin keessan uggurameera."
        elif amount <= 0:
            msg = "❌ Hammi maallaqaa ziiroo ol ta'uu qaba!"
        elif sender['balance'] < total_deduction:
            msg = f"❌ Haafeeka gahaa hin qabdan! Total: {total_deduction:,.2f} Birr (Transfer: {amount:,.2f} + RTGS Fee: {rtgs_fee} Birr)!"
        else:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ft_ref = f"RTGS{datetime.datetime.now().strftime('%y%j')}{random.randint(100000, 999999)}"

            # Deduct Amount + RTGS Fee from Sender
            cursor.execute("UPDATE customers SET balance = balance - ? WHERE customer_id = ?", (total_deduction, sender_id))

            # Record RTGS Transaction for Manager Approval / System Processing
            cursor.execute("""
                INSERT INTO transactions (txn_id, txn_type, customer_id, customer_name, target_account, amount, commission, bank_name, ft_reference, status, created_by, timestamp, reason)
                VALUES (?, 'RTGS_TRANSFER', ?, ?, ?, ?, ?, ?, ?, 'PENDING_MANAGER', 'MOBILE_APP', ?, ?)
            """, (f"TXN-RTGS-{random.randint(100000,999999)}", sender_id, sender['full_name'], target_acc, amount, rtgs_fee, selected_bank, ft_ref, now, f"Gara Bankii: {selected_bank} | Simataa: {target_name} ({target_acc})"))

            conn.commit()
            conn.close()

            add_notification(f"RTGS Transfer Request: {sender['full_name']} transferred {amount} Birr to {selected_bank}.")
            return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", f"""
                <div class="box" style="text-align:center;">
                    <div style="font-size:50px;">🏛️</div>
                    <h2 style="color:#065f46; font-size:18px;">RTGS Gara Bankii Biraa Ergameera!</h2>
                    <p style="font-size:12px; color:#475569; margin-top:8px;">Maallaqni gara <b>{selected_bank}</b> erguuf dhihaateera.</p>
                    <div style="background:#f8fafc; padding:12px; border-radius:8px; margin:12px 0; text-align:left; font-size:12px;">
                        <div>Bankii: <b>{selected_bank}</b></div>
                        <div>Maqaa Simataa: <b>{target_name}</b></div>
                        <div>Acc Simataa: <b>{target_acc}</b></div>
                        <div>Amount: <b>{amount:,.2f} Birr</b></div>
                        <div>RTGS Fee: <b>{rtgs_fee:,.2f} Birr</b></div>
                        <div>Reference: <b>{ft_ref}</b></div>
                    </div>
                    <p style="font-size:11px; color:#92400e; background:#fef3c7; padding:6px; border-radius:4px;">⏱️ Mirkaneessi RTGS Manager baankii keenyaan daqiiqaa muraasa keessatti xumurama.</p>
                    <a href="/mobile_dashboard" class="btn-submit" style="display:block; margin-top:16px; text-decoration:none; background:#047857;">Gara Dasshbordii</a>
                </div>
            """), notifications=NOTIFICATIONS)

        conn.close()

    bank_options = "".join([f"<option value='{b}'>{b}</option>" for b in ETHIOPIAN_BANKS])

    content = f"""
    <div class="box">
        <h2 style="font-size:16px; color:#065f46; margin-bottom:12px;">🏛️ Interbank Transfer (RTGS Bankoota Hundatti)</h2>
        {f"<p style='background:#fee2e2; color:#991b1b; padding:10px; border-radius:8px; font-size:12px; margin-bottom:12px; font-weight:bold;'>{msg}</p>" if msg else ""}
        <form method="POST">
            <div class="form-group">
                <label>Filannoo Bankii Simataa (Destination Bank)</label>
                <select name="bank_name" class="input-field" required>
                    {bank_options}
                </select>
            </div>
            <div class="form-group">
                <label>Maqaa Guutuu Simataa (Receiver Name)</label>
                <input type="text" name="target_name" placeholder="Fkn: Abebe Bikila" class="input-field" required>
            </div>
            <div class="form-group">
                <label>Lakkoofsa Akkaawuntii Bankii Simataa</label>
                <input type="text" name="target_account" placeholder="Fkn: 100022334455" class="input-field" required>
            </div>
            <div class="form-group">
                <label>Hamma Maallaqaa (Birr)</label>
                <input type="number" step="0.01" min="10" name="amount" placeholder="0.00" class="input-field" required>
            </div>
            <div class="form-group">
                <label>PIN Passcode Mobile Banking</label>
                <input type="password" name="pin" maxlength="6" placeholder="****" class="input-field" required>
            </div>
            <button type="submit" class="btn-submit" style="background:#047857;">🏛️ Ergi RTGS Interbank</button>
        </form>
    </div>
    """
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", content), notifications=NOTIFICATIONS)

@app.route('/mobile_topup', methods=['GET', 'POST'])
def mobile_topup():
    if 'mobile_cust_id' not in session:
        return redirect('/mobile_login')

    msg = None
    sender_id = session['mobile_cust_id']

    if request.method == 'POST':
        topup_type = request.form.get('topup_type') # TELEBIRR, CBE_BIRR, MPESA, ETHIO_AIRTIME, SAFARICOM_AIRTIME
        phone = request.form.get('phone', '').strip()
        amount = float(request.form.get('amount', 0.0))
        pin = request.form.get('pin', '').strip()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT customer_id, balance, mobile_pin, freeze_status, full_name FROM customers WHERE customer_id = ?", (sender_id,))
        sender = cursor.fetchone()

        if not sender or sender['mobile_pin'] != pin:
            msg = "❌ PIN Mobile Banking dogoggoraa!"
        elif sender['freeze_status'] == 'FROZEN':
            msg = "🚫 Akkaawunttiin keessan uggurameera."
        elif amount <= 0:
            msg = "❌ Hammi maallaqaa ziiroo ol ta'uu qaba!"
        elif sender['balance'] < amount:
            msg = "❌ Haafeeka gahaa hin qabdan!"
        else:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ft_ref = f"TP{datetime.datetime.now().strftime('%y%j')}{random.randint(100000, 999999)}"

            cursor.execute("UPDATE customers SET balance = balance - ? WHERE customer_id = ?", (amount, sender_id))

            cursor.execute("""
                INSERT INTO transactions (txn_id, txn_type, customer_id, customer_name, target_account, amount, commission, bank_name, ft_reference, status, created_by, timestamp, reason)
                VALUES (?, 'WALLET_TOPUP', ?, ?, ?, ?, 0.0, ?, ?, 'APPROVED', 'MOBILE_APP', ?, ?)
            """, (f"TXN-TOPUP-{random.randint(100000,999999)}", sender_id, sender['full_name'], phone, amount, topup_type, ft_ref, now, f"Tajaajila Topup: {topup_type} Lakk: {phone}"))

            conn.commit()
            conn.close()

            add_notification(f"Mobile Topup: {sender['full_name']} recharged {topup_type} ({amount} Birr).")
            return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", f"""
                <div class="box" style="text-align:center;">
                    <div style="font-size:50px;">⚡</div>
                    <h2 style="color:#166534; font-size:18px;">Topup / Recharge Milkaa'eera!</h2>
                    <p style="font-size:12px; color:#475569; margin-top:8px;">Tajaajila: <b>{topup_type}</b></p>
                    <p style="font-size:12px; color:#64748b;">Lakk Bilbilaa: <b>{phone}</b></p>
                    <p style="font-size:14px; font-weight:bold; color:#047857;">Amount: {amount:,.2f} Birr</p>
                    <p style="font-size:11px; color:#94a3b8; margin-top:4px;">Ref: <b>{ft_ref}</b></p>
                    <a href="/mobile_dashboard" class="btn-submit" style="display:block; margin-top:16px; text-decoration:none; background:#7c3aed;">Gara Dasshbordii</a>
                </div>
            """), notifications=NOTIFICATIONS)

        conn.close()

    content = f"""
    <div class="box">
        <h2 style="font-size:16px; color:#7c3aed; margin-bottom:12px;">⚡ Wallet & Airtime Topup Guutuu</h2>
        {f"<p style='background:#fee2e2; color:#991b1b; padding:10px; border-radius:8px; font-size:12px; margin-bottom:12px; font-weight:bold;'>{msg}</p>" if msg else ""}
        <form method="POST">
            <div class="form-group">
                <label>Filannoo Tajaajila Topup / Wallet</label>
                <select name="topup_type" class="input-field" required>
                    <option value="TELEBIRR_WALLET">Telebirr Wallet</option>
                    <option value="CBE_BIRR_WALLET">CBE Birr Wallet</option>
                    <option value="SAFARICOM_MPESA">Safaricom M-PESA</option>
                    <option value="ETHIO_TELECOM_AIRTIME">Ethio Telecom Airtime Cards</option>
                    <option value="SAFARICOM_AIRTIME">Safaricom Airtime</option>
                </select>
            </div>
            <div class="form-group">
                <label>Lakkoofsa Bilbilaa (Phone Number)</label>
                <input type="text" name="phone" placeholder="09... ykn 07..." value="{session.get('mobile_phone', '')}" class="input-field" required>
            </div>
            <div class="form-group">
                <label>Hamma Topup (Birr)</label>
                <input type="number" step="1" min="5" name="amount" placeholder="0.00" class="input-field" required>
            </div>
            <div class="form-group">
                <label>PIN Passcode Mobile Banking</label>
                <input type="password" name="pin" maxlength="6" placeholder="****" class="input-field" required>
            </div>
            <button type="submit" class="btn-submit" style="background:#7c3aed;">⚡ Topup Guuti</button>
        </form>
    </div>
    """
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", content), notifications=NOTIFICATIONS)

@app.route('/mobile_statement')
def mobile_statement():
    if 'mobile_cust_id' not in session:
        return redirect('/mobile_login')

    cust_id = session['mobile_cust_id']
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT customer_id, full_name, phone, balance, account_type FROM customers WHERE customer_id = ?", (cust_id,))
    cust = cursor.fetchone()

    cursor.execute("SELECT txn_id, txn_type, amount, commission, bank_name, status, timestamp, ft_reference, reason FROM transactions WHERE customer_id = ? ORDER BY timestamp DESC", (cust_id,))
    txns = cursor.fetchall()
    conn.close()

    rows_html = ""
    for t in txns:
        color = "#16a34a" if t['txn_type'] in ['DEPOSIT', 'MOBILE_TOPUP_REC'] else "#dc2626"
        sign = "+" if t['txn_type'] in ['DEPOSIT', 'MOBILE_TOPUP_REC'] else "-"
        rows_html += f"""
        <tr style="border-bottom:1px solid #e2e8f0; font-size:12px;">
            <td style="padding:8px;">{t['timestamp']}</td>
            <td style="padding:8px; font-weight:bold;">{t['ft_reference']}</td>
            <td style="padding:8px;">{t['txn_type']}</td>
            <td style="padding:8px; font-weight:bold; color:{color};">{sign}{t['amount']:,.2f} Birr</td>
            <td style="padding:8px;"><span class="badge badge-active">{t['status']}</span></td>
        </tr>
        """

    content = f"""
    <div class="box">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
            <h2 style="font-size:16px; color:#1e3a8a;">📜 Bank Statement</h2>
            <button onclick="window.print()" class="btn-action btn-purple no-print">🖨️ Maxxansi / Print</button>
        </div>
        <div style="background:#f8fafc; padding:12px; border-radius:8px; margin-bottom:16px; font-size:12px;">
            <div>Maqaa: <b>{cust['full_name']}</b></div>
            <div>Account: <b>{cust['customer_id']}</b> ({cust['account_type']})</div>
            <div>Haafeeka Ammaa: <b style="color:#16a34a; font-size:14px;">{cust['balance']:,.2f} Birr</b></div>
        </div>

        <table style="width:100%; border-collapse:collapse; text-align:left;">
            <thead>
                <tr style="background:#f1f5f9; font-size:11px; color:#64748b;">
                    <th style="padding:8px;">Guyyaa</th>
                    <th style="padding:8px;">Ref</th>
                    <th style="padding:8px;">Gosa</th>
                    <th style="padding:8px;">Amount</th>
                    <th style="padding:8px;">Status</th>
                </tr>
            </thead>
            <tbody>
                {rows_html if rows_html else '<tr><td colspan="5" style="padding:16px; text-align:center;">Sochiin herregaa hin jiru.</td></tr>'}
            </tbody>
        </table>
    </div>
    """
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", content), notifications=NOTIFICATIONS)

@app.route('/mobile_change_pin', methods=['GET', 'POST'])
def mobile_change_pin():
    if 'mobile_cust_id' not in session:
        return redirect('/mobile_login')

    msg = None
    msg_type = "green"
    cust_id = session['mobile_cust_id']

    if request.method == 'POST':
        old_pin = request.form.get('old_pin', '').strip()
        new_pin = request.form.get('new_pin', '').strip()
        confirm_pin = request.form.get('confirm_pin', '').strip()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT mobile_pin FROM customers WHERE customer_id = ?", (cust_id,))
        cust = cursor.fetchone()

        if not cust or cust['mobile_pin'] != old_pin:
            msg = "❌ PIN duraanii dogoggoraa!"
            msg_type = "red"
        elif new_pin != confirm_pin:
            msg = "❌ PIN haaraa fi Mirkaneessaan wal hin simne!"
            msg_type = "red"
        elif len(new_pin) != 4 or not new_pin.isdigit():
            msg = "❌ PIN-ni haaraa lakkoofsa digiti 4 ta'uu qaba!"
            msg_type = "red"
        else:
            cursor.execute("UPDATE customers SET mobile_pin = ? WHERE customer_id = ?", (new_pin, cust_id))
            conn.commit()
            msg = "✅ PIN Mobile Banking keessan milkaa'inaan jijjiirameera!"
            msg_type = "green"

        conn.close()

    content = f"""
    <div class="box">
        <h2 style="font-size:16px; color:#1e3a8a; margin-bottom:12px;">🔑 PIN Mobile Banking Jijjiiri</h2>
        {f"<p style='background:{'#dcfce7' if msg_type=='green' else '#fee2e2'}; color:{'#166534' if msg_type=='green' else '#991b1b'}; padding:10px; border-radius:6px; font-size:12px; font-weight:bold; margin-bottom:12px;'>{msg}</p>" if msg else ""}
        <form method="POST">
            <div class="form-group">
                <label>PIN Duraanii (Current PIN)</label>
                <input type="password" id="old_pin_id" name="old_pin" maxlength="6" placeholder="****" required class="input-field">
                <span id="old_pin_toggle" class="pwd-toggle" onclick="togglePasswordVisibility('old_pin_id', 'old_pin_toggle')">👁️</span>
            </div>
            <div class="form-group">
                <label>PIN Haaraa Digit 4 (New PIN)</label>
                <input type="password" id="new_pin_id" name="new_pin" maxlength="4" placeholder="****" required class="input-field">
                <span id="new_pin_toggle" class="pwd-toggle" onclick="togglePasswordVisibility('new_pin_id', 'new_pin_toggle')">👁️</span>
            </div>
            <div class="form-group">
                <label>PIN Haaraa Mirkaneessi (Confirm New PIN)</label>
                <input type="password" id="conf_pin_id" name="confirm_pin" maxlength="4" placeholder="****" required class="input-field">
                <span id="conf_pin_toggle" class="pwd-toggle" onclick="togglePasswordVisibility('conf_pin_id', 'conf_pin_toggle')">👁️</span>
            </div>
            <button type="submit" class="btn-submit" style="background:#1d4ed8;">💾 PIN Jijjiiri</button>
        </form>
    </div>
    """
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", content), notifications=NOTIFICATIONS)

# ==============================================================================
# 🏦 CORE BANKING STAFF & ADMIN ROUTES (MAKER, MANAGER, AUDITOR, CEO)
# ==============================================================================

@app.route('/ceo_commission')
def ceo_commission():
    if 'role' not in session or session['role'] not in ['CEO', 'MANAGER']:
        return "🚫 Hayyama CEO ykn MANAGER Qofa!", 403

    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    filter_type = request.args.get('filter_type', 'ALL')

    conn = get_db_connection()
    cursor = conn.cursor()

    query = "SELECT txn_id, ft_reference, customer_name, amount, commission, txn_type, timestamp, created_by FROM transactions WHERE status='APPROVED'"
    params = []

    if filter_type != 'ALL':
        query += " AND txn_type = ?"
        params.append(filter_type)

    if start_date:
        query += " AND timestamp >= ?"
        params.append(start_date + " 00:00:00")
    if end_date:
        query += " AND timestamp <= ?"
        params.append(end_date + " 23:59:59")

    query += " ORDER BY timestamp DESC"
    cursor.execute(query, params)
    txns = cursor.fetchall()
    conn.close()

    total_amount = sum([t['amount'] for t in txns])
    total_comm = sum([t['commission'] for t in txns])
    rows_html = ""
    for t in txns:
        rows_html += f"""
        <tr style="border-bottom:1px solid #e2e8f0; font-size:12px;">
            <td style="padding:8px;">{t['timestamp']}</td>
            <td style="padding:8px; font-weight:bold;">{t['ft_reference']}</td>
            <td style="padding:8px;">{t['customer_name']}</td>
            <td style="padding:8px;"><span class="badge badge-active">{t['txn_type']}</span></td>
            <td style="padding:8px;">{t['amount']:,.2f} Birr</td>
            <td style="padding:8px; font-weight:bold; color:#047857;">+{t['commission']:,.2f} Birr</td>
            <td style="padding:8px; font-size:11px; color:#64748b;">{t['created_by']}</td>
        </tr>
        """

    content = f"""
    <div class="card-ceo-profit">
        <div class="net-title">💰 FILTARA TRANSACTION & COMMISSION ({session['role']})</div>
        <div class="net-amount">{total_comm:,.2f} Birr Comm</div>
        <p style="font-size:11px; opacity:0.9; margin-top:4px;">Waliigala Hamma Txn: <b>{total_amount:,.2f} Birr</b></p>
    </div>

    <div class="box">
        <form method="GET" style="display:flex; flex-direction:column; gap:8px;">
            <div style="display:flex; gap:8px;">
                <div style="flex:1;">
                    <label style="font-size:11px; font-weight:bold;">Gosa Txn Filter</label>
                    <select name="filter_type" class="input-field">
                        <option value="ALL" {'selected' if filter_type=='ALL' else ''}>Hunda (All)</option>
                        <option value="DEPOSIT" {'selected' if filter_type=='DEPOSIT' else ''}>Deposit</option>
                        <option value="WITHDRAWAL" {'selected' if filter_type=='WITHDRAWAL' else ''}>Withdrawal</option>
                        <option value="T24_TRANSFER" {'selected' if filter_type=='T24_TRANSFER' else ''}>Transfer</option>
                        <option value="RTGS_TRANSFER" {'selected' if filter_type=='RTGS_TRANSFER' else ''}>RTGS Interbank</option>
                    </select>
                </div>
            </div>
            <div style="display:flex; gap:8px;">
                <div style="flex:1;">
                    <label style="font-size:11px; font-weight:bold;">Guyyaa Jalqabaa</label>
                    <input type="date" name="start_date" value="{start_date}" class="input-field">
                </div>
                <div style="flex:1;">
                    <label style="font-size:11px; font-weight:bold;">Guyyaa Dhumaa</label>
                    <input type="date" name="end_date" value="{end_date}" class="input-field">
                </div>
            </div>
            <button type="submit" class="btn-action btn-purple" style="padding:10px; margin-top:4px;">🔍 Filter Godhi</button>
        </form>
    </div>

    <div class="box" style="padding:0; overflow-x:auto;">
        <table style="width:100%; border-collapse:collapse; text-align:left;">
            <thead>
                <tr style="background:#f8fafc; font-size:11px; color:#64748b; border-bottom:1px solid #e2e8f0;">
                    <th style="padding:8px;">Guyyaa</th>
                    <th style="padding:8px;">Ref</th>
                    <th style="padding:8px;">Maammila</th>
                    <th style="padding:8px;">Type</th>
                    <th style="padding:8px;">Amount</th>
                    <th style="padding:8px;">Commission</th>
                    <th style="padding:8px;">By</th>
                </tr>
            </thead>
            <tbody>
                {rows_html if rows_html else '<tr><td colspan="7" style="padding:16px; text-align:center; color:#64748b;">Transaction-ni argame hin jiru.</td></tr>'}
            </tbody>
        </table>
    </div>
    """
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", content), notifications=NOTIFICATIONS)

@app.route('/manage_users', methods=['GET', 'POST'])
def manage_users():
    if 'role' not in session or session['role'] != 'CEO':
        return "🚫 Hayyama CEO Qofa!", 403

    msg = None
    conn = get_db_connection()
    cursor = conn.cursor()

    if request.method == 'POST':
        action = request.form.get('action')
        uname = request.form.get('username')

        if action == 'add':
            pwd = request.form.get('password').strip()
            urole = request.form.get('role')
            try:
                cursor.execute("INSERT INTO users (username, password, role, status) VALUES (?, ?, ?, 'ACTIVE')", (uname, pwd, urole))
                conn.commit()
                msg = f"✅ Hojjataa/Agent haaraan ({uname} - {urole}) galmaa'eera!"
            except Exception as e:
                msg = f"❌ Error: Username '{uname}' duraan jira!"
        elif action == 'change_role':
            new_role = request.form.get('new_role')
            cursor.execute("UPDATE users SET role = ? WHERE username = ?", (new_role, uname))
            conn.commit()
            msg = f"🔄 Shoorri (Role) Hojjataa '{uname}' gara '{new_role}'itti jijjiirameera!"
        elif action == 'reset_password':
            new_pwd = request.form.get('new_password').strip()
            if new_pwd:
                cursor.execute("UPDATE users SET password = ? WHERE username = ?", (new_pwd, uname))
                conn.commit()
                msg = f"🔑 Password hojjataa '{uname}' milkaa'inaan Reset ta'ee jira!"
        elif action == 'block':
            cursor.execute("UPDATE users SET status = 'BLOCKED' WHERE username = ?", (uname,))
            conn.commit()
            msg = f"🚫 User {uname} Blocked ta'ee jira!"
        elif action == 'unblock':
            cursor.execute("UPDATE users SET status = 'ACTIVE' WHERE username = ?", (uname,))
            conn.commit()
            msg = f"✅ User {uname} Unblocked ta'ee jira!"

    cursor.execute("SELECT username, role, status FROM users")
    users = cursor.fetchall()
    conn.close()

    rows_html = ""
    for u in users:
        if u['username'] == 'ceo':
            st_btn = '<b>Master Admin</b>'
        else:
            st_btn = f'''
            <div style="display:flex; gap:4px; justify-content:flex-end; flex-wrap:wrap;">
                <form method="POST" style="display:inline;">
                    <input type="hidden" name="username" value="{u['username']}">
                    <input type="hidden" name="action" value="change_role">
                    <select name="new_role" style="font-size:10px; padding:3px;" onchange="this.form.submit()">
                        <option value="MAKER" {'selected' if u['role']=='MAKER' else ''}>MAKER</option>
                        <option value="MANAGER" {'selected' if u['role']=='MANAGER' else ''}>MANAGER</option>
                        <option value="AUDITOR" {'selected' if u['role']=='AUDITOR' else ''}>AUDITOR</option>
                        <option value="LOAN_OFFICER" {'selected' if u['role']=='LOAN_OFFICER' else ''}>LOAN_OFFICER</option>
                        <option value="EXTERNAL_AGENT" {'selected' if u['role']=='EXTERNAL_AGENT' else ''}>EXTERNAL_AGENT</option>
                    </select>
                </form>

                <form method="POST" style="display:inline;" onsubmit="return confirm('Password reset gochuu barbaaddaa?')">
                    <input type="hidden" name="username" value="{u['username']}">
                    <input type="hidden" name="action" value="reset_password">
                    <input type="text" name="new_password" placeholder="Pass Haaraa" required style="width:70px; font-size:10px; padding:3px;">
                    <button type="submit" class="btn-action btn-blue" style="padding:3px 6px; font-size:10px;">Reset</button>
                </form>

                <form method="POST" style="display:inline;">
                    <input type="hidden" name="username" value="{u['username']}">
                    <input type="hidden" name="action" value="{"unblock" if u["status"]=="BLOCKED" else "block"}">
                    <button type="submit" class="btn-action {"btn-green" if u["status"]=="BLOCKED" else "btn-red"}" style="padding:3px 6px; font-size:10px;">
                        {"Unblock" if u["status"]=="BLOCKED" else "Block"}
                    </button>
                </form>
            </div>
            '''

        rows_html += f"""
        <tr style="border-bottom:1px solid #e2e8f0; font-size:12px;">
            <td style="padding:8px; font-weight:bold;">{u['username']}</td>
            <td style="padding:8px;"><span class="role-badge">{u['role']}</span></td>
            <td style="padding:8px;">{u['status']}</td>
            <td style="padding:8px; text-align:right;">{st_btn}</td>
        </tr>
        """

    content = f"""
    <div class="box">
        <h2 style="font-size:16px; color:#581c87; margin-bottom:12px;">⚙️ Bulchiinsa Hojjattootaa & External Agents</h2>
        {f"<p style='background:#dcfce7; color:#166534; padding:10px; border-radius:6px; font-size:12px; font-weight:bold; margin-bottom:12px;'>{msg}</p>" if msg else ""}

        <form method="POST" style="margin-bottom:20px;">
            <input type="hidden" name="action" value="add">
            <div class="form-group">
                <label>Username Hojjataa / Agent Haaraa</label>
                <input type="text" name="username" required class="input-field">
            </div>
            <div class="form-group">
                <label>Password</label>
                <input type="password" name="password" required class="input-field">
            </div>
            <div class="form-group">
                <label>Shoora (Role)</label>
                <select name="role" class="input-field" required>
                    <option value="MAKER">MAKER (Galmeessaa / Teller)</option>
                    <option value="MANAGER">MANAGER (Mirkaneessaa)</option>
                    <option value="AUDITOR">AUDITOR (To'ataa)</option>
                    <option value="LOAN_OFFICER">LOAN_OFFICER (Mijjeessaa Liqaa)</option>
                    <option value="EXTERNAL_AGENT">EXTERNAL_AGENT (Baankii Alaa - 10% Profit Comm)</option>
                </select>
            </div>
            <button type="submit" class="btn-submit" style="background:#7c3aed;">➕ Hojjataa / Agent Uumi</button>
        </form>

        <h3 style="font-size:13px; margin-bottom:8px; color:#475569;">📋 Tarree Hojjattootaa & Agents</h3>
        <table style="width:100%; border-collapse:collapse; text-align:left;">
            <thead>
                <tr style="background:#f8fafc; font-size:11px; color:#64748b; border-bottom:1px solid #e2e8f0;">
                    <th style="padding:8px;">Username</th>
                    <th style="padding:8px;">Role</th>
                    <th style="padding:8px;">Status</th>
                    <th style="padding:8px; text-align:right;">Tarkaanfii / Role / Reset</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
        </table>
    </div>
    """
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", content), notifications=NOTIFICATIONS)

@app.route('/change_password', methods=['GET', 'POST'])
def change_password():
    if 'role' not in session:
        return redirect('/login')

    msg = None
    msg_type = "green"

    if request.method == 'POST':
        old_pwd = request.form.get('old_password', '').strip()
        new_pwd = request.form.get('new_password', '').strip()
        confirm_pwd = request.form.get('confirm_password', '').strip()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT password FROM users WHERE username = ?", (session['username'],))
        user = cursor.fetchone()

        if not user or user['password'] != old_pwd:
            msg = "❌ Password duraanii dogoggoraa!"
            msg_type = "red"
        elif new_pwd != confirm_pwd:
            msg = "❌ Password-ni haaraa fi Mirkaneessaan wal hin simne!"
            msg_type = "red"
        elif len(new_pwd) < 4:
            msg = "❌ Password-ni haaraa gabaabaa dha (Minimum 4 characters)!"
            msg_type = "red"
        else:
            cursor.execute("UPDATE users SET password = ? WHERE username = ?", (new_pwd, session['username']))
            conn.commit()
            msg = "✅ Password keessan milkaa'inaan jijjiiramtaniirra!"
            msg_type = "green"

        conn.close()

    content = f"""
    <div class="box">
        <h2 style="font-size: 16px; color:#065f46; margin-bottom: 12px;">🔑 Password Mataa Keetii Jijjiiri</h2>
        {f"<p style='background:{'#dcfce7' if msg_type=='green' else '#fee2e2'}; color:{'#166534' if msg_type=='green' else '#991b1b'}; padding:10px; border-radius:6px; font-size:12px; font-weight:bold; margin-bottom:12px;'>{msg}</p>" if msg else ""}
        <form method="POST">
            <div class="form-group">
                <label>Password Duraanii (Current Password)</label>
                <input type="password" id="old_pwd" name="old_password" required class="input-field">
                <span id="old_pwd_toggle" class="pwd-toggle" onclick="togglePasswordVisibility('old_pwd', 'old_pwd_toggle')">👁️</span>
            </div>
            <div class="form-group">
                <label>Password Haaraa (New Password)</label>
                <input type="password" id="new_pwd" name="new_password" required class="input-field">
                <span id="new_pwd_toggle" class="pwd-toggle" onclick="togglePasswordVisibility('new_pwd', 'new_pwd_toggle')">👁️</span>
            </div>
            <div class="form-group">
                <label>Password Haaraa Mirkaneessi (Confirm Password)</label>
                <input type="password" id="conf_pwd" name="confirm_password" required class="input-field">
                <span id="conf_pwd_toggle" class="pwd-toggle" onclick="togglePasswordVisibility('conf_pwd', 'conf_pwd_toggle')">👁️</span>
            </div>
            <button type="submit" class="btn-submit">💾 Password Jijjiiri</button>
        </form>
    </div>
    """
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", content), notifications=NOTIFICATIONS)

@app.route('/')
def dashboard():
    if 'role' not in session:
        return redirect('/login')
    
    net_cap, deposits, withdraws, cust_bal, total_comm, mud_dep, mud_gross, mud_ceo, mud_cust = get_bank_capital()
    role = session['role']

    maker_btns = ""
    if role == 'MAKER':
        maker_btns = """
        <a href="/register" class="btn-card"><span class="icon">👤</span><span>Galmee Maammilaa</span></a>
        <a href="/transaction" class="btn-card"><span class="icon">💸</span><span>Deposit / Transfer / Withdraw</span></a>
        <a href="/maker_receipts" class="btn-card"><span class="icon">🧾</span><span>Nagahee Maxxansi</span></a>
        """

    agent_btns = ""
    if role == 'EXTERNAL_AGENT':
        agent_btns = """
        <a href="/agent_register" class="btn-card btn-card-ceo"><span class="icon">👤</span><span>External Agent Maammila Uumi</span></a>
        <a href="/agent_transaction" class="btn-card btn-card-ceo"><span class="icon">💸</span><span>External Agent Txn (10% Comm)</span></a>
        """

    manager_btns = ""
    if role == 'MANAGER':
        manager_btns = """
        <a href="/pending" class="btn-card"><span class="icon">🔍</span><span>Manager Approval & Reversals</span></a>
        <a href="/reversals_list" class="btn-card"><span class="icon">🔄</span><span>Reversal Approvals</span></a>
        <a href="/print_receipt_search" class="btn-card"><span class="icon">🖨️</span><span>Barbaadi & Nagahee Maxxansi</span></a>
        """

    auditor_btns = ""
    if role == 'AUDITOR':
        auditor_btns = """
        <a href="/pending" class="btn-card btn-card-auditor"><span class="icon">📋</span><span>View Maammilaa & Approve</span></a>
        <a href="/auditor_reversal_request" class="btn-card btn-card-auditor"><span class="icon">⚠️</span><span>Transaction Reversal Gaafachu</span></a>
        <a href="/print_receipt_search" class="btn-card btn-card-auditor"><span class="icon">🖨️</span><span>Txn Nagahee Maxxansi</span></a>
        """

    loan_btn = ""
    if role in ['LOAN_OFFICER', 'CEO', 'MANAGER']:
        loan_btn = """
        <a href="/islamic_loan" class="btn-card btn-card-mobile"><span class="icon">📜</span><span>Mudaraba & Murabaha Loan</span></a>
        """

    ceo_btn = ""
    ceo_mudaraba_dashboard = ""
    net_capital_html = ""
    
    if role == 'CEO':
        ceo_mudaraba_dashboard = f"""
        <div class="card-ceo-profit">
            <div class="net-title">📊 CEO Private View: Mudaraba 50/50 Profit Share</div>
            <div class="net-amount">{mud_ceo:,.2f} Birr</div>
            <p style="font-size:11px; opacity:0.9; margin-top:4px;">Qoodda Bu'aa Baankii/CEO (50% Share)</p>
            <div class="net-grid">
                <div>📈 Waliigala Kuusaa Mudaraba: <b>{mud_dep:,.2f} Birr</b></div>
                <div>🤝 Qoodda Maammiltootaa (50%): <b>{mud_cust:,.2f} Birr</b></div>
            </div>
        </div>
        """
        net_capital_html = f"""
        <div class="card-net">
            <div class="net-title">Waliigala Kaabitaala Baankii (Net Capital)</div>
            <div class="net-amount">{net_cap:,.2f} Birr</div>
            <div class="net-grid">
                <div>📥 Deposit: <b>{deposits:,.2f} Birr</b></div>
                <div>📤 Withdraw/FT/RTGS: <b>{withdraws:,.2f} Birr</b></div>
            </div>
        </div>
        """
        ceo_btn = """
        <a href="/ceo_commission" class="btn-card btn-card-ceo"><span class="icon">💰</span><span>Comishina Guyyaa (Filtara)</span></a>
        <a href="/reversals_list" class="btn-card btn-card-ceo"><span class="icon">🔄</span><span>CEO Reversal Approval</span></a>
        <a href="/manage_users" class="btn-card btn-card-ceo"><span class="icon">⚙️</span><span>Bulchiinsa Hojjattootaa & Role</span></a>
        """

    content = f"""
    {ceo_mudaraba_dashboard}
    {net_capital_html}

    <h3 style="font-size: 14px; margin-bottom: 12px; color: #475569;">Menu Hojii ({role})</h3>
    <div class="grid-2">
        {maker_btns}
        {agent_btns}
        {manager_btns}
        {auditor_btns}
        {loan_btn}
        <a href="/customers" class="btn-card"><span class="icon">👥</span><span>Listii Maammiltootaa</span></a>
        {ceo_btn}
    </div>
    """
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", content), notifications=NOTIFICATIONS)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'role' not in session or session['role'] not in ['MAKER', 'MANAGER', 'CEO']:
        return "🚫 Hayyama Hin Qabdan!", 403

    msg = None
    if request.method == 'POST':
        full_name = request.form.get('full_name').strip()
        phone = request.form.get('phone').strip()
        gender = request.form.get('gender')
        account_type = request.form.get('account_type')
        enable_mobile = request.form.get('enable_mobile') # Checkbox for Mobile Banking consent
        initial_balance = max(0.0, float(request.form.get('initial_balance', 0.0)))
        photo_file = request.files.get('photo')
        sig_file = request.files.get('signature')

        if photo_file and sig_file and allowed_file(photo_file.filename) and allowed_file(sig_file.filename):
            timestamp_str = int(datetime.datetime.now().timestamp())
            photo_filename = compress_and_save_image(photo_file, f"face_{timestamp_str}_" + secure_filename(photo_file.filename))
            sig_filename = compress_and_save_image(sig_file, f"sig_{timestamp_str}_" + secure_filename(sig_file.filename))

            START_ID = 100099008800
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(CAST(customer_id AS BIGINT)) FROM customers WHERE customer_id >= '100099008800'")
            max_id = cursor.fetchone()[0]
            cust_id = str(START_ID) if max_id is None or max_id < START_ID else str(max_id + 1)
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            mob_status = 'PENDING_APPROVAL' if enable_mobile else 'INACTIVE'

            cursor.execute("""
                INSERT INTO customers (customer_id, full_name, phone, gender, account_type, photo_path, signature_path, balance, status, mobile_pin, mobile_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_APPROVAL', '1234', ?, ?)
            """, (cust_id, full_name, phone, gender, account_type, photo_filename, sig_filename, initial_balance, mob_status, now))

            if initial_balance > 0:
                ft_ref = f"FT{datetime.datetime.now().strftime('%y%j')}{random.randint(10000, 99999)}"
                cursor.execute("""
                    INSERT INTO transactions (txn_id, txn_type, customer_id, customer_name, amount, commission, bank_name, ft_reference, status, created_by, timestamp)
                    VALUES (?, 'DEPOSIT', ?, ?, ?, 0.0, 'Imana Microfinance', ?, 'PENDING_MANAGER', ?, ?)
                """, (f"TXN-{timestamp_str}", cust_id, full_name, initial_balance, ft_ref, session['username'], now))

            conn.commit()
            conn.close()
            msg = f"✅ Maammilli {full_name} galmaa'eera! (Acc ID: {cust_id}, Mobile Status: {mob_status})"
            add_notification(f"Maammilli haaraan ({full_name}) galmaa'ee manager approval eegaa jira.")

    content = f"""
    <div class="box">
        <h2 style="font-size: 16px; margin-bottom: 12px; color:#065f46;">👤 Galmee Maammila Haaraa & Mobile Banking Request</h2>
        {f"<p style='background:#dcfce7; color:#166534; padding:10px; border-radius:6px; font-size:12px; font-weight:bold; margin-bottom:12px;'>{msg}</p>" if msg else ""}
        <form method="POST" enctype="multipart/form-data">
            <div class="form-group">
                <label>Maqaa Guutuu Maammilaa</label>
                <input type="text" name="full_name" placeholder="Fkn: Chala Beyene" required class="input-field">
            </div>
            <div class="form-group">
                <label>Lakkoofsa Bilbilaa</label>
                <input type="text" name="phone" placeholder="0911223344" required class="input-field">
            </div>
            <div class="form-group">
                <label>Saala</label>
                <select name="gender" class="input-field" required>
                    <option value="Dhiira">Dhiira</option>
                    <option value="Dubartii">Dubartii</option>
                </select>
            </div>
            <div class="form-group">
                <label>Gosa Akkaawuntii Islaamaa</label>
                <select name="account_type" class="input-field" required>
                    <option value="WADIA">Wadia Savings (Kuusaa Nagummaa)</option>
                    <option value="MUDARABA">Mudaraba Investment (Kuusaa Bu'aa 50/50)</option>
                </select>
            </div>
            <div class="form-group" style="background:#eff6ff; padding:12px; border-radius:8px; border:1px solid #bfdbfe;">
                <label style="color:#1e3a8a; display:flex; align-items:center; gap:8px; cursor:pointer;">
                    <input type="checkbox" name="enable_mobile" value="1" checked style="width:18px; height:18px;">
                    <span>📲 Tajaajila Mobile Banking Maammilaaf Banuuf Gaaffii Dhiyeessi (Fedhii Maammilaan)</span>
                </label>
                <p style="font-size:11px; color:#64748b; margin-top:4px;">PIN Yeroo: <b>1234</b> (Manager approval booda maammilli login gochuu danda'a)</p>
            </div>
            <div class="form-group">
                <label>Hamma Kusaa Jalqabaa (Birr)</label>
                <input type="number" step="0.01" min="0" name="initial_balance" value="0.00" class="input-field" required>
            </div>
            <div class="form-group">
                <label>Suuraa Maammilaa (Photo)</label>
                <input type="file" name="photo" accept="image/*" required class="input-field">
            </div>
            <div class="form-group">
                <label>Mallattoo Maammilaa (Signature)</label>
                <input type="file" name="signature" accept="image/*" required class="input-field">
            </div>
            <button type="submit" class="btn-submit">💾 Galmeessi (Register & Request Mobile)</button>
        </form>
    </div>
    """
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", content), notifications=NOTIFICATIONS)

@app.route('/enable_mobile_service/<cust_id>')
def enable_mobile_service(cust_id):
    if 'role' not in session or session['role'] not in ['MAKER', 'MANAGER', 'CEO']:
        return "🚫 Hayyama Hin Qabdan!", 403

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE customers SET mobile_status = 'PENDING_APPROVAL' WHERE customer_id = ?", (cust_id,))
    conn.commit()
    conn.close()

    add_notification(f"Maker ({session['username']}) maammila (Acc: {cust_id})'iif Mobile Banking gaafateera. Manager approval eegaa jira.")
    return redirect('/customers')

@app.route('/approve_mobile_banking/<cust_id>')
def approve_mobile_banking(cust_id):
    if 'role' not in session or session['role'] not in ['MANAGER', 'CEO']:
        return "🚫 Hayyama Manager Qofa!", 403

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE customers SET mobile_status = 'ACTIVE' WHERE customer_id = ?", (cust_id,))
    conn.commit()
    conn.close()

    add_notification(f"✅ Manager ({session['username']}) Mobile Banking maammila (Acc: {cust_id}) mirkaneessee jira.")
    return redirect('/pending')

@app.route('/reject_mobile_banking/<cust_id>')
def reject_mobile_banking(cust_id):
    if 'role' not in session or session['role'] not in ['MANAGER', 'CEO']:
        return "🚫 Hayyama Manager Qofa!", 403

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE customers SET mobile_status = 'INACTIVE' WHERE customer_id = ?", (cust_id,))
    conn.commit()
    conn.close()

    add_notification(f"❌ Manager ({session['username']}) Mobile Banking maammila (Acc: {cust_id}) kuffisee jira.")
    return redirect('/pending')

@app.route('/pending')
def pending():
    if 'role' not in session or session['role'] not in ['MANAGER', 'AUDITOR', 'CEO']:
        return "🚫 Hayyama Hin Qabdan!", 403

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM customers WHERE status = 'PENDING_APPROVAL' ORDER BY created_at DESC")
    pending_custs = cursor.fetchall()

    cursor.execute("SELECT * FROM customers WHERE mobile_status = 'PENDING_APPROVAL' AND status = 'ACTIVE' ORDER BY created_at DESC")
    pending_mobiles = cursor.fetchall()

    cursor.execute("SELECT * FROM transactions WHERE status = 'PENDING_MANAGER' ORDER BY timestamp DESC")
    pending_txns = cursor.fetchall()
    conn.close()

    user_role = session.get('role')

    cust_html = ""
    for c in pending_custs:
        manager_cust_btns = ""
        if user_role == 'MANAGER':
            manager_cust_btns = f"""
            <div style="margin-top:8px;">
                <a href="/approve_customer/{c['customer_id']}" class="btn-action btn-green">✅ Mirkaneessi (Approve)</a>
                <a href="/reject_customer/{c['customer_id']}" class="btn-action btn-red" onclick="return confirm('Sassaabuu barbaaddaa?')">❌ Kuffisi (Reject)</a>
            </div>
            """

        cust_html += f"""
        <div class="item-card">
            <div style="font-size:13px; font-weight:bold;">{c['full_name']} (Acc: {c['customer_id']})</div>
            <div style="font-size:11px; color:#64748b;">Phone: {c['phone']} | Acc Type: {c['account_type']} | Balance: {c['balance']:,.2f} Birr</div>
            <div style="font-size:11px; color:#1d4ed8; font-weight:bold; margin-top:2px;">📲 Mobile Request Status: {c['mobile_status']}</div>
            <div class="img-grid">
                <a href="/uploads/{c['photo_path']}" target="_blank"><img src="/uploads/{c['photo_path']}" alt="Face" style="height:60px; border-radius:6px; margin-top:6px; margin-right:6px;"></a>
                <a href="/uploads/{c['signature_path']}" target="_blank"><img src="/uploads/{c['signature_path']}" alt="Signature" style="height:60px; border-radius:6px; margin-top:6px;"></a>
            </div>
            {manager_cust_btns}
        </div>
        """

    mobile_html = ""
    for m in pending_mobiles:
        manager_mobile_btns = ""
        if user_role in ['MANAGER', 'CEO']:
            manager_mobile_btns = f"""
            <div style="margin-top:8px;">
                <a href="/approve_mobile_banking/{m['customer_id']}" class="btn-action btn-green">📲 Approve Mobile Banking</a>
                <a href="/reject_mobile_banking/{m['customer_id']}" class="btn-action btn-red">❌ Reject Request</a>
            </div>
            """

        mobile_html += f"""
        <div class="item-card" style="border-left: 4px solid #2563eb;">
            <div style="font-size:13px; font-weight:bold; color:#1e3a8a;">📱 Mobile Banking Request: {m['full_name']} (Acc: {m['customer_id']})</div>
            <div style="font-size:11px; color:#475569;">Lakk Bilbilaa: <b>{m['phone']}</b> | Gosa Acc: {m['account_type']}</div>
            <div style="font-size:11px; color:#16a34a; font-weight:bold; margin-top:2px;">PIN Yeroo: {m['mobile_pin']}</div>
            {manager_mobile_btns}
        </div>
        """

    txn_html = ""
    for t in pending_txns:
        manager_txn_btns = ""
        if user_role == 'MANAGER':
            manager_txn_btns = f"""
            <div style="margin-top:8px;">
                <a href="/approve_transaction/{t['txn_id']}" class="btn-action btn-green">✅ Approve Txn</a>
                <a href="/reject_transaction/{t['txn_id']}" class="btn-action btn-red" onclick="return confirm('Kuffisuu barbaaddaa?')">❌ Reject Txn</a>
            </div>
            """

        txn_html += f"""
        <div class="item-card">
            <div style="font-size:13px; font-weight:bold; color:#047857;">{t['txn_type']} - {t['amount']:,.2f} Birr</div>
            <div style="font-size:11px; color:#475569;">Maammila: <b>{t['customer_name']}</b> (Acc: {t['customer_id']})</div>
            <div style="font-size:11px; color:#64748b;">Ref: {t['ft_reference']} | Comm: {t['commission']:,.2f} Birr | Bank: {t['bank_name']}</div>
            <div style="font-size:10px; color:#94a3b8;">Remarks: {t['reason']} | By: {t['created_by']}</div>
            {manager_txn_btns}
        </div>
        """

    content = f"""
    <div class="box">
        <h2 style="font-size:16px; color:#065f46; margin-bottom:12px;">📋 Approvals Dhiyaatan ({session['role']})</h2>
        
        <h3 style="font-size:13px; color:#1e3a8a; margin-bottom:8px;">📲 Gaaffii Mobile Banking Pending ({len(pending_mobiles)})</h3>
        {mobile_html if pending_mobiles else '<p style="font-size:12px; color:#64748b; margin-bottom:16px;">Gaaffiin Mobile Banking eegaa jiru hin jiru.</p>'}

        <h3 style="font-size:13px; color:#334155; margin-bottom:8px;">👥 Maammiltoota Pending ({len(pending_custs)})</h3>
        {cust_html if pending_custs else '<p style="font-size:12px; color:#64748b; margin-bottom:16px;">Maammilli eegaa jiru hin jiru.</p>'}

        <h3 style="font-size:13px; color:#334155; margin-top:20px; margin-bottom:8px;">💸 Transactions Pending ({len(pending_txns)})</h3>
        {txn_html if pending_txns else '<p style="font-size:12px; color:#64748b;">Transaction-ni eegaa jiru hin jiru.</p>'}
    </div>
    """
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", content), notifications=NOTIFICATIONS)

@app.route('/customers')
def customers_list():
    if 'role' not in session and 'mobile_cust_id' not in session:
        return redirect('/login')

    search = request.args.get('search', '').strip()
    conn = get_db_connection()
    cursor = conn.cursor()

    if search:
        cursor.execute("SELECT * FROM customers WHERE customer_id LIKE ? OR full_name LIKE ? OR phone LIKE ? ORDER BY created_at DESC", (f"%{search}%", f"%{search}%", f"%{search}%"))
    else:
        cursor.execute("SELECT * FROM customers ORDER BY created_at DESC LIMIT 50")

    custs = cursor.fetchall()
    conn.close()

    user_role = session.get('role')

    rows_html = ""
    for c in custs:
        reset_btn = ""
        mobile_btn = ""

        if user_role in ['MANAGER', 'MAKER', 'CEO']:
            if c['mobile_status'] == 'INACTIVE':
                mobile_btn = f'''<a href="/enable_mobile_service/{c['customer_id']}" class="btn-action btn-blue" style="padding:4px 8px; font-size:11px; text-decoration:none;">📲 Request Mobile</a>'''
            elif c['mobile_status'] == 'PENDING_APPROVAL':
                mobile_btn = f'''<span class="badge badge-pending">Mobile Pending</span>'''
            else:
                mobile_btn = f'''<span class="badge badge-active">Mobile Active</span>'''

            reset_btn = f'''
            <form method="POST" action="/reset_customer_pin/{c['customer_id']}" style="display:inline;" onsubmit="return confirm('PIN maammila kanaa gara default (1234)tti reset gochuu ni feetaa?')">
                <button type="submit" class="btn-action btn-purple" style="padding:4px 8px; font-size:11px;">🔑 Reset PIN</button>
            </form>
            '''

        rows_html += f"""
        <tr style="border-bottom:1px solid #e2e8f0; font-size:12px;">
            <td style="padding:8px; font-weight:bold;">{c['customer_id']}</td>
            <td style="padding:8px;">{c['full_name']}</td>
            <td style="padding:8px;">{c['phone']}</td>
            <td style="padding:8px;">{c['account_type']}</td>
            <td style="padding:8px; font-weight:bold; color:#047857;">{c['balance']:,.2f} Birr</td>
            <td style="padding:8px;"><span class="badge {'badge-active' if c['status']=='ACTIVE' else 'badge-pending'}">{c['status']}</span></td>
            <td style="padding:8px; text-align:right;">{mobile_btn} {reset_btn}</td>
        </tr>
        """

    content = f"""
    <div class="box">
        <h2 style="font-size:16px; color:#065f46; margin-bottom:12px;">👥 Listii Maammiltootaa & Bulchiinsa Mobile PIN</h2>
        <form method="GET" style="margin-bottom:16px; display:flex; gap:8px;">
            <input type="text" name="search" value="{search}" placeholder="Maqaa, Acc No, Phone..." class="input-field" style="flex:1;">
            <button type="submit" class="btn-action btn-blue">🔍 Barbaadi</button>
        </form>

        <div style="overflow-x:auto;">
            <table style="width:100%; border-collapse:collapse; text-align:left;">
                <thead>
                    <tr style="background:#f8fafc; font-size:11px; color:#64748b; border-bottom:1px solid #e2e8f0;">
                        <th style="padding:8px;">Acc No</th>
                        <th style="padding:8px;">Maqaa</th>
                        <th style="padding:8px;">Phone</th>
                        <th style="padding:8px;">Type</th>
                        <th style="padding:8px;">Balance</th>
                        <th style="padding:8px;">Status</th>
                        <th style="padding:8px; text-align:right;">Tarkaanfii (Mobile & Reset PIN)</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html if rows_html else '<tr><td colspan="7" style="padding:16px; text-align:center;">Maammilli argame hin jiru.</td></tr>'}
                </tbody>
            </table>
        </div>
    </div>
    """
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", content), notifications=NOTIFICATIONS)

@app.route('/reset_customer_pin/<cust_id>', methods=['POST', 'GET'])
def reset_customer_pin(cust_id):
    if 'role' not in session or session['role'] not in ['MANAGER', 'MAKER', 'CEO']:
        return "🚫 Hayyama Hin Qabdan!", 403

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE customers SET mobile_pin = '1234' WHERE customer_id = ?", (cust_id,))
    conn.commit()
    conn.close()

    add_notification(f"🔑 PIN Mobile Banking maammilaa (Acc: {cust_id}) gara '1234'tti Reset ta'ee jira ({session['username']}).")
    return redirect('/customers')

@app.route('/approve_customer/<cust_id>')
def approve_customer(cust_id):
    if 'role' not in session or session['role'] not in ['MANAGER', 'CEO']:
        return "🚫 Hayyama Manager Qofa!", 403

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE customers SET status = 'ACTIVE' WHERE customer_id = ?", (cust_id,))
    conn.commit()
    conn.close()

    add_notification(f"✅ Maammilli ({cust_id}) Manager-aan mirkanaa'eera.")
    return redirect('/pending')

@app.route('/reject_customer/<cust_id>')
def reject_customer(cust_id):
    if 'role' not in session or session['role'] not in ['MANAGER', 'CEO']:
        return "🚫 Hayyama Manager Qofa!", 403

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE customers SET status = 'REJECTED' WHERE customer_id = ?", (cust_id,))
    conn.commit()
    conn.close()

    add_notification(f"❌ Maammilli ({cust_id}) Manager-aan kuffifameera.")
    return redirect('/pending')

@app.route('/approve_transaction/<txn_id>')
def approve_transaction(txn_id):
    if 'role' not in session or session['role'] not in ['MANAGER', 'CEO']:
        return "🚫 Hayyama Manager Qofa!", 403

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM transactions WHERE txn_id = ?", (txn_id,))
    txn = cursor.fetchone()

    if txn and txn['status'] == 'PENDING_MANAGER':
        total_deduction = txn['amount'] + txn['commission']

        if txn['txn_type'] in ['DEPOSIT', 'MOBILE_TOPUP_REC']:
            cursor.execute("UPDATE customers SET balance = balance + ? WHERE customer_id = ?", (txn['amount'], txn['customer_id']))
        elif txn['txn_type'] in ['WITHDRAWAL', 'T24_TRANSFER', 'RTGS_TRANSFER', 'WALLET_TOPUP']:
            cursor.execute("UPDATE customers SET balance = balance - ? WHERE customer_id = ?", (total_deduction, txn['customer_id']))
            if txn['target_account'] and txn['txn_type'] == 'T24_TRANSFER':
                cursor.execute("UPDATE customers SET balance = balance + ? WHERE customer_id = ?", (txn['amount'], txn['target_account']))

        cursor.execute("UPDATE transactions SET status = 'APPROVED' WHERE txn_id = ?", (txn_id,))
        conn.commit()
        add_notification(f"✅ Transaction ({txn_id}) Manager-aan mirkanaa'eera.")

    conn.close()
    return redirect('/pending')

@app.route('/reject_transaction/<txn_id>')
def reject_transaction(txn_id):
    if 'role' not in session or session['role'] not in ['MANAGER', 'CEO']:
        return "🚫 Hayyama Manager Qofa!", 403

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE transactions SET status = 'REJECTED' WHERE txn_id = ?", (txn_id,))
    conn.commit()
    conn.close()

    add_notification(f"❌ Transaction ({txn_id}) Manager-aan kuffifameera.")
    return redirect('/pending')

@app.route('/transaction', methods=['GET', 'POST'])
def transaction():
    if 'role' not in session or session['role'] not in ['MAKER', 'MANAGER', 'CEO']:
        return "🚫 Hayyama Hin Qabdan!", 403

    msg = None
    if request.method == 'POST':
        cust_id = request.form.get('customer_id', '').strip()
        txn_type = request.form.get('txn_type')
        amount = float(request.form.get('amount', 0.0))
        target_account = request.form.get('target_account', '').strip()
        reason = request.form.get('reason', '').strip()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT customer_id, full_name, balance, status, freeze_status FROM customers WHERE customer_id = ?", (cust_id,))
        cust = cursor.fetchone()

        if not cust or cust['status'] != 'ACTIVE':
            msg = "❌ Akkaawunttiin maammilaa ACTIVE miti ykn hin jiru!"
        elif cust['freeze_status'] == 'FROZEN':
            msg = "🚫 Akkaawunttiin maammila kanaa uggurameera!"
        elif amount <= 0:
            msg = "❌ Hammi maallaqaa ziiroo ol ta'uu qaba!"
        else:
            comm = get_commission(amount) if txn_type in ['WITHDRAWAL', 'T24_TRANSFER'] else 0.0
            ft_ref = f"FT{datetime.datetime.now().strftime('%y%j')}{random.randint(10000, 99999)}"
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            txn_id = f"TXN-{int(time.time())}-{random.randint(100,999)}"

            if txn_type in ['WITHDRAWAL', 'T24_TRANSFER'] and cust['balance'] < (amount + comm):
                msg = f"❌ Haafeeka gahaa hin qabdu! Balance: {cust['balance']:,.2f} Birr, Required: {amount + comm:,.2f} Birr"
            else:
                cursor.execute("""
                    INSERT INTO transactions (txn_id, txn_type, customer_id, customer_name, target_account, amount, commission, bank_name, ft_reference, status, created_by, timestamp, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'Imana Microfinance', ?, 'PENDING_MANAGER', ?, ?, ?)
                """, (txn_id, txn_type, cust_id, cust['full_name'], target_account, amount, comm, ft_ref, session['username'], now, reason))
                conn.commit()
                msg = f"✅ Transaction {txn_type} ({amount:,.2f} Birr) galmaa'ee Manager Approval eegaa jira! Ref: {ft_ref}"
                add_notification(f"Txn Haaraan ({txn_type} - {amount:,.2f} Birr) galmaa'eera.")
        conn.close()

    content = f"""
    <div class="box">
        <h2 style="font-size:16px; color:#065f46; margin-bottom:12px;">💸 Kaffaltii & Sochii Maallaqaa (Teller Txn)</h2>
        {f"<p style='background:#dcfce7; color:#166534; padding:10px; border-radius:6px; font-size:12px; font-weight:bold; margin-bottom:12px;'>{msg}</p>" if msg else ""}

        <form method="POST">
            <div class="form-group">
                <label>Lakkoofsa Akkaawuntii Maammilaa</label>
                <input type="text" name="customer_id" placeholder="Fkn: 100099008800" class="input-field" required>
            </div>
            <div class="form-group">
                <label>Gosa Transaction</label>
                <select name="txn_type" class="input-field" required>
                    <option value="DEPOSIT">DEPOSIT (Maallaqa Galchuu)</option>
                    <option value="WITHDRAWAL">WITHDRAWAL (Maallaqa Baasuu)</option>
                    <option value="T24_TRANSFER">T24_TRANSFER (Dabarsuu)</option>
                </select>
            </div>
            <div class="form-group">
                <label>Akkaawuntii Simataa (T24_TRANSFER qofaaf)</label>
                <input type="text" name="target_account" placeholder="Fkn: 100099008801" class="input-field">
            </div>
            <div class="form-group">
                <label>Hamma Maallaqaa (Birr)</label>
                <input type="number" step="0.01" min="1" name="amount" placeholder="0.00" class="input-field" required>
            </div>
            <div class="form-group">
                <label>Sababa / Remark</label>
                <input type="text" name="reason" placeholder="Kaffaltii..." class="input-field">
            </div>
            <button type="submit" class="btn-submit">💾 Transaction Galmeessi</button>
        </form>
    </div>
    """
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", content), notifications=NOTIFICATIONS)

@app.route('/maker_receipts')
@app.route('/print_receipt_search')
def receipt_search():
    if 'role' not in session:
        return redirect('/login')

    search = request.args.get('search', '').strip()
    conn = get_db_connection()
    cursor = conn.cursor()

    if search:
        cursor.execute("SELECT * FROM transactions WHERE ft_reference LIKE ? OR customer_id LIKE ? OR customer_name LIKE ? ORDER BY timestamp DESC", (f"%{search}%", f"%{search}%", f"%{search}%"))
    else:
        cursor.execute("SELECT * FROM transactions ORDER BY timestamp DESC LIMIT 20")

    txns = cursor.fetchall()
    conn.close()

    rows_html = ""
    for t in txns:
        rows_html += f"""
        <tr style="border-bottom:1px solid #e2e8f0; font-size:12px;">
            <td style="padding:8px; font-weight:bold;">{t['ft_reference']}</td>
            <td style="padding:8px;">{t['customer_name']} ({t['customer_id']})</td>
            <td style="padding:8px;">{t['txn_type']}</td>
            <td style="padding:8px; font-weight:bold; color:#047857;">{t['amount']:,.2f} Birr</td>
            <td style="padding:8px;"><span class="badge badge-active">{t['status']}</span></td>
            <td style="padding:8px; text-align:right;">
                <a href="/print_receipt/{t['txn_id']}" target="_blank" class="btn-action btn-purple" style="font-size:10px; padding:4px 8px;">🖨️ Maxxansi</a>
            </td>
        </tr>
        """

    content = f"""
    <div class="box">
        <h2 style="font-size:16px; color:#065f46; margin-bottom:12px;">🧾 Nagahee Maxxansuu & Barbaacha Transaction</h2>
        <form method="GET" style="margin-bottom:16px; display:flex; gap:8px;">
            <input type="text" name="search" value="{search}" placeholder="FT Reference, Acc No, Maqaa..." class="input-field" style="flex:1;">
            <button type="submit" class="btn-action btn-blue">🔍 Barbaadi</button>
        </form>

        <div style="overflow-x:auto;">
            <table style="width:100%; border-collapse:collapse; text-align:left;">
                <thead>
                    <tr style="background:#f8fafc; font-size:11px; color:#64748b; border-bottom:1px solid #e2e8f0;">
                        <th style="padding:8px;">FT Ref</th>
                        <th style="padding:8px;">Maammila</th>
                        <th style="padding:8px;">Gosa</th>
                        <th style="padding:8px;">Amount</th>
                        <th style="padding:8px;">Status</th>
                        <th style="padding:8px; text-align:right;">Maxxansi</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html if rows_html else '<tr><td colspan="6" style="padding:16px; text-align:center;">Transaction-ni argame hin jiru.</td></tr>'}
                </tbody>
            </table>
        </div>
    </div>
    """
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", content), notifications=NOTIFICATIONS)

@app.route('/print_receipt/<txn_id>')
def print_receipt(txn_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM transactions WHERE txn_id = ?", (txn_id,))
    txn = cursor.fetchone()
    conn.close()

    if not txn:
        return "Transaction Hin Argamne!", 404

    receipt_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Nagahee Transaction - {txn['ft_reference']}</title>
        <style>
            body {{ font-family: Arial, sans-serif; padding: 20px; max-width: 450px; margin: 0 auto; border: 1px solid #cbd5e1; border-radius: 8px; }}
            .header {{ text-align: center; border-bottom: 2px dashed #065f46; padding-bottom: 10px; margin-bottom: 15px; }}
            .row {{ display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 8px; }}
            .bold {{ font-weight: bold; }}
            .footer {{ text-align: center; margin-top: 20px; font-size: 11px; color: #64748b; border-top: 1px solid #e2e8f0; padding-top: 10px; }}
            @media print {{ .no-print {{ display: none; }} }}
        </style>
    </head>
    <body>
        <div class="header">
            <h2 style="color:#065f46; margin:0; font-size:18px;">Imana Free Interest Microfinance</h2>
            <p style="margin:2px 0; font-size:12px;">Nagahee Kaffaltii & Sochii Maallaqaa</p>
        </div>
        <div class="row"><span class="bold">FT Reference:</span> <span>{txn['ft_reference']}</span></div>
        <div class="row"><span class="bold">Guyyaa:</span> <span>{txn['timestamp']}</span></div>
        <div class="row"><span class="bold">Maammila:</span> <span>{txn['customer_name']}</span></div>
        <div class="row"><span class="bold">Acc No:</span> <span>{txn['customer_id']}</span></div>
        <div class="row"><span class="bold">Gosa Transaction:</span> <span>{txn['txn_type']}</span></div>
        <div class="row"><span class="bold">Hamma Maallaqaa:</span> <span class="bold" style="font-size:15px; color:#047857;">{txn['amount']:,.2f} Birr</span></div>
        <div class="row"><span class="bold">Comishina:</span> <span>{txn['commission']:,.2f} Birr</span></div>
        <div class="row"><span class="bold">Status:</span> <span>{txn['status']}</span></div>
        <div class="row"><span class="bold">Hojjataa:</span> <span>{txn['created_by']}</span></div>

        <div class="footer">
            <p>Galatoomaa! Tajaajila Islaamaa Dhugaa.</p>
            <button onclick="window.print()" class="no-print" style="padding:8px 16px; background:#047857; color:white; border:none; border-radius:4px; font-weight:bold; cursor:pointer; margin-top:10px;">🖨️ Maxxansi / Print</button>
        </div>
    </body>
    </html>
    """
    return receipt_html

@app.route('/reversals_list')
def reversals_list():
    if 'role' not in session or session['role'] not in ['MANAGER', 'CEO']:
        return "🚫 Hayyama Hin Qabdan!", 403

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM reversals ORDER BY timestamp DESC")
    revs = cursor.fetchall()
    conn.close()

    rows_html = ""
    for r in revs:
        rows_html += f"""
        <tr style="border-bottom:1px solid #e2e8f0; font-size:12px;">
            <td style="padding:8px; font-weight:bold;">{r['reversal_id']}</td>
            <td style="padding:8px;">{r['txn_id']}</td>
            <td style="padding:8px;">{r['reason']}</td>
            <td style="padding:8px;">{r['requested_by']}</td>
            <td style="padding:8px;"><span class="badge badge-pending">{r['status']}</span></td>
        </tr>
        """

    content = f"""
    <div class="box">
        <h2 style="font-size:16px; color:#581c87; margin-bottom:12px;">🔄 Reversals Tarree Approval</h2>
        <div style="overflow-x:auto;">
            <table style="width:100%; border-collapse:collapse; text-align:left;">
                <thead>
                    <tr style="background:#f8fafc; font-size:11px; color:#64748b; border-bottom:1px solid #e2e8f0;">
                        <th style="padding:8px;">Reversal ID</th>
                        <th style="padding:8px;">Txn ID</th>
                        <th style="padding:8px;">Sababa</th>
                        <th style="padding:8px;">Gaafataa</th>
                        <th style="padding:8px;">Status</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html if rows_html else '<tr><td colspan="5" style="padding:16px; text-align:center;">Gaaffiin Reversal hin jiru.</td></tr>'}
                </tbody>
            </table>
        </div>
    </div>
    """
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", content), notifications=NOTIFICATIONS)

@app.route('/auditor_reversal_request', methods=['GET', 'POST'])
def auditor_reversal_request():
    if 'role' not in session or session['role'] not in ['AUDITOR', 'MANAGER', 'CEO']:
        return "🚫 Hayyama Hin Qabdan!", 403

    msg = None
    if request.method == 'POST':
        txn_id = request.form.get('txn_id', '').strip()
        reason = request.form.get('reason', '').strip()

        conn = get_db_connection()
        cursor = conn.cursor()
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rev_id = f"REV-{random.randint(10000,99999)}"

        try:
            cursor.execute("""
                INSERT INTO reversals (reversal_id, txn_id, reason, requested_by, status, timestamp)
                VALUES (?, ?, ?, ?, 'PENDING_APPROVAL', ?)
            """, (rev_id, txn_id, reason, session['username'], now))
            conn.commit()
            msg = f"✅ Gaaffiin Reversal ({rev_id}) Manager/CEO eegaa jira!"
            add_notification(f"Auditor ({session['username']}) reversal gaafateera: Txn {txn_id}.")
        except Exception as e:
            msg = f"❌ Error: Reversal transaction kanaaf duraan dhiyaateera!"

        conn.close()

    content = f"""
    <div class="box">
        <h2 style="font-size:16px; color:#c2410c; margin-bottom:12px;">⚠️ Gaaffii Reversal Transaction Dhiyeessi</h2>
        {f"<p style='background:#dcfce7; color:#166534; padding:10px; border-radius:6px; font-size:12px; font-weight:bold; margin-bottom:12px;'>{msg}</p>" if msg else ""}
        <form method="POST">
            <div class="form-group">
                <label>Txn ID (Fkn: TXN-...)</label>
                <input type="text" name="txn_id" required class="input-field">
            </div>
            <div class="form-group">
                <label>Sababa Reversal (Reason)</label>
                <input type="text" name="reason" placeholder="Dogoggora kaffaltii..." required class="input-field">
            </div>
            <button type="submit" class="btn-submit" style="background:#c2410c;">⚠️ Gaaffii Reversal Ergi</button>
        </form>
    </div>
    """
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", content), notifications=NOTIFICATIONS)

@app.route('/islamic_loan', methods=['GET', 'POST'])
def islamic_loan():
    if 'role' not in session or session['role'] not in ['LOAN_OFFICER', 'MANAGER', 'CEO']:
        return "🚫 Hayyama Hin Qabdan!", 403

    msg = None
    if request.method == 'POST':
        cust_id = request.form.get('customer_id', '').strip()
        fin_type = request.form.get('financing_type') # MUDARABA, MURABAHA
        principal = float(request.form.get('principal_amount', 0.0))
        margin = float(request.form.get('profit_margin', 0.0))
        tenure = int(request.form.get('tenure_months', 12))

        total_repay = principal + margin
        monthly = total_repay / tenure if tenure > 0 else total_repay
        loan_id = f"LOAN-{random.randint(10000, 99999)}"
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT full_name FROM customers WHERE customer_id = ?", (cust_id,))
        cust = cursor.fetchone()

        if cust:
            cursor.execute("""
                INSERT INTO islamic_financing (loan_id, customer_id, customer_name, financing_type, principal_amount, profit_margin, total_repayment, tenure_months, monthly_installment, status, created_by, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_MANAGER', ?, ?)
            """, (loan_id, cust_id, cust['full_name'], fin_type, principal, margin, total_repay, tenure, monthly, session['username'], now))
            conn.commit()
            msg = f"✅ Gaaffiin Liqaa Islaamaa ({fin_type} - {total_repay:,.2f} Birr) galmaa'eera!"
            add_notification(f"Gaaffii Liqaa Islaamaa haaraan ({cust['full_name']}) dhiyaateera.")
        else:
            msg = "❌ Customer ID hin argamne!"

        conn.close()

    content = f"""
    <div class="box">
        <h2 style="font-size:16px; color:#1d4ed8; margin-bottom:12px;">📜 Mudaraba & Murabaha Financing (Liqaa Islaamaa)</h2>
        {f"<p style='background:#dcfce7; color:#166534; padding:10px; border-radius:6px; font-size:12px; font-weight:bold; margin-bottom:12px;'>{msg}</p>" if msg else ""}

        <form method="POST">
            <div class="form-group">
                <label>Lakkoofsa Akkaawuntii Maammilaa</label>
                <input type="text" name="customer_id" placeholder="100099008800" required class="input-field">
            </div>
            <div class="form-group">
                <label>Gosa Liqaa Islaamaa</label>
                <select name="financing_type" class="input-field" required>
                    <option value="MURABAHA">MURABAHA (Gurgurtaa Bu'aa Idamaa)</option>
                    <option value="MUDARABA">MUDARABA (Tumsa Investment)</option>
                </select>
            </div>
            <div class="form-group">
                <label>Hamma Liqaa Principal (Birr)</label>
                <input type="number" step="0.01" min="100" name="principal_amount" required class="input-field">
            </div>
            <div class="form-group">
                <label>Bu'aa Baankii / Profit Margin (Birr)</label>
                <input type="number" step="0.01" min="0" name="profit_margin" value="0.00" class="input-field">
            </div>
            <div class="form-group">
                <label>Ji'oota Kaffaltii (Tenure Months)</label>
                <input type="number" min="1" max="120" name="tenure_months" value="12" required class="input-field">
            </div>
            <button type="submit" class="btn-submit" style="background:#1d4ed8;">💾 Gaaffii Liqaa Galmeessi</button>
        </form>
    </div>
    """
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", content), notifications=NOTIFICATIONS)

@app.route('/agent_register', methods=['GET', 'POST'])
def agent_register():
    if 'role' not in session or session['role'] not in ['EXTERNAL_AGENT', 'CEO']:
        return "🚫 Hayyama External Agent Qofa!", 403
    return register()

@app.route('/agent_transaction', methods=['GET', 'POST'])
def agent_transaction():
    if 'role' not in session or session['role'] not in ['EXTERNAL_AGENT', 'CEO']:
        return "🚫 Hayyama External Agent Qofa!", 403
    return transaction()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
