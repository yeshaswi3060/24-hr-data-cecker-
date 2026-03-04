"""
Telegram Number Checker - Render Deployment
Flask web server with CSV upload, background Telegram checker, and GitHub-based persistence.
Designed to run 24/7 on Render with UptimeRobot keep-alive.
"""

import json
import csv
import io
import asyncio
import os
import threading
import time
import base64
from datetime import datetime, timezone
from flask import Flask, request, jsonify, Response
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, PhoneNumberInvalidError
from telethon.tl.functions.contacts import ImportContactsRequest, DeleteContactsRequest
from telethon.tl.types import InputPhoneContact
import requests as http_requests

load_dotenv()

app = Flask(__name__)

# ── Config ──────────────────────────────────────────────────
# We load multiple accounts from the environment variables.
# Format: TELEGRAM_API_ID_1, TELEGRAM_API_HASH_1, TELEGRAM_SESSION_STRING_1, etc.
# Or just TELEGRAM_API_ID, etc for the first one.
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN', '')
GITHUB_REPO = os.getenv('GITHUB_REPO', '')  # e.g. "yeshaswi3060/arrange-data"
SAVE_INTERVAL = int(os.getenv('SAVE_INTERVAL', '25'))

# ── Global State ────────────────────────────────────────────
status = {
    'running': False,
    'completed': False,
    'total': 0,
    'checked': 0,
    'found': 0,
    'found_numbers': [],
    'errors': 0,
    'invalid': 0,
    'current_number': '',
    'last_found': '',
    'current_file': '',
    'started_at': None,
    'last_save': None,
    'message': 'Idle — upload a CSV or start from existing data',
    'flood_wait_seconds': 0,
}

checker_thread = None
stop_flag = threading.Event()


