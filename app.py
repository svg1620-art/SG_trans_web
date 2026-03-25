import os
import math
import subprocess
import tempfile
import json
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from functools import wraps

from flask import Flask, request, jsonify, session, send_file, render_template_string
from openai import OpenAI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
openai_client = OpenAI(api_key=OPENAI_API_KEY)

USERS_JSON = os.environ.get("USERS", '{"admin": "admin123"}')
USERS = json.loads(USERS_JSON)

SUPPORTED = {".ogg", ".mp3", ".wav", ".m4a", ".mp4", ".webm", ".flac"}
CHUNK_MB = 20
MAX_MB = 200
CHUNK_DURATION = (CHUNK_MB * 1024 * 1024 * 8) / (32 * 1000)

history_store = {}


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def get_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True
        )
        return float(r.stdout.strip())
    except Exception as e:
        logger.error(f"get_duration error: {e}")
        return 0


def split_audio(path):
    total = get_duration(path)
    if total == 0:
        return [path], False
    chunks = []
    n = math.ceil(total / CHUNK_DURATION)
    for i in range(n):
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(i * CHUNK_DURATION), "-t", str(CHUNK_DURATION),
             "-i", path, "-ar", "16000", "-ac", "1", "-b:a", "32k", tmp.name],
            capture_output=True
        )
        chunks.append(tmp.name)
    return chunks, n > 1


def transcribe_single(path):
    logger.info(f"Transcribing: {path}")
    with open(path, "rb") as f:
        r = openai_client.audio.transcriptions.create(
            model="whisper-1", file=f, response_format="text"
        )
    result = r.strip() if isinstance(r, str) else r.text.strip()
    logger.info(f"Transcribed {len(result)} chars")
    return result


def transcribe(path):
    size_mb = os.path.getsize(path) / (1024 * 1024)
    logger.info(f"File size: {size_mb:.1f} MB")
    if size_mb <= CHUNK_MB:
        return transcribe_single(path), 1
    chunks, split = split_audio(path)
    try:
        parts = [transcribe_single(c) for c in chunks]
        return " ".join(parts), len(chunks)
    finally:
        for c in chunks:
            try:
                os.unlink(c)
            except:
                pass


def summarize(transcript):
    logger.info(f"Summarizing {len(transcript)} chars")
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты помощник для анализа транскрипций. Отвечай на русском языке."},
                {"role": "user", "content": (
                    "Проанализируй транскрипцию и дай структурированный анализ:\n\n"
                    "1. КРАТКОЕ САММЕРИ (3-5 предложений)\n"
                    "2. КЛЮЧЕВЫЕ ТЕМЫ — список\n"
                    "3. ГЛАВНЫЕ ВЫВОДЫ — самое важное\n"
                    "4. ACTION ITEMS — договорённости и задачи (если есть)\n\n"
                    "Транскрипция:\n" + transcript
                )}
            ],
            max_tokens=1500,
        )
        result = response.choices[0].message.content.strip()
        logger.info(f"Summary done: {len(result)} chars")
        return result
    except Exception as e:
        logger.exception(f"Summarize error: {e}")
        return f"[Ошибка при создании саммери: {e}]"


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/login", methods=["POST"])
def login():
    data = request.json
    username = data.get("username", "")
    password = data.get("password", "")
    if USERS.get(username) == password:
        session["user"] = username
        return jsonify({"ok": True, "username": username})
    return jsonify({"error": "Неверный логин или пароль"}), 401


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me")
def me():
    user = session.get("user")
    if not user:
        return jsonify({"user": None})
    return jsonify({"user": user})


