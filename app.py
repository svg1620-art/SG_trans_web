import os, math, subprocess, tempfile, json, hashlib, logging, threading, re
from datetime import datetime
from pathlib import Path
from functools import wraps
from flask import Flask, request, jsonify, session
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

SALES_SCRIPT = """Ты аналитик отдела продаж B2B SaaS компании ServiceGuru (платформа обучения персонала для HoReCa).

Проанализируй транскрипт звонка менеджера с потенциальным клиентом по тарифу ServiceGuru.Клуб (помесячная подписка от 6 990 руб, без годового контракта).

ЛИД
- Название компании:
- Тип (МКК/КК/СКК):
- Источник лида (если упомянут):
- Размер команды (если упомянут):

КВАЛИФИКАЦИЯ
- Есть ли реальная боль с обучением или текучкой: да/нет/не выяснили
- Есть ли бюджет или полномочия принять решение: да/нет/не выяснили
- Есть ли срочность: да/нет/не выяснили

ПОНИМАНИЕ ПРОДУКТА МЕНЕДЖЕРОМ
- Объяснил ли что такое Клуб чётко: да/частично/нет
- Назвал ли ключевое отличие от годового контракта: да/нет
- Упомянул ли комьюнити как ценность: да/нет

РЕАКЦИЯ КЛИЕНТА
- Общий интерес (высокий/средний/низкий/нулевой):
- Что зацепило клиента (если что-то):
- Что отпугнуло или вызвало скепсис:

ВОЗРАЖЕНИЯ
- Перечисли все возражения дословно или близко к тексту:
- Как менеджер закрыл каждое (закрыл/частично/не закрыл):

РЕЗУЛЬТАТ
- Итог звонка (встреча/КП/отказ/перенос/думает):
- Конкретная договорённость:
- Дедлайн следующего контакта:

КАЧЕСТВО РАБОТЫ МЕНЕДЖЕРА
- Что сделал хорошо:
- Что упустил:
- На каком этапе потерял инициативу (если потерял):

ОЦЕНКА ЗВОНКА (1-10)
- Общая оценка:
- Квалификация клиента:
- Презентация продукта:
- Работа с возражениями:
- Закрытие на следующий шаг:

АНОМАЛИИ
- Что необычно:

РЕКОМЕНДАЦИИ
- Топ-3 конкретных действия для менеджера на следующий контакт:

ОЦЕНКИ В JSON (обязательно в конце, строго этот формат):
{"scores":{"overall":0,"qualification":0,"presentation":0,"objections":0,"closing":0},"errors":[],"manager_tasks":[]}

Где errors — список из 1-3 главных ошибок одной фразой каждая, manager_tasks — список из 2-3 конкретных заданий на отработку."""

RENEWAL_SCRIPT = """Ты аналитик отдела клиентского сервиса B2B SaaS компании ServiceGuru (платформа обучения персонала для HoReCa).

Проанализируй транскрипт звонка менеджера с клиентом на тему продления подписки.

КЛИЕНТ
- Название:
- Тип (МКК/КК/СКК):
- Срок работы с платформой:
- Активность на платформе (высокая/средняя/низкая/не упоминалась):

НАСТРОЕНИЕ КЛИЕНТА
- Общий тон (позитивный/нейтральный/негативный/агрессивный):
- Готовность к диалогу (открыт/закрыт/уклоняется):

СУТЬ РАЗГОВОРА
- Главная боль или проблема клиента:
- Что клиент ценит в платформе (если упомянул):
- Что клиенту не нравится или мешает:

ВОЗРАЖЕНИЯ
- Перечисли все возражения дословно или близко к тексту:
- Как менеджер закрыл каждое (закрыл/частично/не закрыл):

РЕЗУЛЬТАТ
- Итог звонка (продлил/думает/отказал/перенёс решение):
- Конкретная договорённость или следующий шаг:
- Дедлайн следующего контакта (если назван):

РИСК ОТВАЛА
- Уровень риска (низкий/средний/высокий/критический):
- Главная причина риска:

КАЧЕСТВО РАБОТЫ МЕНЕДЖЕРА
- Что сделал хорошо:
- Что упустил или мог сделать лучше:
- Использовал ли оффер помесячного тарифа Клуба (если клиент говорил о деньгах): да/нет/не было повода

ОЦЕНКА ЗВОНКА (1-10)
- Общая оценка:
- Выявление причин риска оттока:
- Работа с возражениями:
- Использование ценности продукта:
- Закрытие на продление:

АНОМАЛИИ
- Что необычно:

РЕКОМЕНДАЦИИ
- Топ-3 конкретных действия для менеджера на следующий контакт:

ОЦЕНКИ В JSON (обязательно в конце, строго этот формат):
{"scores":{"overall":0,"churn_detection":0,"objections":0,"value":0,"closing":0},"errors":[],"manager_tasks":[]}

Где errors — список из 1-3 главных ошибок одной фразой каждая, manager_tasks — список из 2-3 конкретных заданий на отработку."""


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


