from flask import Flask, request, jsonify, render_template, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
import sqlite3, os, json, random, uuid, time
from dotenv import load_dotenv
from huggingface_hub import InferenceClient
from groq import Groq
from srs_service import update_srs_item, get_due_review_items
from quiz_service import create_session, get_session_status
from analytics_service import (
    upsert_word_stats,
    get_performance_trends,
    get_priority_words,
    build_daily_plan,
    log_performance,
)
from gamification_service import add_xp, update_weekly_progress, evaluate_achievements, gamification_summary

load_dotenv()

app = Flask(__name__)
DB = os.path.join(os.path.dirname(__file__), 'words.db')
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-this-secret-in-production")
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

HF_MODELS = [
    "google/gemma-2-2b-it",
    "meta-llama/Llama-3.1-8B-Instruct",
    "mistralai/Mistral-7B-Instruct-v0.3",
]


def call_ai(prompt: str, api_key_override: str = "") -> str:
    hf_key = (api_key_override or os.getenv("HUGGINGFACE_API_KEY") or "").strip()
    groq_key = (os.getenv("GROQ_API_KEY") or "").strip()
    errors = []
    retry_count = 2

    if hf_key:
        hf_client = InferenceClient(api_key=hf_key)
        for model in HF_MODELS:
            for attempt in range(1, retry_count + 1):
                try:
                    completion = hf_client.chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=800,
                    )
                    text = (completion.choices[0].message.content or "").strip()
                    if text:
                        return text
                    raise Exception("Empty response text")
                except Exception as e:
                    msg = f"HuggingFace model {model} failed (attempt {attempt}/{retry_count}): {e}"
                    print(msg)
                    errors.append(msg)
                    time.sleep(1)
    else:
        msg = "HuggingFace skipped: HUGGINGFACE_API_KEY missing."
        print(msg)
        errors.append(msg)

    if groq_key:
        groq_models = ["llama3-70b-8192", "llama-3.3-70b-versatile"]
        for attempt in range(1, retry_count + 1):
            for groq_model in groq_models:
                try:
                    groq_client = Groq(api_key=groq_key)
                    response = groq_client.chat.completions.create(
                        model=groq_model,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    text = (
                        (response.choices[0].message.content if response and response.choices else "")
                        or ""
                    ).strip()
                    if text:
                        return text
                    raise Exception("Empty response text")
                except Exception as e:
                    msg = f"Groq model {groq_model} failed (attempt {attempt}/{retry_count}): {e}"
                    print(msg)
                    errors.append(msg)
                    time.sleep(1)
    else:
        msg = "Groq skipped: GROQ_API_KEY missing."
        print(msg)
        errors.append(msg)

    preview = " | ".join(errors[-6:]) if errors else "No provider attempts were executed."
    raise Exception(f"All AI providers failed. {preview}")


def _normalize_term_list(items):
    seen = set()
    out = []
    for item in items or []:
        val = (item or "").strip()
        if not val:
            continue
        key = val.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(val)
    return out

# ── DB SETUP ────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS words (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER,
                word      TEXT    NOT NULL UNIQUE,
                synonyms  TEXT    DEFAULT '[]',
                antonyms  TEXT    DEFAULT '[]',
                phrases   TEXT    DEFAULT '[]',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS quiz_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                source TEXT NOT NULL DEFAULT 'fallback',
                mode TEXT DEFAULT 'mixed',
                difficulty TEXT DEFAULT 'all',
                topic TEXT DEFAULT 'all',
                total_questions INTEGER DEFAULT 10,
                number_of_questions INTEGER DEFAULT 10,
                exam_start_time TEXT,
                total_duration_seconds INTEGER DEFAULT 500,
                status TEXT DEFAULT 'active',
                correct_count INTEGER DEFAULT 0,
                wrong_count INTEGER DEFAULT 0,
                started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                ended_at DATETIME
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS question_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                session_id INTEGER,
                question_key TEXT NOT NULL,
                question_text TEXT NOT NULL,
                topic TEXT DEFAULT '',
                difficulty TEXT DEFAULT '',
                selected_answer TEXT DEFAULT '',
                correct_answer TEXT DEFAULT '',
                options_json TEXT DEFAULT '[]',
                is_correct INTEGER NOT NULL,
                source TEXT DEFAULT 'fallback',
                word_key TEXT DEFAULT '',
                answered_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS spaced_repetition (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                question_key TEXT NOT NULL,
                question_text TEXT NOT NULL,
                topic TEXT DEFAULT '',
                difficulty TEXT DEFAULT '',
                next_review_at TEXT,
                repetition_count INTEGER DEFAULT 0,
                ease_factor REAL DEFAULT 2.5,
                last_result TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS word_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                word_key TEXT NOT NULL,
                word_text TEXT NOT NULL,
                frequency_seen INTEGER DEFAULT 0,
                frequency_wrong INTEGER DEFAULT 0,
                frequency_correct INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, word_key)
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS performance_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                session_id INTEGER,
                accuracy REAL DEFAULT 0.0,
                correct_count INTEGER DEFAULT 0,
                wrong_count INTEGER DEFAULT 0,
                difficulty TEXT DEFAULT 'all',
                topic TEXT DEFAULT 'all',
                logged_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS phrases_bank (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                phrase TEXT NOT NULL,
                meaning TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS spot_errors_bank (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                question TEXT NOT NULL,
                options TEXT NOT NULL,
                answer TEXT NOT NULL,
                explanation TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT DEFAULT '',
                image_path TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS gamification_profile (
                user_id INTEGER PRIMARY KEY,
                xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                unlocked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, code)
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS weekly_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                week_start TEXT NOT NULL,
                target_questions INTEGER DEFAULT 70,
                target_correct INTEGER DEFAULT 50,
                current_questions INTEGER DEFAULT 0,
                current_correct INTEGER DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, week_start)
            )
        ''')
        # Light migration for old DB files
        for table, column_sql in [
            ("words", "user_id INTEGER"),
            ("quiz_sessions", "user_id INTEGER"),
            ("question_attempts", "user_id INTEGER"),
            ("quiz_sessions", "number_of_questions INTEGER DEFAULT 10"),
            ("quiz_sessions", "exam_start_time TEXT"),
            ("quiz_sessions", "total_duration_seconds INTEGER DEFAULT 500"),
            ("quiz_sessions", "status TEXT DEFAULT 'active'"),
            ("question_attempts", "word_key TEXT DEFAULT ''"),
            ("question_attempts", "options_json TEXT DEFAULT '[]'"),
            ("notes", "image_path TEXT DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_sql}")
            except sqlite3.OperationalError:
                pass
        # Seed default phrases / spot-error rows per user will be created lazily on first access.
        conn.commit()

def row_to_dict(row):
    return {
        'id':       row['id'],
        'word':     row['word'],
        'synonyms': json.loads(row['synonyms'] or '[]'),
        'antonyms': json.loads(row['antonyms'] or '[]'),
        'phrases':  json.loads(row['phrases']  or '[]'),
    }


def current_user_id():
    return session.get("user_id")


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        uid = current_user_id()
        if not uid:
            return jsonify({"error": "Unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapped


def get_current_user(conn):
    uid = current_user_id()
    if not uid:
        return None
    return conn.execute("SELECT id, username FROM users WHERE id=?", (uid,)).fetchone()


def _seed_user_banks_if_needed(conn, user_id):
    phrase_count = conn.execute("SELECT COUNT(*) AS c FROM phrases_bank WHERE user_id=?", (user_id,)).fetchone()["c"]
    if phrase_count == 0:
        phrases = [
            (user_id, "Burn the midnight oil", "To work late into the night"),
            (user_id, "Spill the beans", "To reveal a secret"),
            (user_id, "Hit the nail on the head", "To describe exactly the right thing"),
            (user_id, "Bite the bullet", "To face a difficult situation bravely"),
            (user_id, "A blessing in disguise", "Something good that seemed bad first"),
        ]
        conn.executemany("INSERT INTO phrases_bank (user_id, phrase, meaning) VALUES (?,?,?)", phrases)

    spot_count = conn.execute("SELECT COUNT(*) AS c FROM spot_errors_bank WHERE user_id=?", (user_id,)).fetchone()["c"]
    if spot_count == 0:
        spot_rows = [
            (
                user_id,
                "Each of the cadets (A) / were given a medal (B) / for exceptional courage. (C) / No error (D)",
                json.dumps(["A", "B", "C", "D"]),
                "B",
                "Subject 'Each' is singular, so 'was given' is correct."
            ),
            (
                user_id,
                "No sooner did he arrive (A) / when the bell rang (B) / in the examination hall. (C) / No error (D)",
                json.dumps(["A", "B", "C", "D"]),
                "B",
                "Correct pair is 'No sooner ... than'."
            ),
            (
                user_id,
                "The committee have submitted (A) / its final recommendation (B) / to the ministry. (C) / No error (D)",
                json.dumps(["A", "B", "C", "D"]),
                "A",
                "Use singular verb for collective noun in this context: 'has submitted'."
            ),
        ]
        conn.executemany(
            "INSERT INTO spot_errors_bank (user_id, question, options, answer, explanation) VALUES (?,?,?,?,?)",
            spot_rows,
        )


def _pick_distractors(words, correct, limit=3):
    pool = [w for w in words if w and w.lower() != (correct or "").lower()]
    random.shuffle(pool)
    picked = pool[:limit]
    fallback = ["Arduous", "Subtle", "Rigid", "Trivial", "Robust", "Fragile"]
    i = 0
    while len(picked) < limit and i < len(fallback):
        if fallback[i].lower() != (correct or "").lower():
            picked.append(fallback[i])
        i += 1
    return picked


def _build_syn_ant_questions(conn, user_id, category, count):
    rows = conn.execute("SELECT * FROM words WHERE user_id=? ORDER BY RANDOM()", (user_id,)).fetchall()
    words_pool = [r["word"] for r in rows if r["word"]]
    out = []
    for row in rows:
        word = row["word"]
        syn = json.loads(row["synonyms"] or "[]")
        ant = json.loads(row["antonyms"] or "[]")
        if category == "synonyms" and syn:
            correct = random.choice(syn)
            distractors = _pick_distractors(ant + syn + words_pool, correct)
            options = [correct] + distractors
            random.shuffle(options)
            out.append({
                "id": f"syn_{row['id']}",
                "category": "Synonyms",
                "difficulty": "moderate",
                "question": f'Choose the correct synonym of "{word.upper()}":',
                "options": options[:4],
                "answer": "ABCD"[options[:4].index(correct)],
                "explanation": f'"{correct}" is closest in meaning to "{word}".'
            })
            reverse_distractors = _pick_distractors(words_pool, word)
            reverse_options = [word] + reverse_distractors
            random.shuffle(reverse_options)
            out.append({
                "id": f"syn_rev_{row['id']}",
                "category": "Synonyms",
                "difficulty": "moderate",
                "question": f'"{correct}" is a synonym of which word?',
                "options": reverse_options[:4],
                "answer": "ABCD"[reverse_options[:4].index(word)],
                "explanation": f'"{correct}" matches "{word}".'
            })
        if category == "antonyms" and ant:
            correct = random.choice(ant)
            distractors = _pick_distractors(syn + ant + words_pool, correct)
            options = [correct] + distractors
            random.shuffle(options)
            out.append({
                "id": f"ant_{row['id']}",
                "category": "Antonyms",
                "difficulty": "moderate",
                "question": f'Choose the correct antonym of "{word.upper()}":',
                "options": options[:4],
                "answer": "ABCD"[options[:4].index(correct)],
                "explanation": f'"{correct}" is opposite in meaning to "{word}".'
            })
            reverse_distractors = _pick_distractors(words_pool, word)
            reverse_options = [word] + reverse_distractors
            random.shuffle(reverse_options)
            out.append({
                "id": f"ant_rev_{row['id']}",
                "category": "Antonyms",
                "difficulty": "moderate",
                "question": f'"{correct}" is an antonym of which word?',
                "options": reverse_options[:4],
                "answer": "ABCD"[reverse_options[:4].index(word)],
                "explanation": f'"{correct}" is opposite of "{word}".'
            })
        if len(out) >= count:
            break
    random.shuffle(out)
    return out[:count]


def _build_phrase_questions(conn, user_id, count):
    rows = conn.execute("SELECT * FROM phrases_bank WHERE user_id=? ORDER BY RANDOM()", (user_id,)).fetchall()
    all_meanings = [r["meaning"] for r in rows]
    out = []
    for row in rows:
        correct = row["meaning"]
        distractors = _pick_distractors(all_meanings, correct)
        options = [correct] + distractors
        random.shuffle(options)
        out.append({
            "id": f"phr_{row['id']}",
            "category": "Phrases & Idioms",
            "difficulty": "moderate",
            "question": f'Choose the correct meaning of "{row["phrase"]}":',
            "options": options[:4],
            "answer": "ABCD"[options[:4].index(correct)],
            "explanation": f'"{row["phrase"]}" means: {correct}.'
        })
        if len(out) >= count:
            break
    return out


def _build_spot_error_questions(conn, user_id, count):
    rows = conn.execute("SELECT * FROM spot_errors_bank WHERE user_id=? ORDER BY RANDOM()", (user_id,)).fetchall()
    out = []
    for row in rows[:count]:
        options = json.loads(row["options"] or '["A","B","C","D"]')
        out.append({
            "id": f"se_{row['id']}",
            "category": "Spot the Error",
            "difficulty": "hard",
            "question": row["question"],
            "options": options[:4],
            "answer": row["answer"],
            "explanation": row["explanation"] or "Check grammar agreement and connector usage."
        })
    return out


def _build_ai_general_questions(count, api_key_override=""):
    prompt = (
        "Generate hard CDS English MCQ questions. Return ONLY JSON array. "
        "Each item: question, options (4 strings), answer (A/B/C/D), explanation. "
        f"Need {count} questions."
    )
    raw = call_ai(prompt, api_key_override=api_key_override)
    raw = raw.replace("`json", "").replace("`", "").strip()
    raw = raw[raw.find("["): raw.rfind("]") + 1] if "[" in raw and "]" in raw else "[]"
    try:
        parsed = json.loads(raw)
    except Exception:
        raise RuntimeError("AI did not return valid JSON for quiz questions.")

    out = []
    for i, q in enumerate(parsed[:count]):
        opts = (q.get("options") or [])[:4]
        if len(opts) < 4:
            continue
        ans = (q.get("answer") or "").strip().upper()
        if ans not in {"A", "B", "C", "D"}:
            continue
        out.append({
            "id": f"gen_ai_{i}",
            "category": "General",
            "difficulty": "hard",
            "question": q.get("question", ""),
            "options": opts,
            "answer": ans,
            "explanation": q.get("explanation", "")
        })
    if len(out) < count:
        raise RuntimeError("AI returned too few valid quiz questions.")
    return out[:count]


def _build_category_questions(conn, user_id, category, count, api_key_override=""):
    category = (category or "").strip().lower()
    if category == "synonyms":
        return _build_syn_ant_questions(conn, user_id, "synonyms", count)
    if category == "antonyms":
        return _build_syn_ant_questions(conn, user_id, "antonyms", count)
    if category == "phrases":
        return _build_phrase_questions(conn, user_id, count)
    if category == "spot_error":
        return _build_spot_error_questions(conn, user_id, count)
    if category == "general":
        return _build_ai_general_questions(count, api_key_override=api_key_override)
    return []


# ── ROUTES ───────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/auth/register', methods=['POST'])
def register():
    data = request.json or {}
    username = (data.get('username') or '').strip().lower()
    password = data.get('password') or ''
    if len(username) < 3 or len(password) < 6:
        return jsonify({'error': 'Username/password too short'}), 400
    with get_db() as conn:
        try:
            cur = conn.execute(
                'INSERT INTO users (username, password_hash) VALUES (?, ?)',
                (username, generate_password_hash(password))
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return jsonify({'error': 'Username already exists'}), 409
        session['user_id'] = cur.lastrowid
    return jsonify({'ok': True, 'user': {'username': username}})


@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.json or {}
    username = (data.get('username') or '').strip().lower()
    password = data.get('password') or ''
    with get_db() as conn:
        row = conn.execute(
            'SELECT id, username, password_hash FROM users WHERE username=?',
            (username,)
        ).fetchone()
    if not row or not check_password_hash(row['password_hash'], password):
        return jsonify({'error': 'Invalid credentials'}), 401
    session['user_id'] = row['id']
    return jsonify({'ok': True, 'user': {'id': row['id'], 'username': row['username']}})


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/api/auth/me', methods=['GET'])
def me():
    with get_db() as conn:
        row = get_current_user(conn)
    if not row:
        return jsonify({'user': None})
    return jsonify({'user': {'id': row['id'], 'username': row['username']}})

# GET all words
@app.route('/api/words', methods=['GET'])
@login_required
def get_words():
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM words WHERE user_id=? ORDER BY word', (current_user_id(),)).fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route('/api/questions', methods=['GET'])
@login_required
def get_questions_by_category():
    category = (request.args.get("category") or "").strip().lower()
    count = max(1, min(100, int(request.args.get("count") or 20)))
    api_key_override = (request.headers.get("X-User-Api-Key") or "").strip()
    try:
        with get_db() as conn:
            _seed_user_banks_if_needed(conn, current_user_id())
            if category not in {"synonyms", "antonyms", "phrases", "spot_error", "general"}:
                return jsonify({"error": "Invalid category"}), 400
            questions = _build_category_questions(
                conn, current_user_id(), category, count, api_key_override=api_key_override
            )
            conn.commit()
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"category": category, "questions": questions})


@app.route('/api/questions/full-quiz', methods=['GET'])
@login_required
def get_full_quiz_questions():
    count = max(5, min(100, int(request.args.get("count") or 20)))
    each = max(1, count // 5)
    api_key_override = (request.headers.get("X-User-Api-Key") or "").strip()
    try:
        with get_db() as conn:
            _seed_user_banks_if_needed(conn, current_user_id())
            syn = _build_category_questions(conn, current_user_id(), "synonyms", each)
            ant = _build_category_questions(conn, current_user_id(), "antonyms", each)
            phr = _build_category_questions(conn, current_user_id(), "phrases", each)
            se = _build_category_questions(conn, current_user_id(), "spot_error", each)
            gen = _build_category_questions(
                conn, current_user_id(), "general", each, api_key_override=api_key_override
            )
            merged = syn + ant + phr + se + gen
            while len(merged) < count:
                merged.extend(
                    _build_category_questions(
                        conn, current_user_id(), "general", 1, api_key_override=api_key_override
                    )
                )
            random.shuffle(merged)
            conn.commit()
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"category": "full_quiz", "questions": merged[:count]})


@app.route('/api/quiz-sessions', methods=['POST'])
@login_required
def start_quiz_session():
    data = request.json or {}
    source = (data.get('source') or 'fallback').strip()
    mode = (data.get('mode') or 'mixed').strip()
    difficulty = (data.get('difficulty') or 'all').strip().lower()
    topic = (data.get('topic') or 'all').strip()
    number_of_questions = int(data.get('number_of_questions') or data.get('total_questions') or 10)

    with get_db() as conn:
        payload = create_session(
            conn=conn,
            user_id=current_user_id(),
            source=source,
            mode=mode,
            difficulty=difficulty,
            topic=topic,
            number_of_questions=number_of_questions,
        )
        conn.commit()
    return jsonify(payload), 201


@app.route('/api/quiz-sessions/<int:sid>/finish', methods=['POST'])
@login_required
def finish_quiz_session(sid):
    data = request.json or {}
    correct = int(data.get('correct_count') or 0)
    wrong = int(data.get('wrong_count') or 0)
    with get_db() as conn:
        status_payload = get_session_status(conn, current_user_id(), sid)
        if status_payload is None:
            return jsonify({'error': 'Not found'}), 404
        conn.execute('''
            UPDATE quiz_sessions
            SET correct_count=?, wrong_count=?, ended_at=CURRENT_TIMESTAMP, status=?
            WHERE id=? AND user_id=?
        ''', (
            correct,
            wrong,
            'auto_submitted' if status_payload['remaining_seconds'] <= 0 else 'completed',
            sid,
            current_user_id(),
        ))
        log_performance(
            conn,
            current_user_id(),
            sid,
            correct_count=correct,
            wrong_count=wrong,
            difficulty=(data.get('difficulty') or 'all'),
            topic=(data.get('topic') or 'all'),
        )
        add_xp(conn, current_user_id(), 50)
        update_weekly_progress(conn, current_user_id())
        evaluate_achievements(conn, current_user_id())
        conn.commit()
    return jsonify({'ok': True, 'remaining_seconds': status_payload['remaining_seconds']})


@app.route('/api/quiz-attempts', methods=['POST'])
@login_required
def record_attempt():
    data = request.json or {}
    is_correct = 1 if data.get('is_correct') else 0
    question_key = (data.get('question_key') or '').strip()
    question_text = (data.get('question_text') or '').strip()
    topic = (data.get('topic') or '').strip()
    difficulty = (data.get('difficulty') or '').strip()
    word_key = (data.get('word_key') or question_key).strip()
    word_text = (data.get('word_text') or question_text).strip()
    with get_db() as conn:
        conn.execute('''
            INSERT INTO question_attempts (
                user_id, session_id, question_key, question_text, topic, difficulty,
                selected_answer, correct_answer, options_json, is_correct, source, word_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            current_user_id(),
            data.get('session_id'),
            question_key,
            question_text,
            topic,
            difficulty,
            (data.get('selected_answer') or '').strip(),
            (data.get('correct_answer') or '').strip(),
            json.dumps(data.get('options') or []),
            is_correct,
            (data.get('source') or 'fallback').strip(),
            word_key,
        ))
        update_srs_item(
            conn=conn,
            user_id=current_user_id(),
            question_key=question_key,
            question_text=question_text,
            topic=topic,
            difficulty=difficulty,
            is_correct=bool(is_correct),
        )
        upsert_word_stats(
            conn=conn,
            user_id=current_user_id(),
            word_key=word_key,
            word_text=word_text or word_key,
            is_correct=bool(is_correct),
        )
        add_xp(conn, current_user_id(), 10 if bool(is_correct) else 2)
        update_weekly_progress(conn, current_user_id())
        evaluate_achievements(conn, current_user_id())
        conn.commit()
    return jsonify({'ok': True}), 201