@app.route("/api/transcribe", methods=["POST"])
@login_required
def transcribe_route():
    logger.info("Transcribe request received")
    if "file" not in request.files:
        return jsonify({"error": "Файл не найден"}), 400

    file = request.files["file"]
    ext = Path(file.filename).suffix.lower()
    if ext not in SUPPORTED:
        return jsonify({"error": f"Формат {ext} не поддерживается"}), 400

    content = file.read()
    size_mb = len(content) / (1024 * 1024)
    if size_mb > MAX_MB:
        return jsonify({"error": f"Файл слишком большой ({size_mb:.0f} MB). Максимум {MAX_MB} MB"}), 400

    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    tmp.write(content)
    tmp.close()

    try:
        transcript, n_chunks = transcribe(tmp.name)
        summary = summarize(transcript)

        result_text = (
            "==================================================\n"
            "САММЕРИ И АНАЛИЗ\n"
            "==================================================\n\n"
            + summary +
            "\n\n\n"
            "==================================================\n"
            "ПОЛНАЯ ТРАНСКРИПЦИЯ\n"
            "==================================================\n\n"
            + transcript
        )

        user = session["user"]
        if user not in history_store:
            history_store[user] = []
        entry = {
            "id": hashlib.md5(f"{user}{datetime.now()}".encode()).hexdigest()[:8],
            "filename": file.filename,
            "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "chunks": n_chunks,
            "summary": summary,
            "transcript": transcript,
            "result": result_text,
        }
        history_store[user].insert(0, entry)
        if len(history_store[user]) > 50:
            history_store[user] = history_store[user][:50]

        logger.info(f"Done. Entry {entry['id']} saved.")
        return jsonify({
            "ok": True,
            "summary": summary,
            "transcript": transcript,
            "chunks": n_chunks,
            "entry_id": entry["id"],
        })
    except Exception as e:
        logger.exception(f"Transcribe route error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            os.unlink(tmp.name)
        except:
            pass


@app.route("/api/history")
@login_required
def history():
    user = session["user"]
    items = history_store.get(user, [])
    return jsonify({"history": [{"id": h["id"], "filename": h["filename"], "date": h["date"]} for h in items]})


@app.route("/api/download/<entry_id>")
@login_required
def download(entry_id):
    user = session["user"]
    items = history_store.get(user, [])
    entry = next((h for h in items if h["id"] == entry_id), None)
    if not entry:
        return jsonify({"error": "Не найдено"}), 404
    tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w", encoding="utf-8")
    tmp.write(entry["result"])
    tmp.close()
    return send_file(tmp.name, as_attachment=True, download_name="transcription.txt")


HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SG_Транскрибация</title>
<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@400;600;700&family=Onest:wght@300;400;500&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #0a0a0f; --surface: #13131a; --surface2: #1c1c26; --border: #2a2a3a;
  --accent: #6c5ce7; --accent2: #a29bfe; --text: #e8e8f0; --muted: #7a7a9a;
  --success: #00b894; --error: #e8553e;
}
body { font-family: 'Onest', sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; display: flex; flex-direction: column; }
#login-screen { flex: 1; display: flex; align-items: center; justify-content: center; padding: 20px; }
.login-card { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 48px 40px; width: 100%; max-width: 400px; animation: fadeUp 0.4s ease; }
.login-logo { font-family: 'Unbounded', sans-serif; font-size: 13px; font-weight: 700; letter-spacing: 0.15em; color: var(--accent2); text-transform: uppercase; margin-bottom: 32px; }
.login-title { font-family: 'Unbounded', sans-serif; font-size: 22px; font-weight: 600; margin-bottom: 8px; }
.login-sub { color: var(--muted); font-size: 14px; margin-bottom: 32px; }
.field { margin-bottom: 16px; }
.field label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 6px; }
.field input { width: 100%; background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; color: var(--text); font-family: 'Onest', sans-serif; font-size: 15px; outline: none; transition: border-color 0.2s; }
.field input:focus { border-color: var(--accent); }
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px; background: var(--accent); color: #fff; border: none; border-radius: 8px; padding: 13px 24px; font-family: 'Onest', sans-serif; font-size: 15px; font-weight: 500; cursor: pointer; transition: opacity 0.2s, transform 0.15s; width: 100%; }
.btn:hover { opacity: 0.9; }
.btn:active { transform: scale(0.98); }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.error-msg { color: var(--error); font-size: 13px; margin-top: 12px; text-align: center; }
#app-screen { flex: 1; display: none; flex-direction: column; }
header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 0 32px; height: 60px; display: flex; align-items: center; justify-content: space-between; }
.header-logo { font-family: 'Unbounded', sans-serif; font-size: 13px; font-weight: 700; letter-spacing: 0.12em; color: var(--accent2); text-transform: uppercase; }
.header-user { display: flex; align-items: center; gap: 12px; font-size: 14px; color: var(--muted); }
.logout-btn { background: none; border: 1px solid var(--border); border-radius: 6px; color: var(--muted); font-family: 'Onest', sans-serif; font-size: 13px; padding: 6px 12px; cursor: pointer; transition: border-color 0.2s, color 0.2s; }
.logout-btn:hover { border-color: var(--accent); color: var(--text); }
.main { flex: 1; display: grid; grid-template-columns: 1fr 320px; gap: 24px; max-width: 1200px; width: 100%; margin: 0 auto; padding: 32px; }
@media (max-width: 768px) { .main { grid-template-columns: 1fr; padding: 16px; } }
.upload-section { display: flex; flex-direction: column; gap: 20px; }
.upload-zone { background: var(--surface); border: 2px dashed var(--border); border-radius: 16px; padding: 48px 32px; text-align: center; cursor: pointer; transition: border-color 0.2s, background 0.2s; position: relative; }
.upload-zone:hover, .upload-zone.drag { border-color: var(--accent); background: rgba(108,92,231,0.05); }
.upload-zone input { position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; }
.upload-icon { font-size: 40px; margin-bottom: 16px; }
.upload-title { font-family: 'Unbounded', sans-serif; font-size: 16px; font-weight: 600; margin-bottom: 8px; }
.upload-sub { font-size: 13px; color: var(--muted); }
.upload-formats { font-size: 12px; color: var(--accent2); margin-top: 8px; }
.selected-file { background: var(--surface2); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; display: none; align-items: center; gap: 12px; font-size: 14px; }
.selected-file.show { display: flex; }
.file-icon { font-size: 20px; }
.file-info { flex: 1; }
.file-name { font-weight: 500; }
.file-size { font-size: 12px; color: var(--muted); }
.progress-wrap { display: none; }
.progress-wrap.show { display: block; }
.progress-label { font-size: 13px; color: var(--muted); margin-bottom: 8px; }
.progress-bar-bg { background: var(--surface2); border-radius: 99px; height: 4px; overflow: hidden; }
.progress-bar { height: 100%; background: var(--accent); border-radius: 99px; transition: width 0.3s; width: 0%; }
.progress-status { font-size: 13px; color: var(--accent2); margin-top: 8px; animation: pulse 1.5s infinite; }
.result-card { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 24px; display: none; }
.result-card.show { display: block; animation: fadeUp 0.4s ease; }
.result-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
.result-title { font-family: 'Unbounded', sans-serif; font-size: 14px; font-weight: 600; }
.download-btn { background: var(--success); color: #fff; border: none; border-radius: 8px; padding: 8px 16px; font-family: 'Onest', sans-serif; font-size: 13px; font-weight: 500; cursor: pointer; transition: opacity 0.2s; }
.download-btn:hover { opacity: 0.85; }
.result-tabs { display: flex; gap: 4px; margin-bottom: 16px; border-bottom: 1px solid var(--border); }
.tab { padding: 8px 16px; font-size: 13px; cursor: pointer; color: var(--muted); border-bottom: 2px solid transparent; margin-bottom: -1px; transition: color 0.2s, border-color 0.2s; }
.tab.active { color: var(--accent2); border-bottom-color: var(--accent2); }
.tab-content { display: none; }
.tab-content.active { display: block; }
.result-text { font-size: 14px; line-height: 1.7; color: var(--text); white-space: pre-wrap; max-height: 400px; overflow-y: auto; padding-right: 8px; }
.result-text::-webkit-scrollbar { width: 4px; }
.result-text::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
.history-section { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 20px; height: fit-content; }
.history-title { font-family: 'Unbounded', sans-serif; font-size: 12px; font-weight: 600; letter-spacing: 0.08em; color: var(--muted); text-transform: uppercase; margin-bottom: 16px; }
.history-empty { font-size: 13px; color: var(--muted); text-align: center; padding: 24px 0; }
.history-list { display: flex; flex-direction: column; gap: 8px; }
.history-item { background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 12px; cursor: pointer; transition: border-color 0.2s; }
.history-item:hover { border-color: var(--accent); }
.history-item-name { font-size: 13px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.history-item-date { font-size: 11px; color: var(--muted); margin-top: 4px; }
@keyframes fadeUp { from { opacity: 0; transform: translateY(12px); } to { opacity: 1; transform: translateY(0); } }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
</style>
</head>
<body>
<div id="login-screen">
  <div class="login-card">
    <div class="login-logo">ServiceGuru</div>
    <div class="login-title">SG_Транскрибация</div>
    <div class="login-sub">Войдите чтобы начать</div>
    <div class="field"><label>Логин</label><input type="text" id="username" placeholder="Введите логин"></div>
    <div class="field"><label>Пароль</label><input type="password" id="password" placeholder="Введите пароль" onkeydown="if(event.key==='Enter')doLogin()"></div>
    <button class="btn" onclick="doLogin()" id="login-btn">Войти</button>
    <div class="error-msg" id="login-error"></div>
  </div>
</div>

<div id="app-screen">
  <header>
    <div class="header-logo">SG_Транскрибация</div>
    <div class="header-user">
      <span id="header-username"></span>
      <button class="logout-btn" onclick="doLogout()">Выйти</button>
    </div>
  </header>
  <div class="main">
    <div class="upload-section">
      <div class="upload-zone" id="drop-zone">
        <input type="file" id="file-input" accept=".mp3,.ogg,.wav,.m4a,.mp4,.webm,.flac" onchange="onFileSelect(this)">
        <div class="upload-icon">🎙️</div>
        <div class="upload-title">Загрузите аудиофайл</div>
        <div class="upload-sub">Перетащите файл или нажмите для выбора</div>
        <div class="upload-formats">MP3 · OGG · WAV · M4A · FLAC · до 200 MB</div>
      </div>
      <div class="selected-file" id="selected-file">
        <div class="file-icon">🎵</div>
        <div class="file-info">
          <div class="file-name" id="file-name"></div>
          <div class="file-size" id="file-size"></div>
        </div>
      </div>
      <button class="btn" id="transcribe-btn" onclick="doTranscribe()" disabled>Расшифровать и проанализировать</button>
      <div class="progress-wrap" id="progress-wrap">
        <div class="progress-label">Обработка</div>
        <div class="progress-bar-bg"><div class="progress-bar" id="progress-bar"></div></div>
        <div class="progress-status" id="progress-status">Загружаю файл…</div>
      </div>
      <div class="result-card" id="result-card">
        <div class="result-header">
          <div class="result-title">✅ Готово</div>
          <button class="download-btn" onclick="doDownload()">⬇ Скачать TXT</button>
        </div>
        <div class="result-tabs">
          <div class="tab active" onclick="switchTab('summary')">Саммери</div>
          <div class="tab" onclick="switchTab('transcript')">Транскрипция</div>
        </div>
        <div class="tab-content active" id="tab-summary"><div class="result-text" id="result-summary"></div></div>
        <div class="tab-content" id="tab-transcript"><div class="result-text" id="result-transcript"></div></div>
      </div>
    </div>
    <div class="history-section">
      <div class="history-title">История</div>
      <div id="history-list"><div class="history-empty">Запросов пока нет</div></div>
    </div>
  </div>
</div>

<script>
let currentEntryId = null;
let selectedFile = null;

async function init() {
  const r = await fetch('/api/me');
  const d = await r.json();
  if (d.user) showApp(d.user);
}

async function doLogin() {
  const btn = document.getElementById('login-btn');
  const err = document.getElementById('login-error');
  btn.disabled = true;
  btn.textContent = 'Вхожу…';
  err.textContent = '';
  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: document.getElementById('username').value, password: document.getElementById('password').value})
    });
    const d = await r.json();
    if (d.ok) { showApp(d.username); }
    else { err.textContent = d.error || 'Ошибка входа'; btn.disabled = false; btn.textContent = 'Войти'; }
  } catch(e) { err.textContent = 'Ошибка соединения'; btn.disabled = false; btn.textContent = 'Войти'; }
}

