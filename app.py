import os, math, subprocess, tempfile, json, hashlib, logging, threading, re
from datetime import datetime
from pathlib import Path
from functools import wraps
from flask import Flask, request, jsonify, session
from openai import OpenAI
import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me")
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
USERS = json.loads(os.environ.get("USERS", '{"admin": "admin123"}'))
ADMIN_USER = os.environ.get("ADMIN_USER", list(USERS.keys())[0])
SUPPORTED = {".ogg", ".mp3", ".wav", ".m4a", ".mp4", ".webm", ".flac"}
CHUNK_MB = 20
MAX_MB = 200
CHUNK_SEC = (CHUNK_MB * 1024 * 1024 * 8) / (32 * 1000)
jobs = {}
DATABASE_URL = os.environ.get("DATABASE_URL", "")


def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS calls (
                id SERIAL PRIMARY KEY,
                account TEXT NOT NULL,
                filename TEXT,
                date TEXT,
                mode TEXT,
                manager TEXT,
                prompt TEXT,
                summary TEXT,
                transcript TEXT,
                metrics JSONB,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS employees (
                id SERIAL PRIMARY KEY,
                account TEXT NOT NULL,
                name TEXT NOT NULL,
                in_dashboard BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("DB initialized")
    except Exception as e:
        logger.error(f"DB init error: {e}")


SALES_CLUB_SCRIPT = """Ты аналитик отдела продаж B2B SaaS компании ServiceGuru (платформа обучения персонала для HoReCa).

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

В самом конце выведи ТОЛЬКО этот JSON без каких-либо обёрток, markdown, кавычек вокруг:
{"scores":{"overall":0,"qualification":0,"presentation":0,"objections":0,"closing":0},"errors":["ошибка1"],"manager_tasks":["задание1"]}

Замени нули на реальные оценки, ошибка1 и задание1 на реальные значения."""

SALES_SCRIPT = """Ты аналитик отдела продаж B2B SaaS компании ServiceGuru (платформа обучения персонала для HoReCa).

Проанализируй транскрипт звонка менеджера с потенциальным клиентом по основным тарифам ServiceGuru (полугодовой или годовой контракт).

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
- Объяснил ли платформу чётко: да/частично/нет
- Назвал ли ключевые преимущества (аналитика, SCORM, мобильное приложение): да/частично/нет
- Показал ли ценность для конкретного типа заведения клиента: да/нет

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
- Какой тариф обсуждался (полугодовой/годовой/не уточнили):

КАЧЕСТВО РАБОТЫ МЕНЕДЖЕРА
- Что сделал хорошо:
- Что упустил:
- Предложил ли демо или пробный период: да/нет
- На каком этапе потерял инициативу (если потерял):

ОЦЕНКА ЗВОНКА (1-10)
- Общая оценка:
- Квалификация клиента:
- Презентация платформы:
- Работа с возражениями:
- Закрытие на следующий шаг:

АНОМАЛИИ
- Что необычно:

РЕКОМЕНДАЦИИ
- Топ-3 конкретных действия для менеджера на следующий контакт:

В самом конце выведи ТОЛЬКО этот JSON без каких-либо обёрток, markdown, кавычек вокруг:
{"scores":{"overall":0,"qualification":0,"presentation":0,"objections":0,"closing":0},"errors":["ошибка1"],"manager_tasks":["задание1"]}

Замени нули на реальные оценки, ошибка1 и задание1 на реальные значения."""

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

В самом конце выведи ТОЛЬКО этот JSON без каких-либо обёрток, markdown, кавычек вокруг:
{"scores":{"overall":0,"churn_detection":0,"objections":0,"value":0,"closing":0},"errors":["ошибка1"],"manager_tasks":["задание1"]}

Замени нули на реальные оценки, ошибка1 и задание1 на реальные значения."""


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("user") != ADMIN_USER:
            return jsonify({"error": "forbidden"}), 403
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
    text_clean = re.sub(r'```json\s*', '', text)
    text_clean = re.sub(r'```\s*', '', text_clean)
    idx = text_clean.find('"scores"')
    if idx == -1:
        return {"scores": {"overall": 0}, "errors": [], "manager_tasks": []}
    start = text_clean.rfind('{', 0, idx)
    if start == -1:
        return {"scores": {"overall": 0}, "errors": [], "manager_tasks": []}
    depth = 0
    end = -1
    for i in range(start, len(text_clean)):
        if text_clean[i] == '{':
            depth += 1
        elif text_clean[i] == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return {"scores": {"overall": 0}, "errors": [], "manager_tasks": []}
    try:
        result = json.loads(text_clean[start:end])
        logger.info(f"Extracted metrics: {result}")
        return result
    except Exception as e:
        logger.error(f"JSON parse error: {e}")
        return {"scores": {"overall": 0}, "errors": [], "manager_tasks": []}


def run_analysis(script, system_prompt, transcript, user_prompt):
    extra = f"\nДополнительный контекст: {user_prompt.strip()}\n" if user_prompt.strip() else ""
    r = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": script + extra + "\n\nТРАНСКРИПТ ЗВОНКА:\n" + transcript}
        ],
        max_tokens=3000,
    )
    text = r.choices[0].message.content.strip()
    logger.info(f"GPT tail: {text[-300:]}")
    metrics = extract_json_from_text(text)
    text_clean = re.sub(r'```json\s*', '', text)
    text_clean = re.sub(r'```\s*', '', text_clean)
    text_clean = re.sub(r'\{[^{}]*"scores".*?\}', '', text_clean, flags=re.DOTALL).strip()
    return text_clean, metrics


def analyze_general(transcript, user_prompt=""):
    content = ("Контекст: " + user_prompt.strip() + "\n\n" if user_prompt.strip() else "") + \
              "Проанализируй:\n1. КРАТКОЕ САММЕРИ\n2. КЛЮЧЕВЫЕ ТЕМЫ\n3. ГЛАВНЫЕ ВЫВОДЫ\n4. ACTION ITEMS" + \
              ("\n5. АНАЛИЗ ПО ЗАПРОСУ" if user_prompt.strip() else "") + "\n\nТранскрипция:\n" + transcript
    r = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "Ты помощник для анализа транскрипций. Отвечай на русском языке."}, {"role": "user", "content": content}],
        max_tokens=2000,
    )
    return r.choices[0].message.content.strip(), {}


def parse_metrics(m):
    if isinstance(m, dict):
        return m
    if isinstance(m, str):
        try:
            return json.loads(m)
        except:
            pass
    return {}


def save_call(account, filename, mode, manager, prompt, summary, transcript, metrics, date):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO calls (account, filename, date, mode, manager, prompt, summary, transcript, metrics)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (account, filename, date, mode, manager, prompt, summary, transcript, json.dumps(metrics)))
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"Saved: {filename}, mode={mode}, manager={manager}, overall={metrics.get('scores',{}).get('overall',0)}")
    except Exception as e:
        logger.error(f"Save call error: {e}")


def run_job(job_id, tmp_path, filename, account, user_prompt, mode, manager_name):
    try:
        jobs[job_id]["status"] = "transcribing"
        transcript, n = transcribe(tmp_path)
        jobs[job_id]["status"] = "summarizing"
        sys_prompt = "Ты аналитик B2B SaaS. Заполняй все поля максимально конкретно. В самом конце ответа выведи JSON с оценками — без markdown, без обёрток. Отвечай на русском."
        if mode == "sales_club":
            summary, metrics = run_analysis(SALES_CLUB_SCRIPT, sys_prompt, transcript, user_prompt)
        elif mode == "sales":
            summary, metrics = run_analysis(SALES_SCRIPT, sys_prompt, transcript, user_prompt)
        elif mode == "renewal":
            summary, metrics = run_analysis(RENEWAL_SCRIPT, sys_prompt, transcript, user_prompt)
        else:
            summary, metrics = analyze_general(transcript, user_prompt)
        date = datetime.now().strftime("%d.%m.%Y %H:%M")
        save_call(account, filename, mode, manager_name or "Не указан", user_prompt, summary, transcript, metrics, date)
        jobs[job_id]["status"] = "done"
        jobs[job_id]["result"] = {
            "summary": summary, "transcript": transcript, "filename": filename,
            "chunks": n, "prompt": user_prompt, "mode": mode,
            "manager": manager_name or "Не указан", "metrics": metrics,
        }
    except Exception as e:
        logger.exception(f"Job {job_id} failed")
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
    finally:
        try: os.unlink(tmp_path)
        except: pass


def build_dashboard_for_modes(rows, modes, filter_manager=None, filter_score_min=None, filter_score_max=None, filter_date_from=None, filter_date_to=None):
    scored = []
    for r in rows:
        if r.get("mode") not in modes:
            continue
        m = parse_metrics(r.get("metrics"))
        overall = m.get("scores", {}).get("overall", 0)
        if overall <= 0:
            continue
        if filter_manager and r.get("manager") != filter_manager:
            continue
        if filter_score_min is not None and overall < filter_score_min:
            continue
        if filter_score_max is not None and overall > filter_score_max:
            continue
        if filter_date_from and r.get("date", "") < filter_date_from:
            continue
        if filter_date_to and r.get("date", "") > filter_date_to:
            continue
        r["metrics"] = m
        scored.append(r)

    total_in_mode = len([r for r in rows if r.get("mode") in modes])

    if not scored:
        return {"managers": [], "top_errors": [], "total_calls": total_in_mode, "scored_calls": 0}

    managers = {}
    all_errors = []
    for r in scored:
        name = r.get("manager") or "Не указан"
        if name not in managers:
            managers[name] = {"name": name, "calls": 0, "scores": [], "errors": [], "tasks": []}
        m = managers[name]
        m["calls"] += 1
        overall = r["metrics"]["scores"].get("overall", 0)
        if overall > 0:
            m["scores"].append(overall)
        errs = r["metrics"].get("errors", [])
        m["errors"].extend(errs)
        all_errors.extend(errs)
        m["tasks"].extend(r["metrics"].get("manager_tasks", []))

    from collections import Counter
    result_managers = []
    for name, m in managers.items():
        avg = round(sum(m["scores"]) / len(m["scores"]), 1) if m["scores"] else 0
        result_managers.append({
            "name": name, "calls": m["calls"], "avg_score": avg,
            "top_errors": [e for e, _ in Counter(m["errors"]).most_common(3)],
            "tasks": list(dict.fromkeys(m["tasks"]))[:3],
        })
    result_managers.sort(key=lambda x: x["avg_score"], reverse=True)
    top_errors = [{"error": e, "count": c} for e, c in Counter(all_errors).most_common(5)]
    return {"managers": result_managers, "top_errors": top_errors, "total_calls": total_in_mode, "scored_calls": len(scored)}


@app.route("/")
def index():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    return open(html_path, encoding="utf-8").read()


@app.route("/api/login", methods=["POST"])
def login():
    d = request.json
    if USERS.get(d.get("username","")) == d.get("password",""):
        session["user"] = d["username"]
        return jsonify({"ok": True, "username": d["username"], "is_admin": d["username"] == ADMIN_USER})
    return jsonify({"error": "Неверный логин или пароль"}), 401


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me")
def me():
    user = session.get("user")
    return jsonify({"user": user, "is_admin": user == ADMIN_USER if user else False})


# --- EMPLOYEES ---
@app.route("/api/employees", methods=["GET"])
@login_required
def get_employees():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, name, in_dashboard FROM employees WHERE account=%s ORDER BY name", (session["user"],))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return jsonify({"employees": rows})
    except Exception as e:
        logger.error(f"Get employees error: {e}")
        return jsonify({"employees": []})


@app.route("/api/employees", methods=["POST"])
@login_required
def add_employee():
    if session.get("user") != ADMIN_USER:
        return jsonify({"error": "Только администратор может добавлять сотрудников"}), 403
    d = request.json
    name = d.get("name", "").strip()
    in_dashboard = d.get("in_dashboard", True)
    if not name:
        return jsonify({"error": "Имя обязательно"}), 400
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO employees (account, name, in_dashboard) VALUES (%s, %s, %s) RETURNING id", (session["user"], name, in_dashboard))
        new_id = cur.fetchone()["id"]
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"ok": True, "id": new_id})
    except Exception as e:
        logger.error(f"Add employee error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/employees/<int:emp_id>", methods=["PUT"])
@login_required
def update_employee(emp_id):
    if session.get("user") != ADMIN_USER:
        return jsonify({"error": "Только администратор"}), 403
    d = request.json
    try:
        conn = get_db()
        cur = conn.cursor()
        if "name" in d and "in_dashboard" in d:
            cur.execute("UPDATE employees SET name=%s, in_dashboard=%s WHERE id=%s AND account=%s", (d["name"], d["in_dashboard"], emp_id, session["user"]))
        elif "in_dashboard" in d:
            cur.execute("UPDATE employees SET in_dashboard=%s WHERE id=%s AND account=%s", (d["in_dashboard"], emp_id, session["user"]))
        elif "name" in d:
            cur.execute("UPDATE employees SET name=%s WHERE id=%s AND account=%s", (d["name"], emp_id, session["user"]))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/employees/<int:emp_id>", methods=["DELETE"])
@login_required
def delete_employee(emp_id):
    if session.get("user") != ADMIN_USER:
        return jsonify({"error": "Только администратор"}), 403
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM employees WHERE id=%s AND account=%s", (emp_id, session["user"]))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT filename, date, mode, manager, prompt FROM calls WHERE account=%s ORDER BY created_at DESC LIMIT 50", (session["user"],))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"history": [dict(r) for r in rows]})
    except Exception as e:
        logger.error(f"History error: {e}")
        return jsonify({"history": []})


@app.route("/api/dashboard/<dash_type>")
@login_required
def dashboard(dash_type):
    filter_manager = request.args.get("manager")
    filter_score_min = request.args.get("score_min", type=int)
    filter_score_max = request.args.get("score_max", type=int)
    filter_date_from = request.args.get("date_from")
    filter_date_to = request.args.get("date_to")

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT mode, manager, metrics, date FROM calls WHERE account=%s ORDER BY created_at DESC", (session["user"],))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        return jsonify({"managers": [], "top_errors": [], "total_calls": 0, "scored_calls": 0})

    modes_map = {
        "sales_club": ["sales_club"],
        "sales": ["sales"],
        "renewal": ["renewal"],
        "all": ["sales_club", "sales", "renewal"],
    }
    modes = modes_map.get(dash_type, ["sales_club", "sales", "renewal"])
    return jsonify(build_dashboard_for_modes(rows, modes, filter_manager, filter_score_min, filter_score_max, filter_date_from, filter_date_to))


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