@app.route('/api/progress', methods=['GET'])
@login_required
def progress():
    with get_db() as conn:
        summary_row = conn.execute('''
            SELECT
                COUNT(*) as attempts,
                SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) as correct_attempts,
                SUM(CASE WHEN is_correct = 0 THEN 1 ELSE 0 END) as wrong_attempts
            FROM question_attempts
            WHERE user_id=?
        ''', (current_user_id(),)).fetchone()

        sessions = conn.execute('''
            SELECT id, source, mode, difficulty, topic, total_questions, number_of_questions,
                   total_duration_seconds, exam_start_time, status, correct_count, wrong_count, started_at, ended_at
            FROM quiz_sessions
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT 20
        ''', (current_user_id(),)).fetchall()

        weak = conn.execute('''
            SELECT
                question_key,
                question_text,
                topic,
                difficulty,
                SUM(CASE WHEN is_correct = 0 THEN 1 ELSE 0 END) as wrong_count,
                SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) as correct_count
            FROM question_attempts
            WHERE user_id=?
            GROUP BY question_key, question_text, topic, difficulty
            HAVING wrong_count > 0
            ORDER BY (wrong_count - correct_count) DESC, wrong_count DESC
            LIMIT 15
        ''', (current_user_id(),)).fetchall()

    attempts = int(summary_row['attempts'] or 0)
    correct_attempts = int(summary_row['correct_attempts'] or 0)
    wrong_attempts = int(summary_row['wrong_attempts'] or 0)
    accuracy = round((correct_attempts / attempts) * 100, 1) if attempts else 0.0

    return jsonify({
        'summary': {
            'attempts': attempts,
            'correct_attempts': correct_attempts,
            'wrong_attempts': wrong_attempts,
            'accuracy': accuracy
        },
        'sessions': [dict(r) for r in sessions],
        'weak_questions': [dict(r) for r in weak]
    })