# ── GitHub Storage ──────────────────────────────────────────
class GitHubStorage:
    def __init__(self, token, repo):
        self.token = token
        self.repo = repo
        self.base_url = f"https://api.github.com/repos/{repo}/contents"
        self.headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github.v3+json'
        }
        self._sha_cache = {}

    def read_file(self, path):
        """Read file content from GitHub repo."""
        try:
            resp = http_requests.get(
                f"{self.base_url}/{path}",
                headers=self.headers,
                timeout=15
            )
            if resp.status_code == 200:
                data = resp.json()
                content = base64.b64decode(data['content']).decode('utf-8')
                self._sha_cache[path] = data['sha']
                return content
            return None
        except Exception as e:
            print(f"[GitHub] Read error ({path}): {e}")
            return None

    def write_file(self, path, content, message="auto-update"):
        """Write or update a file in the GitHub repo."""
        try:
            if path not in self._sha_cache:
                self.read_file(path)

            payload = {
                'message': message,
                'content': base64.b64encode(content.encode('utf-8')).decode('utf-8')
            }
            if path in self._sha_cache:
                payload['sha'] = self._sha_cache[path]

            resp = http_requests.put(
                f"{self.base_url}/{path}",
                headers=self.headers,
                json=payload,
                timeout=15
            )
            if resp.status_code in [200, 201]:
                self._sha_cache[path] = resp.json()['content']['sha']
                return True
            else:
                print(f"[GitHub] Write fail ({path}): {resp.status_code} - {resp.text[:200]}")
                return False
        except Exception as e:
            print(f"[GitHub] Write error ({path}): {e}")
            return False

    def list_dir(self, path):
        """List files in a GitHub repo directory."""
        try:
            resp = http_requests.get(
                f"{self.base_url}/{path}",
                headers=self.headers,
                timeout=15
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return [item['name'] for item in data]
            return []
        except:
            return []


storage = None


def get_storage():
    global storage
    if storage is None and GITHUB_TOKEN and GITHUB_REPO:
        storage = GitHubStorage(GITHUB_TOKEN, GITHUB_REPO)
    return storage


# ── Phone Number Parser ─────────────────────────────────────
def parse_numbers_from_csv(csv_content):
    """Extract phone numbers from CSV content. Tries multiple column names."""
    numbers = []
    reader = csv.DictReader(io.StringIO(csv_content))

    # Try common column names
    phone_cols = ['mobile', 'phone', 'phone_number', 'number', 'Mobile', 'Phone',
                  'MOBILE', 'PHONE', 'contact', 'Contact', 'tel', 'telephone']

    for row in reader:
        for col in phone_cols:
            if col in row and row[col]:
                num = str(row[col]).strip()
                if num and any(c.isdigit() for c in num):
                    numbers.append(num)
                break
        else:
            # If no named column found, try first column with digits
            for val in row.values():
                val = str(val).strip()
                if val and len(val) >= 10 and any(c.isdigit() for c in val):
                    numbers.append(val)
                    break

    return numbers


def parse_numbers_from_text(text_content):
    """Extract phone numbers from plain text (one per line)."""
    numbers = []
    for line in text_content.strip().split('\n'):
        line = line.strip().strip('\r')
        if line and any(c.isdigit() for c in line):
            numbers.append(line)
    return numbers


def parse_numbers_from_json(json_content):
    """Extract phone numbers from JSON content."""
    data = json.loads(json_content)

    if isinstance(data, list):
        if len(data) > 0 and isinstance(data[0], dict):
            numbers = []
            for item in data:
                mobile = item.get('mobile') or item.get('phone') or item.get('phone_number')
                if mobile:
                    numbers.append(str(mobile))
            return numbers
        else:
            return [str(p) for p in data]
    elif isinstance(data, dict):
        vals = data.get('phone_numbers') or data.get('phones') or data.get('numbers')
        if vals:
            return [str(p) for p in vals]
    return []


def format_phone(phone):
    """Ensure phone has +91 prefix."""
    phone = str(phone).strip()
    if not phone.startswith('+'):
        if len(phone) == 10 and phone.isdigit():
            phone = '+91' + phone
        else:
            phone = '+' + phone
    return phone


# ── Telegram Checker (Background) ───────────────────────────
def background_save(path, content, message):
    """Fire and forget save to GitHub so it doesn't block checking."""
    store = get_storage()
    if store:
        threading.Thread(target=store.write_file, args=(path, content, message), daemon=True).start()

def get_telegram_accounts():
    """Find all configured Telegram accounts in env vars."""
    accounts = []
    
    # Try the default one without prefix first
    api_id = os.getenv('TELEGRAM_API_ID')
    api_hash = os.getenv('TELEGRAM_API_HASH')
    session = os.getenv('TELEGRAM_SESSION_STRING')
    
    if api_id and api_hash and session:
        accounts.append({
            'id': 1,
            'api_id': int(api_id),
            'api_hash': api_hash,
            'session': session
        })
        
    # Look for numbered ones _1, _2, _3, etc.
    for i in range(1, 10):
        # Allow either _X or just X suffix
        str_i = str(i)
        
        api_id = os.getenv(f'TELEGRAM_API_ID_{str_i}') or os.getenv(f'TELEGRAM_API_ID{str_i}')
        if not api_id and i == 1:
            continue # Already caught by default
            
        api_hash = os.getenv(f'TELEGRAM_API_HASH_{str_i}') or os.getenv(f'TELEGRAM_API_HASH{str_i}')
        session = os.getenv(f'TELEGRAM_SESSION_STRING_{str_i}') or os.getenv(f'TELEGRAM_SESSION_STRING{str_i}')
        
        if api_id and api_hash and session:
            accounts.append({
                'id': i,
                'api_id': int(api_id),
                'api_hash': api_hash,
                'session': session
            })
            
    return accounts
async def run_checker(phone_numbers, job_name):
    """Main checker loop — runs in a background thread."""
    global storage
    store = get_storage()

    accounts = get_telegram_accounts()
    if not accounts:
        status['message'] = 'ERROR: No Telegram sessions configured in environment'
        status['running'] = False
        return
        
    if not store:
        status['message'] = 'ERROR: GitHub credentials not configured'
        status['running'] = False
        return

    # Paths in GitHub repo
    results_path = f"telegram_results/{job_name}_telegram.txt"
    checkpoint_path = f"telegram_results/{job_name}.checkpoint"

    status['current_file'] = job_name
    status['total'] = len(phone_numbers)

    # ─ Load checkpoint ─
    status['message'] = 'Loading checkpoint from GitHub...'
    start_index = 0
    cp_content = store.read_file(checkpoint_path)
    if cp_content:
        try:
            start_index = int(cp_content.strip())
            print(f"[Resume] checkpoint at {start_index}/{len(phone_numbers)}")
        except:
            start_index = 0

    # ─ Load existing results ─
    status['message'] = 'Loading existing results from GitHub...'
    found_numbers = []
    res_content = store.read_file(results_path)
    if res_content:
        found_numbers = [l.strip() for l in res_content.strip().split('\n') if l.strip()]
        print(f"[Resume] {len(found_numbers)} numbers already found")

    # Auto-detect resume position if no checkpoint but results exist
    if start_index == 0 and found_numbers:
        status['message'] = 'Auto-detecting resume position...'
        found_set = set(found_numbers)
        for idx, p in enumerate(phone_numbers):
            if format_phone(p) in found_set:
                start_index = idx + 1
        print(f"[Resume] auto-detected position: {start_index}/{len(phone_numbers)}")

    status['checked'] = start_index
    status['found'] = len(found_numbers)
    status['found_numbers'] = found_numbers[-50:]  # keep last 50 for display

    if start_index >= len(phone_numbers):
        status['message'] = f'✅ All {len(phone_numbers)} numbers already checked! Found {len(found_numbers)} on Telegram.'
        status['running'] = False
        status['completed'] = True
        return

    # ─ Connect to Telegram Pool ─
    status['message'] = f'Connecting to {len(accounts)} Telegram account(s)...'
    clients_pool = []
    
    for acc in accounts:
        client = TelegramClient(StringSession(acc['session']), acc['api_id'], acc['api_hash'])
        try:
            await client.start()
            clients_pool.append({
                'id': acc['id'],
                'client': client,
                'sleeping_until': 0
            })
            print(f"Connected to Account {acc['id']}")
        except Exception as e:
            print(f"Failed to connect Account {acc['id']}: {e}")
            await client.disconnect()
            
    if not clients_pool:
        status['message'] = 'ERROR: Failed to connect to any Telegram accounts'
        status['running'] = False
        return

    try:
        status['message'] = f'Checking numbers with {len(clients_pool)} accounts... ({start_index}/{len(phone_numbers)})'
        unsaved = 0
        current_client_idx = 0

        for i in range(start_index, len(phone_numbers)):
            if stop_flag.is_set():
                status['message'] = '⏸ Stopped by user. Progress saved.'
                break

            phone = format_phone(phone_numbers[i])
            status['current_number'] = phone
            status['message'] = f'Checking {phone}... ({i+1}/{len(phone_numbers)})'

            # ─ Select active client ─
            active_client_info = None
            wait_msg_sent = False
            
            while not active_client_info:
                if stop_flag.is_set():
                    break
                    
                now = time.time()
                for i in range(len(clients_pool)):
                    idx = (current_client_idx + i) % len(clients_pool)
                    if clients_pool[idx]['sleeping_until'] <= now:
                        active_client_info = clients_pool[idx]
                        current_client_idx = idx
                        break
                        
                if not active_client_info:
                    # All clients are rate limited, find the one that wakes up first
                    next_wakeup = min(c['sleeping_until'] for c in clients_pool)
                    wait_time = int(next_wakeup - now)
                    if wait_time > 0:
                        status['flood_wait_seconds'] = wait_time
                        if not wait_msg_sent:
                            status['message'] = f'⏳ All accounts rate-limited — waiting {wait_time}s'
                            print(f"  ⚠ All accounts sleeping for {wait_time}s")
                            # Save before deep sleep
                            background_save(checkpoint_path, str(i), "checkpoint before long sleep")
                            wait_msg_sent = True
                        await asyncio.sleep(min(wait_time, 10)) # Check periodically
            
            if stop_flag.is_set():
                break
                
            active_client = active_client_info['client']
            status['message'] = f'Checking {phone}... (Account {active_client_info["id"]}) ({i+1}/{len(phone_numbers)})'

            retry = 0
            while retry < 5:
                if stop_flag.is_set():
                    break
                try:
                    contact = InputPhoneContact(
                        client_id=i, phone=phone,
                        first_name=f"C{i}", last_name=""
                    )
                    result = await active_client(ImportContactsRequest([contact]))

                    if result.users:
                        user = result.users[0]
                        found_numbers.append(phone)
                        status['found'] = len(found_numbers)
                        status['found_numbers'] = found_numbers[-50:]
                        name = f"{user.first_name or ''} {user.last_name or ''}".strip()
                        status['last_found'] = f"{phone} — {name}"
                        print(f"  ✓ {phone}: {name}")

                        # Auto-save results to GitHub in BACKGROUND
                        background_save(
                            results_path,
                            '\n'.join(found_numbers) + '\n',
                            f"found: {phone}"
                        )
                        status['last_save'] = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')

                        try:
                            await active_client(DeleteContactsRequest(id=[user.id]))
                        except:
                            pass

                    await asyncio.sleep(0.3)
                    break

                except PhoneNumberInvalidError:
                    status['invalid'] += 1
                    break

                except FloodWaitError as e:
                    wait = e.seconds
                    status['flood_wait_seconds'] = wait
                    print(f"  ⚠ Account {active_client_info['id']} Flood wait {wait}s")
                    
                    # Mark this client as sleeping
                    active_client_info['sleeping_until'] = time.time() + wait + 1
                    
                    # If we have multiple accounts, just break this inner retry loop
                    # and the outer loop will pick the next available account for this same number
                    if len(clients_pool) > 1:
                        status['message'] = f'🔄 Account {active_client_info["id"]} rate limited, switching...'
                        print(f"  🔄 Switching to next account...")
                        # Save progress before switching just in case
                        background_save(checkpoint_path, str(i), f"checkpoint (switching from {active_client_info['id']})")
                        break # Break inner loop, will retry same number with new client
                    else:
                        # Only one account, must wait
                        retry += 1
                        status['message'] = f'⏳ Rate limited — waiting {wait}s (retry {retry}/5)'
                        background_save(checkpoint_path, str(i), "checkpoint before flood")
                        await asyncio.sleep(wait + 1)
                        status['flood_wait_seconds'] = 0

                except Exception as e:
                    status['errors'] += 1
                    print(f"  ✗ {phone}: {e}")
                    break

            status['checked'] = i + 1
            unsaved += 1

            # Save checkpoint periodically (background)
            if unsaved >= SAVE_INTERVAL:
                background_save(checkpoint_path, str(i + 1), f"checkpoint {i+1}/{len(phone_numbers)}")
                status['last_save'] = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')
                unsaved = 0

        # Final save (also background)
        background_save(checkpoint_path, str(status['checked']), "final checkpoint")
        background_save(
            results_path,
            '\n'.join(found_numbers) + '\n',
            f"final: {len(found_numbers)} found out of {len(phone_numbers)}"
        )
        status['last_save'] = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')

        if not stop_flag.is_set():
            status['message'] = f'✅ Complete! Checked {len(phone_numbers)} numbers. Found {len(found_numbers)} on Telegram.'
            status['completed'] = True

    except Exception as e:
        status['message'] = f'ERROR: {e}'
        print(f"Checker error: {e}")
    finally:
        for c in clients_pool:
            try:
                await c['client'].disconnect()
            except:
                pass
        status['running'] = False


def start_checker(phone_numbers, job_name):
    """Launch the checker in a background thread."""
    global checker_thread
    if checker_thread and checker_thread.is_alive():
        return False

    stop_flag.clear()
    status['running'] = True
    status['completed'] = False
    status['started_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    status['errors'] = 0
    status['invalid'] = 0

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run_checker(phone_numbers, job_name))
        finally:
            loop.close()

    checker_thread = threading.Thread(target=_run, daemon=True)
    checker_thread.start()
    return True


# ── In-memory number store (loaded from upload or GitHub) ───
current_numbers = []
current_job = ''


# ── Dashboard HTML ──────────────────────────────────────────
DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Telegram Checker</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',sans-serif;background:#0a0e17;color:#e2e8f0;min-height:100vh}
.container{max-width:900px;margin:0 auto;padding:24px 16px}
h1{font-size:1.6rem;font-weight:700;background:linear-gradient(135deg,#60a5fa,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:4px}
.subtitle{color:#64748b;font-size:.85rem;margin-bottom:28px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-bottom:24px}
.card{background:#131a2b;border:1px solid #1e293b;border-radius:12px;padding:16px;text-align:center}
.card .val{font-size:1.8rem;font-weight:700;color:#60a5fa}
.card .val.green{color:#34d399}
.card .val.red{color:#f87171}
.card .val.yellow{color:#fbbf24}
.card .lbl{font-size:.7rem;color:#64748b;text-transform:uppercase;letter-spacing:.5px;margin-top:4px}
.progress-wrap{background:#131a2b;border:1px solid #1e293b;border-radius:12px;padding:16px;margin-bottom:24px}
.progress-bar{width:100%;height:22px;background:#1e293b;border-radius:11px;overflow:hidden;margin:8px 0}
.progress-fill{height:100%;background:linear-gradient(90deg,#3b82f6,#8b5cf6);border-radius:11px;transition:width .5s ease;min-width:0}
.progress-text{display:flex;justify-content:space-between;font-size:.8rem;color:#94a3b8}
.status-msg{background:#131a2b;border:1px solid #1e293b;border-radius:12px;padding:14px 16px;margin-bottom:24px;font-size:.85rem;color:#94a3b8;display:flex;align-items:center;gap:8px}
.status-msg .dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.dot.green{background:#34d399;box-shadow:0 0 8px #34d39966}
.dot.blue{background:#60a5fa;box-shadow:0 0 8px #60a5fa66;animation:pulse 1.5s infinite}
.dot.red{background:#f87171}
.dot.yellow{background:#fbbf24;animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.upload-box{background:#131a2b;border:2px dashed #1e293b;border-radius:12px;padding:32px;text-align:center;margin-bottom:24px;transition:border-color .2s}
.upload-box:hover{border-color:#3b82f6}
.upload-box h3{font-size:1rem;margin-bottom:8px;color:#cbd5e1}
.upload-box p{font-size:.8rem;color:#64748b;margin-bottom:16px}
.upload-box input[type=file]{display:none}
.upload-box label{display:inline-block;padding:10px 24px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#fff;border-radius:8px;cursor:pointer;font-size:.85rem;font-weight:600;transition:transform .15s}
.upload-box label:hover{transform:scale(1.03)}
.file-name{margin-top:10px;font-size:.8rem;color:#60a5fa}
.btn{padding:10px 20px;border:none;border-radius:8px;font-size:.85rem;font-weight:600;cursor:pointer;font-family:inherit;transition:transform .1s,opacity .1s}
.btn:active{transform:scale(.96)}
.btn-start{background:linear-gradient(135deg,#22c55e,#16a34a);color:#fff}
.btn-stop{background:linear-gradient(135deg,#ef4444,#dc2626);color:#fff}
.btn-secondary{background:#1e293b;color:#94a3b8;margin-left:8px}
.btn:disabled{opacity:.4;cursor:not-allowed}
.actions{display:flex;gap:8px;margin-bottom:24px;flex-wrap:wrap}
.found-list{background:#131a2b;border:1px solid #1e293b;border-radius:12px;padding:16px;max-height:300px;overflow-y:auto}
.found-list h3{font-size:.9rem;color:#cbd5e1;margin-bottom:10px}
.found-list .item{font-size:.8rem;color:#34d399;padding:4px 0;border-bottom:1px solid #1e293b1a;font-family:monospace}
.found-list .empty{color:#475569;font-size:.8rem;font-style:italic}
.info-row{display:flex;justify-content:space-between;font-size:.75rem;color:#475569;margin-top:16px;flex-wrap:wrap;gap:4px}
</style>
</head>
<body>
<div class="container">
    <h1>📱 Telegram Number Checker</h1>
    <p class="subtitle">Upload CSV / JSON / TXT — checks numbers against Telegram — saves results to GitHub</p>

    <div id="statusMsg" class="status-msg">
        <span class="dot blue"></span>
        <span id="msgText">Loading...</span>
    </div>

    <div class="cards">
        <div class="card"><div class="val" id="vTotal">0</div><div class="lbl">Total</div></div>
        <div class="card"><div class="val" id="vChecked">0</div><div class="lbl">Checked</div></div>
        <div class="card"><div class="val green" id="vFound">0</div><div class="lbl">Found</div></div>
        <div class="card"><div class="val red" id="vErrors">0</div><div class="lbl">Errors</div></div>
        <div class="card"><div class="val yellow" id="vInvalid">0</div><div class="lbl">Invalid</div></div>
    </div>

    <div class="progress-wrap">
        <div class="progress-text">
            <span id="pLabel">Progress</span>
            <span id="pPct">0%</span>
        </div>
        <div class="progress-bar"><div class="progress-fill" id="pFill" style="width:0%"></div></div>
        <div class="progress-text">
            <span id="pCurrent"></span>
            <span id="pLastSave"></span>
        </div>
    </div>

    <div class="upload-box" id="uploadBox">
        <h3>Upload Phone Numbers</h3>
        <p>Supports CSV, JSON, or TXT files with phone numbers</p>
        <form id="uploadForm" enctype="multipart/form-data">
            <label for="fileInput">Choose File</label>
            <input type="file" id="fileInput" accept=".csv,.json,.txt" onchange="showFileName(this)">
        </form>
        <div class="file-name" id="fileName"></div>
    </div>

    <div class="actions">
        <button class="btn btn-start" id="btnStart" onclick="startChecker()">▶ Start Checking</button>
        <button class="btn btn-stop" id="btnStop" onclick="stopChecker()" disabled>⏹ Stop</button>
        <button class="btn btn-secondary" onclick="location.reload()">↻ Refresh</button>
    </div>

    <div class="found-list">
        <h3>🟢 Recent Telegram Numbers Found</h3>
        <div id="foundItems"><div class="empty">No numbers found yet</div></div>
    </div>

    <div class="info-row">
        <span>File: <strong id="iFile">—</strong></span>
        <span>Started: <strong id="iStarted">—</strong></span>
        <span>Last save: <strong id="iSave">—</strong></span>
    </div>
</div>

<script>
function showFileName(input) {
    const name = input.files[0]?.name || '';
    document.getElementById('fileName').textContent = name;
}

async function startChecker() {
    const fileInput = document.getElementById('fileInput');
    const file = fileInput.files[0];

    if (file) {
        // Upload file first
        const formData = new FormData();
        formData.append('file', file);
        const resp = await fetch('/upload', {method:'POST', body: formData});
        const data = await resp.json();
        if (data.error) { alert(data.error); return; }
    }

    const resp = await fetch('/start');
    const data = await resp.json();
    if (data.error) alert(data.error);
    refreshStatus();
}

async function stopChecker() {
    await fetch('/stop');
    refreshStatus();
}

async function refreshStatus() {
    try {
        const resp = await fetch('/api/status');
        const s = await resp.json();

        document.getElementById('vTotal').textContent = s.total.toLocaleString();
        document.getElementById('vChecked').textContent = s.checked.toLocaleString();
        document.getElementById('vFound').textContent = s.found.toLocaleString();
        document.getElementById('vErrors').textContent = s.errors.toLocaleString();
        document.getElementById('vInvalid').textContent = s.invalid.toLocaleString();

        const pct = s.total > 0 ? Math.round((s.checked / s.total) * 100) : 0;
        document.getElementById('pFill').style.width = pct + '%';
        document.getElementById('pPct').textContent = pct + '%';
        document.getElementById('pLabel').textContent = s.checked + ' / ' + s.total;
        document.getElementById('pCurrent').textContent = s.current_number ? 'Current: ' + s.current_number : '';
        document.getElementById('pLastSave').textContent = s.last_found ? 'Last found: ' + s.last_found : '';

        // Status message
        const msgEl = document.getElementById('msgText');
        const dotEl = document.querySelector('.dot');
        msgEl.textContent = s.message;
        dotEl.className = 'dot ' + (s.running ? (s.flood_wait_seconds > 0 ? 'yellow' : 'blue') : (s.completed ? 'green' : 'red'));

        // Buttons
        document.getElementById('btnStart').disabled = s.running;
        document.getElementById('btnStop').disabled = !s.running;

        // Found list
        const foundDiv = document.getElementById('foundItems');
        if (s.found_numbers && s.found_numbers.length > 0) {
            foundDiv.innerHTML = s.found_numbers.slice().reverse().map(n =>
                '<div class="item">' + n + '</div>'
            ).join('');
        } else {
            foundDiv.innerHTML = '<div class="empty">No numbers found yet</div>';
        }

        // Info row
        document.getElementById('iFile').textContent = s.current_file || '—';
        document.getElementById('iStarted').textContent = s.started_at || '—';
        document.getElementById('iSave').textContent = s.last_save || '—';

    } catch(e) { console.error('Status fetch error:', e); }
}

// Auto-refresh every 5 seconds
setInterval(refreshStatus, 5000);
refreshStatus();
</script>
</body>
</html>"""


# ── Routes ──────────────────────────────────────────────────

@app.route('/')
def dashboard():
    return Response(DASHBOARD, mimetype='text/html')


@app.route('/health')
def health():
    """Health check for UptimeRobot. Also auto-restarts checker if it died."""
    global current_numbers, current_job

    # Auto-restart if we have numbers loaded and checker isn't running/completed
    if current_numbers and not status['running'] and not status['completed']:
        start_checker(current_numbers, current_job)

    return jsonify({
        'status': 'ok',
        'checker_running': status['running'],
        'checked': status['checked'],
        'total': status['total'],
        'found': status['found'],
    })


@app.route('/upload', methods=['POST'])
def upload():
    """Upload CSV/JSON/TXT file with phone numbers."""
    global current_numbers, current_job

    if status['running']:
        return jsonify({'error': 'Checker is already running. Stop it first.'}), 400

    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'Empty filename'}), 400

    filename = file.filename
    content = file.read().decode('utf-8', errors='ignore')

    # Parse numbers based on file type
    try:
        if filename.endswith('.csv'):
            numbers = parse_numbers_from_csv(content)
        elif filename.endswith('.json'):
            numbers = parse_numbers_from_json(content)
        elif filename.endswith('.txt'):
            numbers = parse_numbers_from_text(content)
        else:
            return jsonify({'error': 'Unsupported file type. Use .csv, .json, or .txt'}), 400
    except Exception as e:
        return jsonify({'error': f'Failed to parse file: {e}'}), 400

    if not numbers:
        return jsonify({'error': 'No phone numbers found in the file'}), 400

    # Store in GitHub for persistence
    store = get_storage()
    job_name = os.path.splitext(filename)[0]

    if store:
        store.write_file(
            f"telegram_results/{job_name}_input.txt",
            '\n'.join(numbers) + '\n',
            f"Upload: {filename} ({len(numbers)} numbers)"
        )

    current_numbers = numbers
    current_job = job_name

    # Reset status
    status['completed'] = False
    status['checked'] = 0
    status['found'] = 0
    status['found_numbers'] = []
    status['total'] = len(numbers)
    status['message'] = f'Uploaded {filename} — {len(numbers)} numbers ready'

    return jsonify({
        'status': 'uploaded',
        'filename': filename,
        'numbers_found': len(numbers),
    })


@app.route('/start')
def start():
    """Start the checker."""
    global current_numbers, current_job

    if status['running']:
        return jsonify({'error': 'Already running'})

    # If no numbers loaded from upload, try loading from GitHub
    if not current_numbers:
        store = get_storage()
        if store:
            # List available jobs in telegram_results/
            files = store.list_dir('telegram_results')
            input_files = [f for f in files if f.endswith('_input.txt')]
            if input_files:
                # Load the most recent input file
                latest = input_files[-1]
                content = store.read_file(f"telegram_results/{latest}")
                if content:
                    current_numbers = [l.strip() for l in content.strip().split('\n') if l.strip()]
                    current_job = latest.replace('_input.txt', '')
                    status['total'] = len(current_numbers)

    if not current_numbers:
        return jsonify({'error': 'No numbers loaded. Upload a file first.'})

    if start_checker(current_numbers, current_job):
        return jsonify({'status': 'started', 'total': len(current_numbers), 'job': current_job})
    return jsonify({'error': 'Failed to start'})


@app.route('/stop')
def stop():
    """Stop the checker gracefully."""
    stop_flag.set()
    status['message'] = '⏸ Stopping... saving progress...'
    return jsonify({'status': 'stopping'})


@app.route('/api/status')
def api_status():
    """JSON status endpoint for the dashboard."""
    return jsonify(status)


# ── Auto-load from GitHub on startup ────────────────────────
def auto_load_job():
    """On startup, check if there's an unfinished job in GitHub and resume it."""
    global current_numbers, current_job
    store = get_storage()
    if not store:
        return

    files = store.list_dir('telegram_results')
    input_files = [f for f in files if f.endswith('_input.txt')]

    for input_file in reversed(input_files):
        job = input_file.replace('_input.txt', '')
        # Check if this job has a checkpoint (i.e. unfinished)
        cp = store.read_file(f"telegram_results/{job}.checkpoint")
        content = store.read_file(f"telegram_results/{input_file}")

        if content:
            numbers = [l.strip() for l in content.strip().split('\n') if l.strip()]
            checked = 0
            if cp:
                try:
                    checked = int(cp.strip())
                except:
                    checked = 0

            if checked < len(numbers):
                current_numbers = numbers
                current_job = job
                status['total'] = len(numbers)
                status['message'] = f'Resumable job found: {job} ({checked}/{len(numbers)})'
                print(f"[Startup] Found resumable job: {job} ({checked}/{len(numbers)})")
                # Auto-start
                start_checker(current_numbers, current_job)
                return


# ── Entry Point ─────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))

    # Try to auto-resume on startup
    threading.Timer(2.0, auto_load_job).start()

    app.run(host='0.0.0.0', port=port, debug=False)