async function doLogout() {
  await fetch('/api/logout', {method: 'POST'});
  document.getElementById('app-screen').style.display = 'none';
  document.getElementById('login-screen').style.display = 'flex';
  document.getElementById('login-btn').disabled = false;
  document.getElementById('login-btn').textContent = 'Войти';
}

function showApp(username) {
  document.getElementById('login-screen').style.display = 'none';
  document.getElementById('app-screen').style.display = 'flex';
  document.getElementById('header-username').textContent = username;
  loadHistory();
}

function onFileSelect(input) {
  const file = input.files[0];
  if (!file) return;
  selectedFile = file;
  document.getElementById('file-name').textContent = file.name;
  document.getElementById('file-size').textContent = (file.size / (1024*1024)).toFixed(1) + ' MB';
  document.getElementById('selected-file').classList.add('show');
  document.getElementById('transcribe-btn').disabled = false;
}

const dz = document.getElementById('drop-zone');
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag'); });
dz.addEventListener('dragleave', () => dz.classList.remove('drag'));
dz.addEventListener('drop', e => {
  e.preventDefault(); dz.classList.remove('drag');
  const file = e.dataTransfer.files[0];
  if (file) {
    selectedFile = file;
    document.getElementById('file-name').textContent = file.name;
    document.getElementById('file-size').textContent = (file.size / (1024*1024)).toFixed(1) + ' MB';
    document.getElementById('selected-file').classList.add('show');
    document.getElementById('transcribe-btn').disabled = false;
  }
});

