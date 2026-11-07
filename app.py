import os
import sqlite3
import datetime
import random
import psycopg2
from psycopg2.extras import DictCursor

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from flask import (
    Flask, request, redirect, url_for, session, 
    render_template_string, send_from_directory, jsonify
)
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "imana_free_interest_microfinance_secret_key_2026")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'pdf'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
NOTIFICATIONS = []

def get_db_connection():
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    return psycopg2.connect(database_url, cursor_factory=DictCursor)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def add_notification(message):
    now = datetime.datetime.now().strftime("%H:%M:%S")
    NOTIFICATIONS.insert(0, f"[{now}] {message}")
    if len(NOTIFICATIONS) > 20:
        NOTIFICATIONS.pop()

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
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
            ('maker1', 'maker123', 'MAKER', 'ACTIVE')
        ]
        cursor.executemany("INSERT INTO users VALUES (%s, %s, %s, %s)", default_users)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            customer_id TEXT PRIMARY KEY,
            full_name TEXT,
            phone TEXT,
            gender TEXT DEFAULT 'Dhiira',
            account_type TEXT DEFAULT 'WADIA',
            photo_path TEXT,
            signature_path TEXT,
            balance REAL DEFAULT 0.0,
            status TEXT DEFAULT 'PENDING_APPROVAL',
            freeze_status TEXT DEFAULT 'UNFROZEN',
            mobile_pin TEXT DEFAULT '1234',
            mobile_status TEXT DEFAULT 'INACTIVE',
            created_at TEXT
        )
    """)

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
            reason TEXT DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()

try:
    init_db()
except Exception as e:
    print(f"DB Init Warning: {e}")

HTML_LAYOUT = """
<!DOCTYPE html>
<html lang="om">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Imana Microfinance</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: sans-serif; }
        body { background-color: #f1f5f9; padding-bottom: 80px; color: #0f172a; }
        nav { background: #047857; color: white; padding: 12px 16px; display: flex; justify-content: space-between; align-items: center; }
        .container { max-width: 600px; margin: 0 auto; padding: 16px; }
        .box { background: white; padding: 20px; border-radius: 12px; margin-bottom: 16px; border: 1px solid #e2e8f0; }
        .form-group { margin-bottom: 12px; }
        .form-group label { display: block; font-size: 12px; font-weight: bold; margin-bottom: 4px; }
        .input-field { width: 100%; padding: 11px; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 14px; }
        .btn-submit { width: 100%; background: #047857; color: white; border: none; padding: 12px; border-radius: 8px; font-weight: bold; cursor: pointer; }
        .badge { padding: 3px 8px; border-radius: 4px; font-size: 10px; font-weight: bold; display: inline-block; }
        .badge-active { background: #dcfce7; color: #166534; }
        .badge-pending { background: #fef3c7; color: #92400e; }
        .btn-action { padding: 6px 12px; border-radius: 6px; color: white; text-decoration: none; font-size: 11px; font-weight: bold; display: inline-block; border: none; cursor: pointer; }
        .btn-green { background: #16a34a; } .btn-red { background: #dc2626; } .btn-blue { background: #2563eb; }
    </style>
</head>
<body>
    <audio id="alertAudio" src="https://assets.mixkit.co/active_storage/sfx/2869/2869-preview.mp3" preload="auto"></audio>
    <nav>
        <h1>Imana Microfinance</h1>
        {% if session.get('username') %}
            <div><span><b>{{ session['username'] }}</b></span> <a href="/logout" style="color: #fca5a5; margin-left:8px; text-decoration:none;">Logout</a></div>
        {% elif session.get('mobile_cust_id') %}
            <div><span>📱 <b>{{ session['mobile_name'] }}</b></span> <a href="/mobile_logout" style="color: #fca5a5; margin-left:8px; text-decoration:none;">Logout</a></div>
        {% else %}
            <a href="/mobile_login" style="color: #fbbf24; font-weight: bold; text-decoration: none;">Mobile Banking</a>
        {% endif %}
    </nav>
    <div class="container">
        {% if notifications %}
            <div style="background: #fef3c7; color: #92400e; padding: 10px; border-radius: 8px; font-size: 11px; margin-bottom: 12px; font-weight: bold;">
                🔔 NOTIFICATION: {{ notifications[0] }}
            </div>
        {% endif %}
        {% block content %}{% endblock %}
    </div>
    <script>
    function playAlertSound() {
        var audio = document.getElementById('alertAudio');
        if(audio) { audio.play().catch(e => console.log("Audio play blocked")); }
    }
    window.onload = function() {
        {% if notifications %} playAlertSound(); {% endif %}
    };
    </script>
</body>
</html>
"""

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = %s AND password = %s", (username, password))
        user = cursor.fetchone()
        conn.close()
        if user:
            session.clear()
            session['username'] = user['username']
            session['role'] = user['role']
            return redirect('/')
        else:
            error = "Username ykn Password dogoggoraa!"
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", f"""
    <div class="box" style="text-align: center;">
        <h2>Staff Login</h2>
        {f"<p style='color:red; font-size:12px;'>{error}</p>" if error else ""}
        <form method="POST">
            <div class="form-group" style="text-align:left;"><label>Username</label><input type="text" name="username" class="input-field" required></div>
            <div class="form-group" style="text-align:left;"><label>Password</label><input type="password" name="password" class="input-field" required></div>
            <button type="submit" class="btn-submit">Seeni</button>
        </form>
    </div>
    """))

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/')
def dashboard():
    if 'role' not in session: return redirect('/login')
    role = session['role']
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", f"""
    <div class="box">
        <h3>Baga Nagaan Dhuftan ({role})</h3>
        <p style="margin-top:10px;"><a href="/customers" style="color: #2563eb;">Listii Maammiltootaa</a> | <a href="/pending" style="color: #2563eb;">Manager Approvals</a></p>
    </div>
    """))

# --- TASK 2: API TO VERIFY ACCOUNT NAME & PHOTO FOR MOBILE TRANSFERS ---
@app.route('/api/verify_account/<acc_id>')
def api_verify_account(acc_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT full_name, photo_path, status, freeze_status FROM customers WHERE customer_id = %s", (acc_id.strip(),))
    cust = cursor.fetchone()
    conn.close()
    if cust:
        if cust['status'] != 'ACTIVE' or cust['freeze_status'] == 'FROZEN':
            return jsonify({'success': False, 'message': 'Akkaawuntiin kun active miti ykn uggurameera.'})
        return jsonify({
            'success': True,
            'full_name': cust['full_name'],
            'photo_url': url_for('uploaded_file', filename=cust['photo_path']) if cust['photo_path'] else ''
        })
    return jsonify({'success': False, 'message': 'Akkaawuntii hin argamne!'})

@app.route('/mobile_login', methods=['GET', 'POST'])
def mobile_login():
    msg = None
    if request.method == 'POST':
        cust_id = request.form.get('customer_id', '').strip()
        pin = request.form.get('mobile_pin', '').strip()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM customers WHERE customer_id = %s", (cust_id,))
        cust = cursor.fetchone()
        conn.close()
        if cust:
            if cust['mobile_status'] != 'ACTIVE':
                msg = "❌ Mobile Banking aktivii miti (Manager approval eegaa jira)"
            elif cust['mobile_pin'] != pin:
                msg = "❌ PIN dogoggoraa!"
            else:
                session.clear()
                session['mobile_cust_id'] = cust['customer_id']
                session['mobile_name'] = cust['full_name']
                return redirect('/mobile_dashboard')
        else:
            msg = "❌ Akkaawuntiin hin argamne!"
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", f"""
    <div class="box" style="text-align:center;">
        <h2>Mobile Banking Login</h2>
        {f"<p style='color:red; font-size:12px;'>{msg}</p>" if msg else ""}
        <form method="POST">
            <div class="form-group" style="text-align:left;"><label>Account No</label><input type="text" name="customer_id" class="input-field" required></div>
            <div class="form-group" style="text-align:left;"><label>Mobile PIN (Default: 1234)</label><input type="password" name="mobile_pin" maxlength="4" class="input-field" required></div>
            <button type="submit" class="btn-submit">Seeni</button>
        </form>
    </div>
    """))

@app.route('/mobile_logout')
def mobile_logout():
    session.clear()
    return redirect('/mobile_login')

@app.route('/mobile_dashboard')
def mobile_dashboard():
    if 'mobile_cust_id' not in session: return redirect('/mobile_login')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM customers WHERE customer_id = %s", (session['mobile_cust_id'],))
    cust = cursor.fetchone()
    conn.close()
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", f"""
    <div class="box">
        <h3>Baga Nagaan Dhuftan, {cust['full_name']}</h3>
        <p><b>Balance:</b> {cust['balance']:,.2f} Birr</p>
        <hr style="margin:12px 0;">
        <a href="/mobile_transfer" class="btn-action btn-blue" style="padding:10px 16px; font-size:13px;">🔄 Qarshii Dabarsuu (Transfer)</a>
    </div>
    """))

# --- TASK 3: MOBILE TRANSFER WITH NAME/PHOTO VERIFICATION & SOUND ALERT ---
@app.route('/mobile_transfer', methods=['GET', 'POST'])
def mobile_transfer():
    if 'mobile_cust_id' not in session: return redirect('/mobile_login')
    msg = None
    sender_id = session['mobile_cust_id']
    if request.method == 'POST':
        target_acc = request.form.get('target_account', '').strip()
        amount = float(request.form.get('amount', 0.0))
        pin = request.form.get('pin', '').strip()
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM customers WHERE customer_id = %s", (sender_id,))
        sender = cursor.fetchone()
        cursor.execute("SELECT * FROM customers WHERE customer_id = %s", (target_acc,))
        receiver = cursor.fetchone()

        if not sender or sender['mobile_pin'] != pin:
            msg = "❌ PIN dogoggoraa!"
        elif not receiver or receiver['status'] != 'ACTIVE':
            msg = "❌ Akkaawuntii simataa sirrii miti!"
        elif sender['balance'] < amount:
            msg = "❌ Haafeeka gahaa hin qabdan!"
        else:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ft_ref = f"FT{random.randint(10000, 99999)}"
            cursor.execute("UPDATE customers SET balance = balance - %s WHERE customer_id = %s", (amount, sender_id))
            cursor.execute("UPDATE customers SET balance = balance + %s WHERE customer_id = %s", (amount, target_acc))
            
            cursor.execute("INSERT INTO transactions VALUES (%s, 'T24_TRANSFER', %s, %s, %s, %s, 0.0, 'Imana', %s, 'APPROVED', 'MOBILE', %s, '')", 
                           (f"TXN-{random.randint(1000,9999)}", sender_id, sender['full_name'], target_acc, amount, ft_ref, now))
            cursor.execute("INSERT INTO transactions VALUES (%s, 'MOBILE_TOPUP_REC', %s, %s, %s, %s, 0.0, 'Imana', %s, 'APPROVED', 'MOBILE', %s, %s)", 
                           (f"TXN-{random.randint(1000,9999)}", target_acc, receiver['full_name'], sender_id, amount, ft_ref, now, f"Eergama {sender['full_name']} irraa"))
            conn.commit()
            conn.close()
            
            # Sound alert trigger message
            add_notification(f"🔔 Alert: Qarshiin {amount} Birr {sender['full_name']} irraa gara {receiver['full_name']} tti milkaa'inaan ergameera!")
            return redirect('/mobile_dashboard')
        conn.close()

    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", f"""
    <div class="box">
        <h3>🔄 Qarshii Dabarsuu (Transfer)</h3>
        {f"<p style='color:red; font-size:12px;'>{msg}</p>" if msg else ""}
        <form method="POST">
            <div class="form-group">
                <label>Lakkoofsa Akkaawuntii Simataa (Receiver Acc)</label>
                <input type="text" id="target_account" name="target_account" class="input-field" onblur="verifyAccount()" required>
                <div id="verify_box" style="margin-top:8px; display:none; background:#f8fafc; padding:10px; border-radius:6px; border:1px solid #cbd5e1;">
                    <img id="receiver_photo" src="" alt="Photo" style="width:50px; height:50px; border-radius:50%; object-fit:cover; float:left; margin-right:10px;">
                    <div style="font-size:12px; font-weight:bold; color:#166534;" id="receiver_name"></div>
                    <div style="font-size:10px; color:#64748b;">Mirkanaa'eera (Verified)</div>
                </div>
            </div>
            <div class="form-group"><label>Amount (Birr)</label><input type="number" step="0.01" name="amount" class="input-field" required></div>
            <div class="form-group"><label>Mobile PIN</label><input type="password" name="pin" maxlength="4" class="input-field" required></div>
            <button type="submit" class="btn-submit">Dabarsi</button>
        </form>
    </div>
    <script>
    function verifyAccount() {
        var acc = document.getElementById('target_account').value;
        if(acc.length > 5) {
            fetch('/api/verify_account/' + acc).then(res => res.json()).then(data => {
                var box = document.getElementById('verify_box');
                if(data.success) {
                    box.style.display = 'block';
                    document.getElementById('receiver_name').textContent = "Maqaa: " + data.full_name;
                    if(data.photo_url) { document.getElementById('receiver_photo').src = data.photo_url; }
                } else {
                    box.style.display = 'none';
                    alert(data.message);
                }
            });
        }
    }
    </script>
    """))

# --- TASK 1: MAKER ACTIONS ROUTED TO PENDING MANAGER APPROVAL ---
@app.route('/pending')
def pending():
    if 'role' not in session or session['role'] not in ['MANAGER', 'CEO']: return "Hayyama hin qabdan", 403
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM customers WHERE status = 'PENDING_APPROVAL'")
    custs = cursor.fetchall()
    conn.close()
    
    c_html = "".join([f"<div class='box'><b>{c['full_name']}</b> ({c['customer_id']}) <a href='/approve_customer/{c['customer_id']}' class='btn-action btn-green'>Approve</a></div>" for c in custs])
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", f"""
    <div class="box">
        <h3>Pending Approvals (Manager View)</h3>
        {c_html if c_html else '<p>Gaaffiin eegaa jiru hin jiru.</p>'}
    </div>
    """))

@app.route('/approve_customer/<cust_id>')
def approve_customer(cust_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE customers SET status = 'ACTIVE' WHERE customer_id = %s", (cust_id,))
    conn.commit()
    conn.close()
    add_notification(f"Maammilli Acc: {cust_id} Manageran mirkanaa'eera.")
    return redirect('/pending')

@app.route('/customers')
def customers_list():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM customers ORDER BY created_at DESC LIMIT 20")
    custs = cursor.fetchall()
    conn.close()
    rows = "".join([f"<tr><td>{c['customer_id']}</td><td>{c['full_name']}</td><td>{c['balance']} Birr</td><td><span class='badge badge-{ 'active' if c['status']=='ACTIVE' else 'pending' }'>{c['status']}</span></td></tr>" for c in custs])
    return render_template_string(HTML_LAYOUT.replace("{% block content %}{% endblock %}", f"""
    <div class="box">
        <h3>Listii Maammiltootaa</h3>
        <table style="width:100%; margin-top:10px; border-collapse:collapse; font-size:12px;">
            <tr style="background:#f8fafc;"><th style="padding:6px; text-align:left;">Acc</th><th>Maqaa</th><th>Balance</th><th>Status</th></tr>
            {rows}
        </table>
    </div>
    """))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