def extract_json_from_text(text):
    try:
        match = re.search(r'\{.*"scores".*\}', text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except:
        pass
    return {"scores": {"overall": 0}, "errors": [], "manager_tasks": []}


def analyze_general(transcript, user_prompt=""):
    if user_prompt.strip():
        context = f"Контекст от пользователя: {user_prompt.strip()}\n\n"
        instruction = (
            "Учитывай контекст выше при анализе. "
            "Проанализируй транскрипцию:\n\n"
            "1. КРАТКОЕ САММЕРИ (3-5 предложений)\n"
            "2. КЛЮЧЕВЫЕ ТЕМЫ\n"
            "3. ГЛАВНЫЕ ВЫВОДЫ\n"
            "4. ACTION ITEMS\n"
            "5. АНАЛИЗ ПО ЗАПРОСУ\n\n"
            "Транскрипция:\n" + transcript
        )
    else:
        context = ""
        instruction = (
            "Проанализируй транскрипцию:\n\n"
            "1. КРАТКОЕ САММЕРИ (3-5 предложений)\n"
            "2. КЛЮЧЕВЫЕ ТЕМЫ\n"
            "3. ГЛАВНЫЕ ВЫВОДЫ\n"
            "4. ACTION ITEMS\n\n"
            "Транскрипция:\n" + transcript
        )
    r = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Ты помощник для анализа транскрипций. Отвечай на русском языке."},
            {"role": "user", "content": context + instruction}
        ],
        max_tokens=2000,
    )
    return r.choices[0].message.content.strip(), {}


def analyze_sales_call(transcript, user_prompt=""):
    extra = f"\nДополнительный контекст: {user_prompt.strip()}\n" if user_prompt.strip() else ""
    r = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Ты аналитик отдела продаж B2B SaaS. Заполняй все поля структуры максимально конкретно. В конце ОБЯЗАТЕЛЬНО выведи JSON с оценками. Отвечай на русском языке."},
            {"role": "user", "content": SALES_SCRIPT + extra + "\n\nТРАНСКРИПТ ЗВОНКА:\n" + transcript}
        ],
        max_tokens=3000,
    )
    text = r.choices[0].message.content.strip()
    metrics = extract_json_from_text(text)
    clean_text = re.sub(r'\{.*"scores".*\}', '', text, flags=re.DOTALL).strip()
    return clean_text, metrics


def analyze_renewal_call(transcript, user_prompt=""):
    extra = f"\nДополнительный контекст: {user_prompt.strip()}\n" if user_prompt.strip() else ""
    r = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Ты аналитик клиентского сервиса B2B SaaS. Заполняй все поля структуры максимально конкретно. В конце ОБЯЗАТЕЛЬНО выведи JSON с оценками. Отвечай на русском языке."},
            {"role": "user", "content": RENEWAL_SCRIPT + extra + "\n\nТРАНСКРИПТ ЗВОНКА:\n" + transcript}
        ],
        max_tokens=3000,
    )
    text = r.choices[0].message.content.strip()
    metrics = extract_json_from_text(text)
    clean_text = re.sub(r'\{.*"scores".*\}', '', text, flags=re.DOTALL).strip()
    return clean_text, metrics