async function doTranscribe() {
  if (!selectedFile) return;
  const btn = document.getElementById('transcribe-btn');
  btn.disabled = true;
  document.getElementById('result-card').classList.remove('show');
  document.getElementById('progress-wrap').classList.add('show');
  setProgress(10, 'Загружаю файл…');
  const fd = new FormData();
  fd.append('file', selectedFile);
  let p = 10;
  const ticker = setInterval(() => {
    p = Math.min(p + Math.random() * 6, 85);
    const status = p < 35 ? 'Загружаю файл…' : p < 65 ? 'Расшифровываю аудио…' : 'Делаю саммери…';
    setProgress(p, status);
  }, 1500);
  try {
    const r = await fetch('/api/transcribe', {method: 'POST', body: fd});
    const d = await r.json();
    clearInterval(ticker);
    if (d.ok) {
      setProgress(100, 'Готово!');
      setTimeout(() => {
        document.getElementById('progress-wrap').classList.remove('show');
        showResult(d);
        loadHistory();
      }, 600);
    } else {
      document.getElementById('progress-wrap').classList.remove('show');
      alert('Ошибка: ' + (d.error || 'Неизвестная ошибка'));
      btn.disabled = false;
    }
  } catch(e) {
    clearInterval(ticker);
    document.getElementById('progress-wrap').classList.remove('show');
    alert('Ошибка соединения. Попробуйте ещё раз.');
    btn.disabled = false;
  }
}