@app.route('/api/wrong-questions', methods=['GET'])
@login_required
def wrong_questions():
    with get_db() as conn:
        rows = conn.execute(
            '''
            SELECT
                question_key,
                MAX(question_text) AS question_text,
                MAX(topic) AS topic,
                MAX(difficulty) AS difficulty,
                MAX(correct_answer) AS correct_answer,
                MAX(options_json) AS options_json,
                SUM(CASE WHEN is_correct = 0 THEN 1 ELSE 0 END) AS wrong_count,
                SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) AS correct_count
            FROM question_attempts
            WHERE user_id=?
            GROUP BY question_key
            HAVING wrong_count > 0
            ORDER BY (wrong_count - correct_count) DESC, wrong_count DESC
            ''',
            (current_user_id(),),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/wrong-questions/quiz', methods=['GET'])
@login_required
def wrong_questions_quiz():
    limit = max(1, min(100, int(request.args.get("count") or 20)))
    with get_db() as conn:
        rows = conn.execute(
            '''
            SELECT
                question_key,
                MAX(question_text) AS question_text,
                MAX(topic) AS topic,
                MAX(difficulty) AS difficulty,
                MAX(correct_answer) AS correct_answer,
                MAX(options_json) AS options_json,
                SUM(CASE WHEN is_correct = 0 THEN 1 ELSE 0 END) AS wrong_count,
                SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) AS correct_count
            FROM question_attempts
            WHERE user_id=?
            GROUP BY question_key
            HAVING wrong_count > 0
            ORDER BY (wrong_count - correct_count) DESC, wrong_count DESC
            LIMIT ?
            ''',
            (current_user_id(), limit),
        ).fetchall()

    out = []
    for r in rows:
        try:
            opts = json.loads(r["options_json"] or "[]")
        except Exception:
            opts = []
        ans = (r["correct_answer"] or "").strip().upper()
        if len(opts) < 4 or ans not in {"A", "B", "C", "D"}:
            continue
        out.append({
            "id": r["question_key"],
            "category": "Wrong Questions",
            "difficulty": r["difficulty"] or "moderate",
            "question": r["question_text"] or "",
            "options": opts[:4],
            "answer": ans,
            "explanation": "From your wrong-question practice set.",
        })
    return jsonify({"questions": out, "count": len(out)})


@app.route('/api/review-queue', methods=['GET'])
@login_required
def review_queue():
    limit = int(request.args.get('limit') or 50)
    with get_db() as conn:
        items = get_due_review_items(conn, current_user_id(), limit=limit)
    return jsonify({"items": items, "count": len(items)})


@app.route('/api/daily-plan', methods=['GET'])
@login_required
def daily_plan():
    with get_db() as conn:
        plan = build_daily_plan(conn, current_user_id(), [])
    return jsonify({
        "weak": plan["weak"],
        "revision": plan["revision"],
        "new": plan["new"],
        "effective_difficulty": plan["effective_difficulty"],
    })


@app.route('/api/performance-trends', methods=['GET'])
@login_required
def performance_trends():
    with get_db() as conn:
        trends = get_performance_trends(conn, current_user_id())
    return jsonify(trends)


@app.route('/api/priority-words', methods=['GET'])
@login_required
def priority_words():
    limit = int(request.args.get('limit') or 20)
    with get_db() as conn:
        items = get_priority_words(conn, current_user_id(), limit=limit)
    return jsonify({"items": items})


@app.route('/api/gamification', methods=['GET'])
@login_required
def gamification():
    with get_db() as conn:
        data = gamification_summary(conn, current_user_id())
        conn.commit()
    return jsonify(data)


@app.route('/api/weekly-goal', methods=['PUT'])
@login_required
def update_weekly_goal():
    data = request.json or {}
    target_questions = max(10, min(300, int(data.get("target_questions") or 70)))
    target_correct = max(1, min(target_questions, int(data.get("target_correct") or 50)))
    with get_db() as conn:
        summary = gamification_summary(conn, current_user_id())
        ws = summary["weekly_goal"]["week_start"]
        conn.execute(
            """
            UPDATE weekly_goals
            SET target_questions=?, target_correct=?, updated_at=CURRENT_TIMESTAMP
            WHERE user_id=? AND week_start=?
            """,
            (target_questions, target_correct, current_user_id(), ws),
        )
        conn.commit()
        updated = gamification_summary(conn, current_user_id())
    return jsonify(updated["weekly_goal"])


@app.route('/api/quiz-sessions/<int:sid>/status', methods=['GET'])
@login_required
def quiz_session_status(sid):
    with get_db() as conn:
        payload = get_session_status(conn, current_user_id(), sid)
        if payload is None:
            return jsonify({"error": "Not found"}), 404
        conn.commit()
    return jsonify(payload)


@app.route('/uploads/<path:filename>', methods=['GET'])
@login_required
def serve_upload(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route('/api/notes', methods=['GET'])
@login_required
def list_notes():
    category = (request.args.get("category") or "").strip()
    with get_db() as conn:
        if category and category.lower() != "all":
            rows = conn.execute(
                "SELECT * FROM notes WHERE user_id=? AND category=? ORDER BY updated_at DESC",
                (current_user_id(), category),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM notes WHERE user_id=? ORDER BY updated_at DESC",
                (current_user_id(),),
            ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "category": r["category"],
            "title": r["title"],
            "body": r["body"],
            "image_url": f"/uploads/{r['image_path']}" if r["image_path"] else "",
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
    return jsonify(out)


@app.route('/api/notes', methods=['POST'])
@login_required
def create_note():
    if request.content_type and "multipart/form-data" in request.content_type:
        category = (request.form.get("category") or "Others").strip()
        title = (request.form.get("title") or "").strip()
        body = (request.form.get("body") or "").strip()
        file = request.files.get("image")
    else:
        data = request.json or {}
        category = (data.get("category") or "Others").strip()
        title = (data.get("title") or "").strip()
        body = (data.get("body") or "").strip()
        file = None
    if not title:
        return jsonify({"error": "Title is required"}), 400
    image_path = ""
    if file and file.filename:
        clean = secure_filename(file.filename)
        image_path = f"{uuid.uuid4().hex}_{clean}"
        file.save(os.path.join(UPLOAD_DIR, image_path))
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO notes (user_id, category, title, body, image_path)
            VALUES (?, ?, ?, ?, ?)
            """,
            (current_user_id(), category, title, body, image_path),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM notes WHERE id=? AND user_id=?", (cur.lastrowid, current_user_id())).fetchone()
    return jsonify({
        "id": row["id"],
        "category": row["category"],
        "title": row["title"],
        "body": row["body"],
        "image_url": f"/uploads/{row['image_path']}" if row["image_path"] else "",
    }), 201


@app.route('/api/notes/<int:nid>', methods=['PUT'])
@login_required
def update_note(nid):
    data = request.json or {}
    category = (data.get("category") or "Others").strip()
    title = (data.get("title") or "").strip()
    body = (data.get("body") or "").strip()
    if not title:
        return jsonify({"error": "Title is required"}), 400
    with get_db() as conn:
        conn.execute(
            """
            UPDATE notes
            SET category=?, title=?, body=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND user_id=?
            """,
            (category, title, body, nid, current_user_id()),
        )
        conn.commit()
    return jsonify({"ok": True})


@app.route('/api/notes/<int:nid>', methods=['DELETE'])
@login_required
def delete_note(nid):
    with get_db() as conn:
        row = conn.execute("SELECT image_path FROM notes WHERE id=? AND user_id=?", (nid, current_user_id())).fetchone()
        conn.execute("DELETE FROM notes WHERE id=? AND user_id=?", (nid, current_user_id()))
        conn.commit()
    if row and row["image_path"]:
        try:
            os.remove(os.path.join(UPLOAD_DIR, row["image_path"]))
        except OSError:
            pass
    return jsonify({"ok": True})


@app.route('/api/chat', methods=['POST'])
@login_required
def chat():
    data = request.json or {}
    message = (data.get('message') or '').strip()
    context = (data.get('context') or '').strip()
    if not message:
        return jsonify({'error': 'Message is required'}), 400

    prompt = (
        "You are a CDS English tutor for defence aspirants. "
        "Answer clearly and concisely with examples when useful. "
        f"Student context: {context}\n\nUser: {message}"
    )
    api_key_override = (request.headers.get("X-User-Api-Key") or "").strip()
    try:
        text = call_ai(prompt, api_key_override=api_key_override)
        return jsonify({'reply': text})
    except Exception as e:
        return jsonify({'error': str(e)}), 502

# POST add word
@app.route('/api/words', methods=['POST'])
@login_required
def add_word():
    data = request.json
    word = (data.get('word') or '').strip()
    if not word:
        return jsonify({'error': 'Word is required'}), 400
    synonyms = json.dumps(_normalize_term_list(data.get('synonyms', [])))
    antonyms = json.dumps(_normalize_term_list(data.get('antonyms', [])))
    phrases  = json.dumps(data.get('phrases',  []))
    try:
        with get_db() as conn:
            cur = conn.execute(
                'INSERT INTO words (user_id, word, synonyms, antonyms, phrases) VALUES (?,?,?,?,?)',
                (current_user_id(), word, synonyms, antonyms, phrases)
            )
            conn.commit()
            row = conn.execute('SELECT * FROM words WHERE id=? AND user_id=?', (cur.lastrowid, current_user_id())).fetchone()
        return jsonify(row_to_dict(row)), 201
    except sqlite3.IntegrityError:
        return jsonify({'error': f'"{word}" already exists'}), 409

# PUT update word
@app.route('/api/words/<int:wid>', methods=['PUT'])
@login_required
def update_word(wid):
    data = request.json
    word     = (data.get('word') or '').strip()
    synonyms = json.dumps(_normalize_term_list(data.get('synonyms', [])))
    antonyms = json.dumps(_normalize_term_list(data.get('antonyms', [])))
    phrases  = json.dumps(data.get('phrases',  []))
    with get_db() as conn:
        conn.execute(
            'UPDATE words SET word=?, synonyms=?, antonyms=?, phrases=? WHERE id=? AND user_id=?',
            (word, synonyms, antonyms, phrases, wid, current_user_id())
        )
        conn.commit()
        row = conn.execute('SELECT * FROM words WHERE id=? AND user_id=?', (wid, current_user_id())).fetchone()
    if row is None:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(row_to_dict(row))

# DELETE word
@app.route('/api/words/<int:wid>', methods=['DELETE'])
@login_required
def delete_word(wid):
    with get_db() as conn:
        conn.execute('DELETE FROM words WHERE id=? AND user_id=?', (wid, current_user_id()))
        conn.commit()
    return jsonify({'ok': True})

# ── MAIN ─────────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    print("\n✅  CDS Vocab App running at http://localhost:5000\n")
    app.run(
        debug=os.environ.get("FLASK_DEBUG", "true").lower() == "true",
        host=os.environ.get("FLASK_HOST", "127.0.0.1"),
        port=int(os.environ.get("FLASK_PORT", "5000")),
    )
