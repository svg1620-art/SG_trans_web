import os, math, subprocess, tempfile, json, hashlib, logging, threading
from datetime import datetime
from pathlib import Path
from functools import wraps
from flask import Flask, request, jsonify, session, render_template_string
from openai import OpenAI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me")
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
USERS = json.loads(os.environ.get("USERS", '{"admin": "admin123"}'))
SUPPORTED = {".ogg", ".mp3", ".wav", ".m4a", ".mp4", ".webm", ".flac"}
CHUNK_MB = 20
MAX_MB = 200
CHUNK_SEC = (CHUNK_MB * 1024 * 1024 * 8) / (32 * 1000)
history_store = {}
jobs = {}

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

def get_duration(path):
    try:
        r = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",path], capture_output=True, text=True)
        return float(r.stdout.strip())
    except:
        return 0

def split_audio(path):
    total = get_duration(path)
    if total == 0:
        return [path], 1
    chunks = []
    n = math.ceil(total / CHUNK_SEC)
    for i in range(n):
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        subprocess.run(["ffmpeg","-y","-ss",str(i*CHUNK_SEC),"-t",str(CHUNK_SEC),"-i",path,"-ar","16000","-ac","1","-b:a","32k",tmp.name], capture_output=True)
        chunks.append(tmp.name)
    return chunks, n

def transcribe_single(path):
    with open(path, "rb") as f:
        r = openai_client.audio.transcriptions.create(model="whisper-1", file=f, response_format="text")
    return r.strip() if isinstance(r, str) else r.text.strip()

def transcribe(path):
    size_mb = os.path.getsize(path) / (1024*1024)
    if size_mb <= CHUNK_MB:
        return transcribe_single(path), 1
    chunks, n = split_audio(path)
    try:
        return " ".join(transcribe_single(c) for c in chunks), n
    finally:
        for c in chunks:
            try: os.unlink(c)
            except: pass

def summarize(transcript):
    r = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Ты помощник для анализа транскрипций. Отвечай на русском языке."},
            {"role": "user", "content": "Проанализируй транскрипцию:\n\n1. КРАТКОЕ САММЕРИ (3-5 предложений)\n2. КЛЮЧЕВЫЕ ТЕМЫ\n3. ГЛАВНЫЕ ВЫВОДЫ\n4. ACTION ITEMS\n\nТранскрипция:\n" + transcript}
        ],
        max_tokens=1500,
    )
    return r.choices[0].message.content.strip()

def run_job(job_id, tmp_path, filename, user):
    try:
        jobs[job_id]["status"] = "transcribing"
        transcript, n = transcribe(tmp_path)
        jobs[job_id]["status"] = "summarizing"
        summary = summarize(transcript)
        if user not in history_store:
            history_store[user] = []
        history_store[user].insert(0, {"filename": filename, "date": datetime.now().strftime("%d.%m.%Y %H:%M")})
        if len(history_store[user]) > 50:
            history_store[user] = history_store[user][:50]
        jobs[job_id]["status"] = "done"
        jobs[job_id]["result"] = {"summary": summary, "transcript": transcript, "filename": filename, "chunks": n}
    except Exception as e:
        logger.exception(f"Job {job_id} failed")
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
    finally:
        try: os.unlink(tmp_path)
        except: pass

@app.route("/")
def index():
    return render_template_string(open("/app/index.html").read())

@app.route("/api/login", methods=["POST"])
def login():
    d = request.json
    if USERS.get(d.get("username","")) == d.get("password",""):
        session["user"] = d["username"]
        return jsonify({"ok": True, "username": d["username"]})
    return jsonify({"error": "Неверный логин или пароль"}), 401

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/me")
def me():
    return jsonify({"user": session.get("user")})

@app.route("/api/transcribe", methods=["POST"])
@login_required
def transcribe_route():
    if "file" not in request.files:
        return jsonify({"error": "Файл не найден"}), 400
    file = request.files["file"]
    ext = Path(file.filename).suffix.lower()
    if ext not in SUPPORTED:
        return jsonify({"error": f"Формат {ext} не поддерживается"}), 400
    content = file.read()
    size_mb = len(content) / (1024*1024)
    if size_mb > MAX_MB:
        return jsonify({"error": f"Файл слишком большой ({size_mb:.0f} MB)"}), 400
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    tmp.write(content)
    tmp.close()
    job_id = hashlib.md5(f"{session['user']}{datetime.now()}".encode()).hexdigest()[:12]
    jobs[job_id] = {"status": "starting", "result": None, "error": None}
    t = threading.Thread(target=run_job, args=(job_id, tmp.name, file.filename, session["user"]))
    t.daemon = True
    t.start()
    return jsonify({"ok": True, "job_id": job_id})

@app.route("/api/status/<job_id>")
@login_required
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Не найдено"}), 404
    return jsonify(job)

@app.route("/api/history")
@login_required
def history():
    return jsonify({"history": history_store.get(session["user"], [])})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