function setProgress(pct, status) {
  document.getElementById('progress-bar').style.width = pct + '%';
  document.getElementById('progress-status').textContent = status;
}

function showResult(d) {
  currentEntryId = d.entry_id;
  document.getElementById('result-summary').textContent = d.summary;
  document.getElementById('result-transcript').textContent = d.transcript;
  document.getElementById('result-card').classList.add('show');
  document.getElementById('transcribe-btn').disabled = false;
  switchTab('summary');
}

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t, i) => {
    t.classList.toggle('active', (i===0&&name==='summary')||(i===1&&name==='transcript'));
  });
  document.getElementById('tab-summary').classList.toggle('active', name==='summary');
  document.getElementById('tab-transcript').classList.toggle('active', name==='transcript');
}

function doDownload() { if (currentEntryId) window.open('/api/download/' + currentEntryId); }

async function loadHistory() {
  const r = await fetch('/api/history');
  const d = await r.json();
  const el = document.getElementById('history-list');
  if (!d.history || d.history.length === 0) { el.innerHTML = '<div class="history-empty">Запросов пока нет</div>'; return; }
  el.innerHTML = d.history.map(h => `<div class="history-item" onclick="window.open('/api/download/${h.id}')"><div class="history-item-name">${h.filename}</div><div class="history-item-date">${h.date}</div></div>`).join('');
}

init();
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