def run_job(job_id, tmp_path, filename, user, user_prompt, mode, manager_name):
    try:
        jobs[job_id]["status"] = "transcribing"
        transcript, n = transcribe(tmp_path)
        jobs[job_id]["status"] = "summarizing"

        if mode == "sales":
            summary, metrics = analyze_sales_call(transcript, user_prompt)
        elif mode == "renewal":
            summary, metrics = analyze_renewal_call(transcript, user_prompt)
        else:
            summary, metrics = analyze_general(transcript, user_prompt)

        if user not in history_store:
            history_store[user] = []

        entry = {
            "filename": filename,
            "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "prompt": user_prompt[:100] if user_prompt else "",
            "mode": mode,
            "manager": manager_name or "Не указан",
            "metrics": metrics,
            "summary": summary,
            "transcript": transcript,
        }
        history_store[user].insert(0, entry)
        if len(history_store[user]) > 100:
            history_store[user] = history_store[user][:100]

        jobs[job_id]["status"] = "done"
        jobs[job_id]["result"] = {
            "summary": summary,
            "transcript": transcript,
            "filename": filename,
            "chunks": n,
            "prompt": user_prompt,
            "mode": mode,
            "manager": manager_name or "Не указан",
            "metrics": metrics,
        }
    except Exception as e:
        logger.exception(f"Job {job_id} failed")
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
    finally:
        try: os.unlink(tmp_path)
        except: pass


@app.route("/")
def index():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    return open(html_path, encoding="utf-8").read()


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
    user_prompt = request.form.get("prompt", "")
    mode = request.form.get("mode", "general")
    manager_name = request.form.get("manager", "")
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    tmp.write(content)
    tmp.close()
    job_id = hashlib.md5(f"{session['user']}{datetime.now()}".encode()).hexdigest()[:12]
    jobs[job_id] = {"status": "starting", "result": None, "error": None}
    t = threading.Thread(target=run_job, args=(job_id, tmp.name, file.filename, session["user"], user_prompt, mode, manager_name))
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
    items = history_store.get(session["user"], [])
    return jsonify({"history": [{"filename": h["filename"], "date": h["date"], "mode": h["mode"], "manager": h.get("manager",""), "prompt": h.get("prompt","")} for h in items]})


@app.route("/api/dashboard")
@login_required
def dashboard():
    items = history_store.get(session["user"], [])
    scored = [h for h in items if h.get("metrics") and h["metrics"].get("scores", {}).get("overall", 0) > 0]

    if not scored:
        return jsonify({"managers": [], "top_errors": [], "total_calls": len(items), "scored_calls": 0})

    managers = {}
    all_errors = []
    for h in scored:
        name = h.get("manager", "Не указан")
        if name not in managers:
            managers[name] = {"name": name, "calls": 0, "scores": [], "errors": [], "tasks": [], "modes": []}
        m = managers[name]
        m["calls"] += 1
        overall = h["metrics"]["scores"].get("overall", 0)
        if overall > 0:
            m["scores"].append(overall)
        errs = h["metrics"].get("errors", [])
        m["errors"].extend(errs)
        all_errors.extend(errs)
        tasks = h["metrics"].get("manager_tasks", [])
        m["tasks"].extend(tasks)
        m["modes"].append(h.get("mode", "general"))

    result_managers = []
    for name, m in managers.items():
        avg = round(sum(m["scores"]) / len(m["scores"]), 1) if m["scores"] else 0
        from collections import Counter
        top_errors = [e for e, _ in Counter(m["errors"]).most_common(3)]
        top_tasks = list(dict.fromkeys(m["tasks"]))[:3]
        result_managers.append({
            "name": name,
            "calls": m["calls"],
            "avg_score": avg,
            "top_errors": top_errors,
            "tasks": top_tasks,
            "modes": m["modes"],
        })

    result_managers.sort(key=lambda x: x["avg_score"], reverse=True)

    from collections import Counter
    top_errors = [{"error": e, "count": c} for e, c in Counter(all_errors).most_common(5)]

    return jsonify({
        "managers": result_managers,
        "top_errors": top_errors,
        "total_calls": len(items),
        "scored_calls": len(scored),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
