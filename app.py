from psycopg_pool import ConnectionPool
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
from google import genai
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from gtts import gTTS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect

import psycopg
from psycopg.rows import dict_row
import os
import base64
import io
import json
import calendar
from datetime import date, datetime, timedelta
# ===========================
# ENVIRONMENT
# ===========================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ENV_PATH = os.path.join(
    BASE_DIR,
    ".env"
)

load_dotenv(
    dotenv_path=ENV_PATH,
    override=True
)

GEMINI_API_KEY = os.getenv(
    "GEMINI_API_KEY",
    ""
).strip()


print(
    "Gemini API key status:",
    "LOADED" if GEMINI_API_KEY else "NOT FOUND"
)

print(
    "Gemini API key length:",
    len(GEMINI_API_KEY)
)

print(
    "Gemini API key loaded:",
    bool(GEMINI_API_KEY)
)

app = Flask(__name__)


# =========================================================
# SECURITY - SESSION HARDENING
# =========================================================

app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (
    os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
)

app.secret_key = os.getenv("SECRET_KEY")

if not app.secret_key:
    raise RuntimeError(
        "SECRET_KEY environment variable is required."
    )

csrf = CSRFProtect(app)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=(
        os.getenv(
            "SESSION_COOKIE_SECURE",
            "false"
        ).lower() == "true"
    ),
    SESSION_COOKIE_SAMESITE="Lax",
)

RATE_LIMIT_STORAGE_URI = (
    os.getenv("REDIS_URL", "").strip()
    or "memory://"
)

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],
    storage_uri=RATE_LIMIT_STORAGE_URI,
    strategy="fixed-window",
    headers_enabled=True,
    key_prefix="linguamind",
    in_memory_fallback_enabled=True,
)



# ===========================
# DATABASE CONNECTION POOL
# ===========================

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is required.")

DB_POOL_MIN = max(1, int(os.getenv("DB_POOL_MIN", "1")))
DB_POOL_MAX = max(DB_POOL_MIN, int(os.getenv("DB_POOL_MAX", "5")))

db_pool = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=DB_POOL_MIN,
    max_size=DB_POOL_MAX,
    kwargs={"row_factory": dict_row},
    open=True,
    close_returns=True,
    timeout=10.0,
    max_idle=300.0,
    max_lifetime=1800.0,
    name="linguamind-db-pool",
)


def get_db():
    return db_pool.getconn(timeout=10.0)


def create_subscription_system():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'users'
    """)

    columns = {
        row["column_name"]
        for row in cursor.fetchall()
    }

    if "plan" not in columns:
        cursor.execute("""
            ALTER TABLE users
            ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'
        """)

    if "subscription_status" not in columns:
        cursor.execute("""
            ALTER TABLE users
            ADD COLUMN subscription_status TEXT NOT NULL DEFAULT 'inactive'
        """)

    if "subscription_started_at" not in columns:
        cursor.execute("""
            ALTER TABLE users
            ADD COLUMN subscription_started_at TEXT
        """)

    if "subscription_expires_at" not in columns:
        cursor.execute("""
            ALTER TABLE users
            ADD COLUMN subscription_expires_at TEXT
        """)

    cursor.execute("""
        UPDATE users
        SET plan = 'free'
        WHERE plan IS NULL OR TRIM(plan) = ''
    """)

    cursor.execute("""
        UPDATE users
        SET subscription_status = 'inactive'
        WHERE subscription_status IS NULL
           OR TRIM(subscription_status) = ''
    """)

    conn.commit()
    conn.close()

def create_core_tables():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        full_name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    )
""")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS vocabulary (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            english_word TEXT NOT NULL,
            uzbek_word TEXT NOT NULL,
            example_sentence TEXT,
            status TEXT NOT NULL DEFAULT 'New',
            favorite INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    conn.commit()
    conn.close()


def add_calendar_month(moment):

    year = moment.year
    month = moment.month + 1

    if month == 13:
        month = 1
        year += 1

    last_day = calendar.monthrange(
        year,
        month
    )[1]

    day = min(
        moment.day,
        last_day
    )

    return moment.replace(
        year=year,
        month=month,
        day=day
    )


def add_calendar_year(moment):

    try:
        return moment.replace(
            year=moment.year + 1
        )

    except ValueError:
        return moment.replace(
            year=moment.year + 1,
            month=2,
            day=28
        )


def refresh_user_subscription(user_id):

    conn = get_db()

    user = conn.execute("""
        SELECT
            plan,
            subscription_status,
            subscription_started_at,
            subscription_expires_at
        FROM users
        WHERE id = %s
    """, (user_id,)).fetchone()

    if not user:
        conn.close()

        return {
            "plan": "free",
            "status": "inactive",
            "started_at": None,
            "expires_at": None,
            "is_pro": False
        }

    plan = (
        user["plan"]
        or "free"
    )

    status = (
        user["subscription_status"]
        or "inactive"
    )

    started_at = user[
        "subscription_started_at"
    ]

    expires_at = user[
        "subscription_expires_at"
    ]

    is_pro = (
        plan in {
            "pro_monthly",
            "pro_yearly"
        }
        and
        status == "active"
    )

    if is_pro and expires_at:

        try:
            expires_at_dt = datetime.fromisoformat(
                expires_at
            )

            now = datetime.utcnow()

            if now >= expires_at_dt:

                conn.execute("""
                    UPDATE users
                    SET
                        plan = 'free',
                        subscription_status = 'expired'
                    WHERE id = %s
                """, (user_id,))

                conn.commit()

                plan = "free"
                status = "expired"
                is_pro = False

        except ValueError:
            pass

    conn.close()

    return {
        "plan": plan,
        "status": status,
        "started_at": started_at,
        "expires_at": expires_at,
        "is_pro": is_pro
    }


@app.before_request
def keep_subscription_fresh():

    user_id = session.get(
        "user_id"
    )

    if user_id:
        refresh_user_subscription(
            user_id
        )


@app.route("/api/subscription/status")
def subscription_status_api():

    user_id = session.get(
        "user_id"
    )

    if not user_id:
        return jsonify({
            "error": "Login required."
        }), 401

    return jsonify(
        refresh_user_subscription(
            user_id
        )
    )


# ===========================
# PRO ACCESS CONTROL
# ===========================

def require_pro_api(feature_name):
    """
    Protect a premium API endpoint.

    Returns:
        None when the signed-in user has an active Pro plan.
        A Flask JSON response tuple otherwise.
    """

    user_id = session.get("user_id")

    if not user_id:
        return jsonify({
            "error": "Login required.",
            "code": "LOGIN_REQUIRED"
        }), 401

    subscription = refresh_user_subscription(
        user_id
    )

    if subscription.get("is_pro"):
        return None

    return jsonify({
        "error": (
            f"{feature_name} is a LinguaMind Pro feature. "
            "Upgrade to Pro to use it."
        ),
        "code": "PRO_REQUIRED",
        "upgrade_required": True,
        "feature": feature_name,
        "plan": subscription.get(
            "plan",
            "free"
        ),
        "status": subscription.get(
            "status",
            "inactive"
        ),
        "pricing_url": url_for(
            "pricing"
        )
    }), 403


def require_pro_page(feature_name):
    """
    Protect a premium page/action reached with a normal browser request.
    """

    user_id = session.get("user_id")

    if not user_id:
        return redirect(
            url_for("login")
        )

    subscription = refresh_user_subscription(
        user_id
    )

    if subscription.get("is_pro"):
        return None

    return redirect(
        url_for(
            "pricing",
            feature=feature_name,
            upgrade="required"
        )
    )


# ===========================
# LOCAL DEVELOPMENT PLAN TEST
# ===========================
# This route is only for local testing while debug mode is enabled.
# It does NOT process real payments and must not be used as payment proof.

@app.route(
    "/dev/subscription/<plan_name>"
)
def dev_subscription_switch(
    plan_name
):

    if (
        not app.debug
        or
        request.remote_addr not in {
            "127.0.0.1",
            "::1"
        }
    ):
        return jsonify({
            "error": "Not found."
        }), 404

    user_id = session.get(
        "user_id"
    )

    if not user_id:
        return redirect(
            url_for("login")
        )

    allowed_plans = {
        "free",
        "pro_monthly",
        "pro_yearly"
    }

    if plan_name not in allowed_plans:
        return jsonify({
            "error": "Invalid development plan."
        }), 400

    conn = get_db()

    if plan_name == "free":

        conn.execute(
            """
            UPDATE users
            SET
                plan = 'free',
                subscription_status = 'inactive',
                subscription_started_at = NULL,
                subscription_expires_at = NULL
            WHERE id = %s
            """,
            (user_id,)
        )

    else:

        started_at = datetime.utcnow().replace(
            microsecond=0
        )

        if plan_name == "pro_monthly":
            expires_at = add_calendar_month(
                started_at
            )
        else:
            expires_at = add_calendar_year(
                started_at
            )

        conn.execute(
            """
            UPDATE users
            SET
                plan = %s,
                subscription_status = 'active',
                subscription_started_at = %s,
                subscription_expires_at = %s
            WHERE id = %s
            """,
            (
                plan_name,
                started_at.isoformat(),
                expires_at.isoformat(),
                user_id
            )
        )

    conn.commit()
    conn.close()

    return redirect(
        url_for("settings")
    )



# ===========================
# GAMIFICATION / XP SYSTEM
# ===========================

def create_gamification_tables():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_progress (
            user_id INTEGER PRIMARY KEY,
            total_xp INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS xp_events (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            event_key TEXT NOT NULL,
            xp INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, event_key),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_xp_events_user_date
        ON xp_events(user_id, created_at)
    """)
    conn.commit()
    conn.close()


def ensure_user_progress(user_id):
    conn = get_db()
    cursor = conn.cursor()
    row = cursor.execute("SELECT total_xp FROM user_progress WHERE user_id = %s LIMIT 1", (user_id,)).fetchone()
    if row:
        total_xp = int(row["total_xp"] or 0)
        conn.close()
        return total_xp

    vocabulary_total = cursor.execute("SELECT COUNT(*) AS total FROM vocabulary WHERE user_id = %s", (user_id,)).fetchone()["total"]
    mastered_total = cursor.execute("SELECT COUNT(*) AS total FROM vocabulary WHERE user_id = %s AND status = 'Mastered'", (user_id,)).fetchone()["total"]
    quiz_total = cursor.execute("SELECT COUNT(*) AS total FROM quiz_results WHERE user_id = %s", (user_id,)).fetchone()["total"]
    completed_tasks = cursor.execute("SELECT COUNT(*) AS total FROM study_tasks WHERE user_id = %s AND status = 'completed'", (user_id,)).fetchone()["total"]

    starting_xp = int(vocabulary_total)*5 + int(mastered_total)*12 + int(quiz_total)*45 + int(completed_tasks)*30
    cursor.execute("INSERT INTO user_progress (user_id, total_xp) VALUES (%s, %s)", (user_id, starting_xp))
    conn.commit()
    conn.close()
    return starting_xp


def award_xp(user_id, amount, event_type, event_key):
    ensure_user_progress(user_id)
    amount = max(0, int(amount))
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO xp_events (user_id, event_type, event_key, xp)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT DO NOTHING
""", (user_id, event_type, event_key, amount))
    was_added = cursor.rowcount == 1
    if was_added:
        cursor.execute("""
            UPDATE user_progress
            SET total_xp = total_xp + %s, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s
        """, (amount, user_id))
    conn.commit()
    conn.close()
    return amount if was_added else 0


def record_daily_checkin(user_id):
    today_key = date.today().isoformat()
    return award_xp(user_id, 5, "daily_checkin", "daily_checkin:" + today_key)


def level_details(total_xp):
    total_xp = max(0, int(total_xp))
    level = 1
    level_start_xp = 0
    requirement = 200
    while total_xp >= level_start_xp + requirement:
        level_start_xp += requirement
        level += 1
        requirement = 200 + (level - 1) * 100
    current_level_xp = total_xp - level_start_xp
    progress_percent = round((current_level_xp / requirement) * 100) if requirement else 100
    titles = {1:"Starter",2:"Explorer",3:"Learner",4:"Scholar",5:"Achiever",6:"Expert",7:"Master"}
    return {
        "level": level,
        "title": titles.get(level, "Legend"),
        "total_xp": total_xp,
        "current_level_xp": current_level_xp,
        "level_requirement": requirement,
        "next_level_xp": level_start_xp + requirement,
        "progress_percent": max(0, min(100, progress_percent))
    }


def calculate_learning_streak(user_id):
    conn = get_db()
    cursor = conn.cursor()
    activity_dates = set()
    queries = [
        ("SELECT DISTINCT DATE(created_at) AS activity_date FROM xp_events WHERE user_id = %s AND created_at IS NOT NULL", "created_at"),
        ("SELECT DISTINCT DATE(created_at) AS activity_date FROM quiz_results WHERE user_id = %s AND created_at IS NOT NULL", "created_at"),
        ("SELECT DISTINCT DATE(completed_at) AS activity_date FROM study_tasks WHERE user_id = %s AND completed_at IS NOT NULL", "completed_at")
    ]
    for sql, _ in queries:
        for row in cursor.execute(sql, (user_id,)).fetchall():
            if row["activity_date"]:
                activity_dates.add(row["activity_date"])
    conn.close()
    if not activity_dates:
        return 0
    current_day = date.today()
    if current_day.isoformat() not in activity_dates:
        yesterday = current_day - timedelta(days=1)
        if yesterday.isoformat() not in activity_dates:
            return 0
        current_day = yesterday
    streak = 0
    while current_day.isoformat() in activity_dates:
        streak += 1
        current_day -= timedelta(days=1)
    return streak


def get_weekly_activity(user_id):
    conn = get_db()
    cursor = conn.cursor()
    today = date.today()
    days = []
    for offset in range(6, -1, -1):
        day_value = today - timedelta(days=offset)
        day_iso = day_value.isoformat()
        row = cursor.execute("""
            SELECT COALESCE(SUM(xp), 0) AS xp
            FROM xp_events
            WHERE user_id = %s AND DATE(created_at) = %s
        """, (user_id, day_iso)).fetchone()
        days.append({"date":day_iso, "label":day_value.strftime("%a"), "xp":int(row["xp"] or 0)})
    conn.close()
    max_xp = max([d["xp"] for d in days] or [0])
    for d in days:
        d["height"] = 10 if max_xp <= 0 else max(12, round((d["xp"] / max_xp) * 100))
    return days


def get_daily_missions(user_id):
    today_iso = date.today().isoformat()
    conn = get_db()
    cursor = conn.cursor()
    quiz_today = cursor.execute("SELECT COUNT(*) AS total FROM quiz_results WHERE user_id = %s AND DATE(created_at) = %s", (user_id, today_iso)).fetchone()["total"]
    task_today = cursor.execute("SELECT COUNT(*) AS total FROM study_tasks WHERE user_id = %s AND status = 'completed' AND completed_at IS NOT NULL AND DATE(completed_at) = %s", (user_id, today_iso)).fetchone()["total"]
    mastered_today = cursor.execute("SELECT COUNT(*) AS total FROM xp_events WHERE user_id = %s AND event_type = 'word_mastered' AND DATE(created_at) = %s", (user_id, today_iso)).fetchone()["total"]
    conn.close()
    missions = [
        {"key":"quiz","icon":"✓","title":"Complete one quiz","detail":"Grammar, Vocabulary, IELTS or SAT","reward":25,"done":int(quiz_today)>=1},
        {"key":"master_word","icon":"📚","title":"Master one word","detail":"Move a vocabulary word to Mastered","reward":20,"done":int(mastered_today)>=1},
        {"key":"study_task","icon":"◷","title":"Complete one study task","detail":"Finish a task from your Study Plan","reward":30,"done":int(task_today)>=1}
    ]
    for mission in missions:
        if mission["done"]:
            award_xp(user_id, mission["reward"], "daily_mission", "mission:" + mission["key"] + ":" + today_iso)
    return missions


def get_gamification_state(user_id):
    ensure_user_progress(user_id)
    missions = get_daily_missions(user_id)
    conn = get_db()
    cursor = conn.cursor()
    progress_row = cursor.execute("SELECT total_xp FROM user_progress WHERE user_id = %s LIMIT 1", (user_id,)).fetchone()
    total_xp = int(progress_row["total_xp"] if progress_row else 0)
    word_total = cursor.execute("SELECT COUNT(*) AS total FROM vocabulary WHERE user_id = %s", (user_id,)).fetchone()["total"]
    mastered_total = cursor.execute("SELECT COUNT(*) AS total FROM vocabulary WHERE user_id = %s AND status = 'Mastered'", (user_id,)).fetchone()["total"]
    quiz_total = cursor.execute("SELECT COUNT(*) AS total FROM quiz_results WHERE user_id = %s", (user_id,)).fetchone()["total"]
    conn.close()
    streak = calculate_learning_streak(user_id)
    state = level_details(total_xp)
    state.update({
        "streak": streak,
        "weekly_activity": get_weekly_activity(user_id),
        "missions": missions,
        "missions_done": sum(1 for m in missions if m["done"]),
        "missions_total": len(missions),
        "mastered_total": int(mastered_total),
        "quiz_total": int(quiz_total),
        "achievements": [
            {"icon":"🚀","title":"First Steps","detail":"Earn your first XP","unlocked":total_xp>0},
            {"icon":"📚","title":"Word Collector","detail":"Save 50 words","unlocked":int(word_total)>=50},
            {"icon":"🏆","title":"Quiz Challenger","detail":"Complete 10 quizzes","unlocked":int(quiz_total)>=10},
            {"icon":"🔥","title":"7 Day Streak","detail":"Learn 7 days in a row","unlocked":streak>=7}
        ]
    })
    return state


# ===========================
# AI HISTORY DATABASE TABLES
# ===========================

def create_ai_history_tables():

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ai_chats (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT 'New Chat',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ai_messages (
            id SERIAL PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chat_id) REFERENCES ai_chats(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_ai_chats_user
        ON ai_chats(user_id)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_ai_messages_chat
        ON ai_messages(chat_id)
    """)

    conn.commit()
    conn.close()

# ===========================
# QUIZ DATABASE TABLES

# ===========================
# AI STUDY PLANNER TABLES
# ===========================

def create_study_plan_tables():

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS study_profiles (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL UNIQUE,
            main_goal TEXT NOT NULL DEFAULT 'general_english',
            current_level TEXT NOT NULL DEFAULT 'not_sure',
            daily_minutes INTEGER NOT NULL DEFAULT 60,
            days_per_week INTEGER NOT NULL DEFAULT 6,
            has_exam INTEGER NOT NULL DEFAULT 0,
            exam_type TEXT,
            exam_date TEXT,
            target_score TEXT,
            weak_areas TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS study_plans (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            period_type TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            coach_message TEXT,
            goal TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            exam_date TEXT,
            target_score TEXT,
            plan_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS study_tasks (
            id SERIAL PRIMARY KEY,
            plan_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            task_date TEXT NOT NULL,
            period_label TEXT,
            title TEXT NOT NULL,
            description TEXT,
            category TEXT NOT NULL,
            minutes INTEGER NOT NULL DEFAULT 20,
            action_type TEXT NOT NULL DEFAULT 'ai_teacher',
            difficulty TEXT NOT NULL DEFAULT 'mixed',
            status TEXT NOT NULL DEFAULT 'pending',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY (plan_id) REFERENCES study_plans(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_study_plans_user_status
        ON study_plans(user_id, status)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_study_tasks_user_date
        ON study_tasks(user_id, task_date)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_study_tasks_plan
        ON study_tasks(plan_id)
    """)

    conn.commit()
    conn.close()

# ===========================

def create_quiz_tables():

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quiz_results (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            difficulty TEXT NOT NULL,
            score INTEGER NOT NULL,
            total_questions INTEGER NOT NULL,
            percentage INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_quiz_results_user
        ON quiz_results(user_id)
    """)

    conn.commit()
    conn.close()


# HOME PAGE

@app.route("/")
def home():

    # Main LinguaMind entry point.
    # If the user is already signed in, show the full Dashboard V4
    # directly on http://127.0.0.1:8000/ (and later on the real domain).
    if "user_id" in session:
        return dashboard()

    # Logged-out visitors still see the public landing page.
    return render_template("index.html")




# REGISTER

@app.route("/register", methods=["GET", "POST"])
def register():


    if request.method == "POST":


        full_name = request.form["full_name"]

        email = request.form["email"]

        password = request.form["password"]



        conn = get_db()

        cursor = conn.cursor()



        cursor.execute(
            "SELECT * FROM users WHERE email=%s",
            (email,)
        )


        existing_user = cursor.fetchone()



        if existing_user:

            conn.close()

            return render_template(
                "auth_notice.html",
                notice_type="error",
                eyebrow="ACCOUNT ALREADY EXISTS",
                title="This email is already registered",
                message=(
                    "You already have a LinguaMind account with this email. "
                    "Sign in to continue learning, or go back and use another email."
                ),
                primary_label="Sign in →",
                primary_url=url_for("login"),
                secondary_label="Use another email",
                secondary_url=url_for("register")
            )




        hashed_password = generate_password_hash(password)



        cursor.execute(
            """
            INSERT INTO users
            (full_name,email,password)

            VALUES(%s,%s,%s)
            """,

            (
                full_name,
                email,
                hashed_password
            )

        )



        conn.commit()

        conn.close()



        return redirect(url_for("login"))



    return render_template("register.html")






# LOGIN

@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():

    if request.method == "POST":

        email = request.form["email"].strip()
        password = request.form["password"]

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT *
            FROM users
            WHERE LOWER(email) = LOWER(%s)
            LIMIT 1
        """, (email,))

        user = cursor.fetchone()
        conn.close()

        if user and check_password_hash(
            user["password"],
            password
        ):
            session["user_id"] = user["id"]
            session.permanent = True
            session["username"] = user["full_name"]

            if (
                bool(user.get("is_admin"))
                and user["email"].lower()
                == "baxtiyorxamidov941@gmail.com"
            ):
                return redirect(
                    url_for("admin_dashboard")
                )

            return redirect(
                url_for("dashboard")
            )

        return render_template(
            "auth_notice.html",
            notice_type="error",
            eyebrow="SIGN IN FAILED",
            title="Email or password is incorrect",
            message=(
                "We could not sign you in with those details. "
                "Check your email and password, then try again."
            ),
            primary_label="Try again →",
            primary_url=url_for("login"),
            secondary_label="Create account",
            secondary_url=url_for("register")
        )

    return render_template("login.html")


# DASHBOARD
# ===========================

# ===========================
# DASHBOARD
# ===========================

@app.route("/dashboard")
def dashboard():

    if "user_id" not in session:
        return redirect(
            url_for("login")
        )


    user_id = session["user_id"]

    conn = get_db()
    cursor = conn.cursor()


    # ===========================
    # VOCABULARY STATS
    # ===========================

    cursor.execute(
        """
        SELECT COUNT(*) AS total
        FROM vocabulary
        WHERE user_id = %s
        """,
        (user_id,)
    )

    total = cursor.fetchone()["total"]


    cursor.execute(
        """
        SELECT COUNT(*) AS total
        FROM vocabulary
        WHERE user_id = %s
        AND status = 'New'
        """,
        (user_id,)
    )

    new_count = cursor.fetchone()["total"]


    cursor.execute(
        """
        SELECT COUNT(*) AS total
        FROM vocabulary
        WHERE user_id = %s
        AND status = 'Learning'
        """,
        (user_id,)
    )

    learning_count = cursor.fetchone()["total"]


    cursor.execute(
        """
        SELECT COUNT(*) AS total
        FROM vocabulary
        WHERE user_id = %s
        AND status = 'Mastered'
        """,
        (user_id,)
    )

    mastered_count = cursor.fetchone()["total"]


    cursor.execute(
        """
        SELECT COUNT(*) AS total
        FROM vocabulary
        WHERE user_id = %s
        AND favorite = 1
        """,
        (user_id,)
    )

    favorites_count = cursor.fetchone()["total"]


    if total > 0:

        progress = round(
            (
                mastered_count /
                total
            ) * 100
        )

    else:

        progress = 0


    # ===========================
    # QUIZ STATS
    # ===========================

    cursor.execute(
        """
        SELECT
            COUNT(*) AS completed,
            COALESCE(
                ROUND(AVG(percentage)),
                0
            ) AS average_score,
            COALESCE(
                MAX(percentage),
                0
            ) AS best_score
        FROM quiz_results
        WHERE user_id = %s
        """,
        (user_id,)
    )

    quiz_stats = cursor.fetchone()


    quizzes_completed = quiz_stats["completed"]

    average_quiz_score = quiz_stats["average_score"]

    best_quiz_score = quiz_stats["best_score"]


    # ===========================
    # LATEST QUIZ
    # ===========================

    cursor.execute(
        """
        SELECT
            category,
            difficulty,
            score,
            total_questions,
            percentage,
            created_at
        FROM quiz_results
        WHERE user_id = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,)
    )

    latest_quiz = cursor.fetchone()


    # ===========================
    # TODAY'S STUDY PLAN
    # ===========================

    today_iso = date.today().isoformat()

    cursor.execute(
        """
        SELECT
            id,
            title,
            summary,
            period_type,
            exam_date,
            target_score
        FROM study_plans
        WHERE user_id = %s
        AND status = 'active'
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,)
    )

    active_plan = cursor.fetchone()

    today_tasks_total = 0
    today_tasks_done = 0

    if active_plan:

        cursor.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(
                    SUM(
                        CASE
                            WHEN status = 'completed'
                            THEN 1
                            ELSE 0
                        END
                    ),
                    0
                ) AS done
            FROM study_tasks
            WHERE user_id = %s
            AND plan_id = %s
            AND task_date = %s
            """,
            (
                user_id,
                active_plan["id"],
                today_iso
            )
        )

        today_plan_stats = cursor.fetchone()

        today_tasks_total = (
            today_plan_stats["total"]
            if today_plan_stats
            else 0
        )

        today_tasks_done = (
            today_plan_stats["done"]
            if today_plan_stats
            else 0
        )


    conn.close()


    record_daily_checkin(
        user_id
    )

    gamification = get_gamification_state(
        user_id
    )


    return render_template(
        "dashboard.html",

        total=total,

        new_count=new_count,

        learning_count=
            learning_count,

        mastered_count=
            mastered_count,

        favorites_count=
            favorites_count,

        progress=progress,

        quizzes_completed=
            quizzes_completed,

        average_quiz_score=
            average_quiz_score,

        best_quiz_score=
            best_quiz_score,

        latest_quiz=
            latest_quiz,

        active_plan=
            active_plan,

        today_tasks_total=
            today_tasks_total,

        today_tasks_done=
            today_tasks_done,

        gamification=
            gamification
    )

@app.route("/vocabulary", methods=["GET", "POST"])
def vocabulary():

    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    cursor = conn.cursor()

    if request.method == "POST":

        english_word = request.form["english_word"]
        uzbek_word = request.form["uzbek_word"]
        example_sentence = request.form["example_sentence"]

        cursor.execute(
            """
            INSERT INTO vocabulary
            (
                user_id,
                english_word,
                uzbek_word,
                example_sentence
            )

            VALUES (%s, %s, %s, %s)
            """,
            (
                session["user_id"],
                english_word,
                uzbek_word,
                example_sentence
            )
        )

        conn.commit()

    search = request.args.get("search", "")

    if search:

        cursor.execute(
            """
            SELECT *

            FROM vocabulary

            WHERE user_id=%s

            AND
            (
                english_word LIKE %s
                OR
                uzbek_word LIKE %s
            )

            ORDER BY id DESC
            """,
            (
                session["user_id"],
                "%" + search + "%",
                "%" + search + "%"
            )
        )

    else:

        cursor.execute(
            """
            SELECT *

            FROM vocabulary

            WHERE user_id=%s

            ORDER BY id DESC
            """,
            (
                session["user_id"],
            )
        )

    words = cursor.fetchall()

    conn.close()

    return render_template(
        "vocabulary.html",
        words=words,
        search=search
    )
    # ===========================
# DELETE WORD
# ===========================

@app.route("/delete_word/<int:word_id>")
def delete_word(word_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        DELETE FROM vocabulary
        WHERE id=%s AND user_id=%s
        """,
        (word_id, session["user_id"])
    )

    conn.commit()
    conn.close()

    return redirect(url_for("vocabulary"))


@app.route("/favorite/<int:word_id>")
def favorite(word_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE vocabulary
        SET favorite =
        CASE
            WHEN favorite = 1 THEN 0
            ELSE 1
        END
        WHERE id=%s AND user_id=%s
        """,
        (word_id, session["user_id"])
    )

    conn.commit()
    conn.close()

    return redirect(request.referrer or url_for("vocabulary"))
    # ===========================
# RUN APP
# ===========================
# ===========================
# LOGOUT
# ===========================
# ===========================
# ===========================
# ===========================
# SET WORD STATUS
# ===========================

@app.route(
    "/set_status/<int:word_id>/<status>"
)
def set_status(
    word_id,
    status
):

    if "user_id" not in session:
        return redirect(
            url_for("login")
        )

    allowed_status = {
        "New",
        "Learning",
        "Mastered"
    }

    if status not in allowed_status:
        return redirect(
            url_for("vocabulary")
        )

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE vocabulary
        SET status = %s
        WHERE id = %s
        AND user_id = %s
        """,
        (
            status,
            word_id,
            session["user_id"]
        )
    )

    conn.commit()
    conn.close()

    if status == "Mastered":
        award_xp(
            session["user_id"],
            15,
            "word_mastered",
            "word_mastered:" + str(word_id)
        )

    return redirect(
        url_for("vocabulary")
    )


# ===========================
# EDIT WORD
# ===========================

@app.route(
    "/edit_word/<int:word_id>",
    methods=["GET", "POST"]
)
def edit_word(
    word_id
):

    if "user_id" not in session:
        return redirect(
            url_for("login")
        )

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT *
        FROM vocabulary
        WHERE id = %s
        AND user_id = %s
        LIMIT 1
        """,
        (
            word_id,
            session["user_id"]
        )
    )

    word = cursor.fetchone()

    if not word:

        conn.close()

        return redirect(
            url_for("vocabulary")
        )

    if request.method == "POST":

        english_word = str(
            request.form.get(
                "english_word",
                ""
            )
        ).strip()

        uzbek_word = str(
            request.form.get(
                "uzbek_word",
                ""
            )
        ).strip()

        example_sentence = str(
            request.form.get(
                "example_sentence",
                ""
            )
        ).strip()

        status = str(
            request.form.get(
                "status",
                word["status"]
            )
        ).strip()

        if not english_word or not uzbek_word:

            conn.close()

            return render_template(
                "edit_word.html",
                word=word,
                error=(
                    "English and Uzbek words "
                    "cannot be empty."
                )
            )

        allowed_status = {
            "New",
            "Learning",
            "Mastered"
        }

        if status not in allowed_status:
            status = "New"

        cursor.execute(
            """
            UPDATE vocabulary
            SET
                english_word = %s,
                uzbek_word = %s,
                example_sentence = %s,
                status = %s
            WHERE id = %s
            AND user_id = %s
            """,
            (
                english_word,
                uzbek_word,
                example_sentence,
                status,
                word_id,
                session["user_id"]
            )
        )

        conn.commit()
        conn.close()

        return redirect(
            url_for("vocabulary")
        )

    conn.close()

    return render_template(
        "edit_word.html",
        word=word
    )


# ===========================
# ADD WORD
# ===========================

@app.route("/add_word", methods=["GET", "POST"])
def add_word():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":

        english_word = request.form.get(
            "english_word",
            ""
        ).strip()

        uzbek_word = request.form.get(
            "uzbek_word",
            ""
        ).strip()

        example_sentence = request.form.get(
            "example_sentence",
            ""
        ).strip()

        status = request.form.get(
            "status",
            "New"
        )

        if not english_word or not uzbek_word:
            return redirect(url_for("add_word"))

        allowed_status = [
            "New",
            "Learning",
            "Mastered"
        ]

        if status not in allowed_status:
            status = "New"

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO vocabulary
            (
                user_id,
                english_word,
                uzbek_word,
                example_sentence,
                status
            )
            VALUES (%s, %s, %s, %s, %s)
RETURNING id
            """,
            (
                session["user_id"],
                english_word,
                uzbek_word,
                example_sentence,
                status
            )
        )

        word_id = cursor.fetchone()["id"]

        conn.commit()
        conn.close()

        award_xp(
            session["user_id"],
            8,
            "word_added",
            "word_added:" + str(word_id)
        )

        if status == "Mastered":
            award_xp(
                session["user_id"],
                15,
                "word_mastered",
                "word_mastered:" + str(word_id)
            )

        return redirect(url_for("vocabulary"))

    return render_template("add_word.html")


# ===========================
# AI TEACHER
# ===========================

@app.route("/ai_teacher")
def ai_teacher():

    if "user_id" not in session:
        return redirect(url_for("login"))

    return render_template("ai_teacher.html")

# ===========================
# GEMINI AI TEACHER API
# ===========================

# ===========================
# GEMINI AI TEACHER API
# ===========================

# ===========================
# GEMINI AI TEACHER API
# ===========================

# ===========================
# AI CHAT HISTORY
# ===========================

@app.route("/api/ai_chats", methods=["GET", "POST", "DELETE"])
def ai_chats_api():

    if "user_id" not in session:
        return jsonify({
            "error": "Login required."
        }), 401

    user_id = session["user_id"]


    # ===========================
    # GET ALL CHATS
    # ===========================

    if request.method == "GET":

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                id,
                title,
                created_at,
                updated_at
            FROM ai_chats
            WHERE user_id = %s
            ORDER BY updated_at DESC, id DESC
            """,
            (user_id,)
        )

        rows = cursor.fetchall()
        conn.close()

        chats = []

        for row in rows:
            chats.append({
                "id": row["id"],
                "title": row["title"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"]
            })

        return jsonify({
            "chats": chats
        })


    # ===========================
    # CREATE NEW CHAT
    # ===========================

    if request.method == "POST":

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO ai_chats (
                user_id,
                title
            )
            VALUES (%s, %s)
RETURNING id
            """,
            (
                user_id,
                "New Chat"
            )
        )

        chat_id = cursor.fetchone()["id"]

        conn.commit()
        conn.close()

        return jsonify({
            "success": True,
            "chat_id": chat_id,
            "title": "New Chat"
        })


    # ===========================
    # DELETE ALL CHATS
    # ===========================

    if request.method == "DELETE":

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            """
            DELETE FROM ai_messages
            WHERE chat_id IN (
                SELECT id
                FROM ai_chats
                WHERE user_id = %s
            )
            """,
            (user_id,)
        )

        cursor.execute(
            """
            DELETE FROM ai_chats
            WHERE user_id = %s
            """,
            (user_id,)
        )

        conn.commit()
        conn.close()

        return jsonify({
            "success": True
        })


# ===========================
# SINGLE CHAT
# ===========================

@app.route(
    "/api/ai_chats/<int:chat_id>",
    methods=["GET", "DELETE"]
)
def ai_single_chat_api(chat_id):

    if "user_id" not in session:
        return jsonify({
            "error": "Login required."
        }), 401

    user_id = session["user_id"]

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, title
        FROM ai_chats
        WHERE id = %s
        AND user_id = %s
        """,
        (
            chat_id,
            user_id
        )
    )

    chat = cursor.fetchone()


    if not chat:

        conn.close()

        return jsonify({
            "error": "Chat not found."
        }), 404


    # ===========================
    # LOAD CHAT
    # ===========================

    if request.method == "GET":

        cursor.execute(
            """
            SELECT
                id,
                role,
                content,
                created_at
            FROM ai_messages
            WHERE chat_id = %s
            AND user_id = %s
            ORDER BY id ASC
            """,
            (
                chat_id,
                user_id
            )
        )

        rows = cursor.fetchall()

        messages = []

        for row in rows:

            messages.append({
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "created_at": row["created_at"]
            })

        result = {
            "id": chat["id"],
            "title": chat["title"],
            "messages": messages
        }

        conn.close()

        return jsonify(result)


    # ===========================
    # DELETE ONE CHAT
    # ===========================

    if request.method == "DELETE":

        cursor.execute(
            """
            DELETE FROM ai_messages
            WHERE chat_id = %s
            AND user_id = %s
            """,
            (
                chat_id,
                user_id
            )
        )

        cursor.execute(
            """
            DELETE FROM ai_chats
            WHERE id = %s
            AND user_id = %s
            """,
            (
                chat_id,
                user_id
            )
        )

        conn.commit()
        conn.close()

        return jsonify({
            "success": True
        })


# ===========================
# GEMINI AI TEACHER
# ===========================

@app.route("/api/ai_teacher", methods=["POST"])
def ai_teacher_api():

    if "user_id" not in session:

        return jsonify({
            "reply": "Please log in first."
        }), 401


    if not GEMINI_API_KEY:

        return jsonify({
            "reply": "Gemini API key was not found."
        }), 500


    user_id = session["user_id"]

    data = request.get_json(
        silent=True
    ) or {}


    message = str(
        data.get(
            "message",
            ""
        )
    ).strip()


    mode = str(
        data.get(
            "mode",
            "general"
        )
    ).strip().lower()


    chat_id = data.get(
        "chat_id"
    )


    if not message:

        return jsonify({
            "reply": "Please write a message first."
        }), 400


    allowed_modes = {
        "general",
        "grammar",
        "vocabulary",
        "conversation",
        "ielts",
        "sat",
        "sat_math"
    }


    if mode not in allowed_modes:
        mode = "general"


    try:

        chat_id = (
            int(chat_id)
            if chat_id is not None
            else None
        )

    except (
        TypeError,
        ValueError
    ):

        chat_id = None


    # ===========================
    # CHAT + HISTORY
    # ===========================

    conn = get_db()
    cursor = conn.cursor()

    chat = None
    created_new_chat = False


    if chat_id:

        cursor.execute(
            """
            SELECT id, title
            FROM ai_chats
            WHERE id = %s
            AND user_id = %s
            LIMIT 1
            """,
            (
                chat_id,
                user_id
            )
        )

        chat = cursor.fetchone()


    if chat:

        chat_title = chat["title"]

    else:

        cursor.execute(
            """
            INSERT INTO ai_chats (
                user_id,
                title
            )
            VALUES (%s, %s)
RETURNING id
            """,
            (
                user_id,
                "New Chat"
            )
        )

        chat_id = cursor.fetchone()["id"]
        chat_title = "New Chat"
        created_new_chat = True

        conn.commit()


    cursor.execute(
        """
        SELECT role, content
        FROM ai_messages
        WHERE chat_id = %s
        AND user_id = %s
        ORDER BY id DESC
        LIMIT 8
        """,
        (
            chat_id,
            user_id
        )
    )

    previous_messages = list(
        reversed(
            cursor.fetchall()
        )
    )


    history_lines = []

    for item in previous_messages:

        if item["role"] == "user":

            history_lines.append(
                "Student: "
                + item["content"]
            )

        elif item["role"] == "assistant":

            history_lines.append(
                "Teacher: "
                + item["content"]
            )


    history_text = "\n".join(
        history_lines
    )


    # ===========================
    # TEACHER MODES
    # ===========================

    mode_instructions = {

        "general": """
Help with general English learning.
Support grammar, vocabulary, speaking,
writing, IELTS and SAT when useful.
""",

        "grammar": """
Focus on English grammar.

If the learner makes a mistake:
- show the corrected sentence,
- explain the mistake clearly,
- give useful examples.
""",

        "vocabulary": """
Focus on useful vocabulary.

When useful include:
- English word,
- Uzbek meaning,
- simple definition,
- example sentence,
- synonym or collocation.
""",

        "conversation": """
Act as a natural English conversation partner.

Keep the conversation moving.
Ask useful follow-up questions.
Correct important mistakes politely.
""",

        "ielts": """
Act as an IELTS teacher.

Help with:
- Speaking,
- Writing,
- Reading,
- grammar,
- vocabulary.

Use realistic IELTS-style practice.
""",

        "sat": """
Act as a Digital SAT Reading and Writing teacher.

Help with:
- Main Idea,
- Vocabulary in Context,
- Transitions,
- Grammar,
- Rhetorical Synthesis,
- Standard English Conventions.
""",

        "sat_math": """
Act as a Digital SAT Math tutor.

Help with:
- Algebra,
- Advanced Math,
- Problem Solving and Data Analysis,
- Geometry,
- Trigonometry.

Teach step by step.
Check the learner's work.
Explain mistakes clearly.
Do not jump directly to the final answer
when guided practice is more useful.
"""
    }


    # ===========================
    # STUDY PLAN CONTEXT
    # ===========================

    study_plan_context = (
        "No active study-plan task is selected."
    )

    active_study_task_id = session.get(
        "active_study_task_id"
    )


    if active_study_task_id:

        cursor.execute(
            """
            SELECT
                st.title,
                st.description,
                st.category,
                st.minutes,
                st.difficulty,
                sp.title AS plan_title,
                sp.goal,
                sp.exam_date,
                sp.target_score
            FROM study_tasks AS st
            JOIN study_plans AS sp
                ON sp.id = st.plan_id
            WHERE st.id = %s
            AND st.user_id = %s
            AND sp.status = 'active'
            LIMIT 1
            """,
            (
                active_study_task_id,
                user_id
            )
        )

        active_task = cursor.fetchone()

        if active_task:

            study_plan_context = f"""
The learner opened AI Teacher from an active LinguaMind Study Plan.

PLAN:
{active_task["plan_title"]}

CURRENT TASK:
{active_task["title"]}

TASK DESCRIPTION:
{active_task["description"] or "No extra description."}

CATEGORY:
{active_task["category"]}

EXPECTED SESSION LENGTH:
{active_task["minutes"]} minutes

DIFFICULTY:
{active_task["difficulty"]}

PLAN GOAL:
{active_task["goal"]}

EXAM DATE:
{active_task["exam_date"] or "No exam date"}

TARGET SCORE:
{active_task["target_score"] or "No target score"}

Guide the learner through THIS task.
Actively teach, ask questions, give practice,
check answers and continue the planned skill.

If the category is SAT Math,
teach SAT Math even if the page mode
is General or SAT.
"""

    else:

        today_iso = date.today().isoformat()

        cursor.execute(
            """
            SELECT
                st.title,
                st.category,
                st.minutes
            FROM study_tasks AS st
            JOIN study_plans AS sp
                ON sp.id = st.plan_id
            WHERE st.user_id = %s
            AND st.task_date = %s
            AND st.status != 'completed'
            AND sp.status = 'active'
            ORDER BY
                st.sort_order ASC,
                st.id ASC
            LIMIT 3
            """,
            (
                user_id,
                today_iso
            )
        )

        today_tasks = cursor.fetchall()

        if today_tasks:

            today_lines = []

            for task in today_tasks:

                today_lines.append(
                    "- "
                    + task["title"]
                    + " ("
                    + task["category"]
                    + ", "
                    + str(task["minutes"])
                    + " min)"
                )

            study_plan_context = (
                "Today's active LinguaMind Study Plan tasks:\n"
                + "\n".join(
                    today_lines
                )
            )


    conn.close()


    teacher_prompt = f"""
You are LinguaMind AI Teacher.

You are a friendly, accurate and practical
personal teacher.

CURRENT MODE:
{mode.upper()}

MODE INSTRUCTIONS:
{mode_instructions[mode]}

GENERAL RULES:
- Explain clearly.
- If the learner writes in Uzbek, you may explain in Uzbek.
- Keep English examples in English.
- Correct English mistakes accurately.
- Use Markdown when useful.
- Keep answers focused and not unnecessarily long.
- Remember the recent conversation below.
- Behave like a real personal teacher.
- When a Study Plan task is active, follow that task.

CURRENT STUDY PLAN CONTEXT:
{study_plan_context}

RECENT CONVERSATION:
{history_text}

CURRENT STUDENT MESSAGE:
{message}

Answer now as LinguaMind AI Teacher.
"""


    try:

        client = genai.Client(
            api_key=GEMINI_API_KEY,
            http_options={
                "timeout": 120000
            }
        )


        response = client.interactions.create(
            model="gemini-3.6-flash",
            input=teacher_prompt
        )


        reply = (
            getattr(
                response,
                "output_text",
                ""
            )
            or
            ""
        ).strip()


        if not reply:

            raise RuntimeError(
                "Gemini returned an empty response."
            )


        # ===========================
        # SAVE AFTER SUCCESS ONLY
        # ===========================

        conn = get_db()
        cursor = conn.cursor()


        cursor.execute(
            """
            INSERT INTO ai_messages (
                chat_id,
                user_id,
                role,
                content
            )
            VALUES (%s, %s, %s, %s)
            """,
            (
                chat_id,
                user_id,
                "user",
                message
            )
        )


        cursor.execute(
            """
            INSERT INTO ai_messages (
                chat_id,
                user_id,
                role,
                content
            )
            VALUES (%s, %s, %s, %s)
            """,
            (
                chat_id,
                user_id,
                "assistant",
                reply
            )
        )


        if chat_title == "New Chat":

            title = message.replace(
                "\n",
                " "
            ).strip()


            if len(title) > 42:
                title = title[:42] + "..."


            if not title:
                title = "New Chat"


            cursor.execute(
                """
                UPDATE ai_chats
                SET title = %s
                WHERE id = %s
                AND user_id = %s
                """,
                (
                    title,
                    chat_id,
                    user_id
                )
            )

            chat_title = title


        cursor.execute(
            """
            UPDATE ai_chats
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            AND user_id = %s
            """,
            (
                chat_id,
                user_id
            )
        )


        conn.commit()
        conn.close()


        return jsonify({
            "reply": reply,
            "mode": mode,
            "chat_id": chat_id,
            "title": chat_title
        })


    except Exception as e:

        import traceback

        print(
            "\nGEMINI AI TEACHER ERROR:"
        )
        print(
            repr(e)
        )
        traceback.print_exc()


        # Remove an empty chat created by a failed request.
        if created_new_chat:

            try:

                cleanup_conn = get_db()
                cleanup_cursor = cleanup_conn.cursor()

                cleanup_cursor.execute(
                    """
                    DELETE FROM ai_messages
                    WHERE chat_id = %s
                    AND user_id = %s
                    """,
                    (
                        chat_id,
                        user_id
                    )
                )

                cleanup_cursor.execute(
                    """
                    DELETE FROM ai_chats
                    WHERE id = %s
                    AND user_id = %s
                    """,
                    (
                        chat_id,
                        user_id
                    )
                )

                cleanup_conn.commit()
                cleanup_conn.close()

            except Exception as cleanup_error:

                print(
                    "AI TEACHER CLEANUP ERROR:",
                    repr(cleanup_error)
                )


        error_text = (
            type(e).__name__
            + " "
            + str(e)
        ).lower()


        if (
            "ratelimit" in error_text
            or
            "429" in error_text
            or
            "quota" in error_text
            or
            "too_many_requests" in error_text
        ):

            return jsonify({
                "reply":
                    "Gemini request limit reached. Please wait about 30–60 seconds and try again.",
                "error":
                    "AI_TEACHER_RATE_LIMIT"
            }), 429


        if "timeout" in error_text:

            return jsonify({
                "reply":
                    "Gemini took too long to answer. Please send the message again.",
                "error":
                    "AI_TEACHER_TIMEOUT"
            }), 504


        return jsonify({
            "reply":
                "Gemini had a temporary problem. Please send the message again.",
            "error":
                "AI_TEACHER_TEMPORARY_ERROR"
        }), 500


# ===========================
# QUIZ
# ===========================

@app.route("/quiz")
def quiz():

    if "user_id" not in session:
        return redirect(url_for("login"))

    return render_template("quiz.html")
    # ===========================
# SETTINGS
# ===========================

@app.route("/pricing")
def pricing():
    return render_template("pricing.html")


@app.route("/settings")
def settings():

    if "user_id" not in session:
        return redirect(url_for("login"))

    return render_template("settings.html")



# ===========================
# PROFILE + ACHIEVEMENTS
# ===========================

@app.route("/profile")
def profile():

    if "user_id" not in session:
        return redirect(
            url_for("login")
        )

    user_id = session["user_id"]

    record_daily_checkin(
        user_id
    )

    gamification = get_gamification_state(
        user_id
    )

    subscription = refresh_user_subscription(
        user_id
    )

    conn = get_db()
    cursor = conn.cursor()

    user = cursor.execute("""
        SELECT
            id,
            full_name,
            email
        FROM users
        WHERE id = %s
        LIMIT 1
    """, (user_id,)).fetchone()

    vocab = cursor.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(
                CASE
                    WHEN status = 'Mastered'
                    THEN 1
                    ELSE 0
                END
            ) AS mastered,
            SUM(
                CASE
                    WHEN favorite = 1
                    THEN 1
                    ELSE 0
                END
            ) AS favorites
        FROM vocabulary
        WHERE user_id = %s
    """, (user_id,)).fetchone()

    total_words = int(
        vocab["total"]
        or 0
    )

    mastered_words = int(
        vocab["mastered"]
        or 0
    )

    favorite_words = int(
        vocab["favorites"]
        or 0
    )

    mastery_percent = (
        round(
            mastered_words
            /
            total_words
            *
            100
        )
        if total_words
        else 0
    )

    quiz = cursor.execute("""
        SELECT
            COUNT(*) AS attempts,
            COALESCE(
                ROUND(
                    AVG(percentage)
                ),
                0
            ) AS average_score,
            COALESCE(
                MAX(percentage),
                0
            ) AS best_score
        FROM quiz_results
        WHERE user_id = %s
    """, (user_id,)).fetchone()

    quiz_attempts = int(
        quiz["attempts"]
        or 0
    )

    quiz_average = int(
        quiz["average_score"]
        or 0
    )

    quiz_best = int(
        quiz["best_score"]
        or 0
    )

    study = cursor.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(
                CASE
                    WHEN status = 'completed'
                    THEN 1
                    ELSE 0
                END
            ) AS completed
        FROM study_tasks
        WHERE user_id = %s
    """, (user_id,)).fetchone()

    study_tasks_total = int(
        study["total"]
        or 0
    )

    study_tasks_completed = int(
        study["completed"]
        or 0
    )

    conn.close()

    total_xp = int(
        gamification.get(
            "total_xp",
            0
        )
    )

    streak = int(
        gamification.get(
            "streak",
            0
        )
    )

    achievements = [
        {
            "icon": "🚀",
            "title": "First Steps",
            "detail": "Earn your first XP",
            "current": total_xp,
            "target": 1,
            "unit": "XP"
        },
        {
            "icon": "📚",
            "title": "Word Collector",
            "detail": "Save 50 vocabulary words",
            "current": total_words,
            "target": 50,
            "unit": "words"
        },
        {
            "icon": "🎓",
            "title": "Vocabulary Master",
            "detail": "Master 25 words",
            "current": mastered_words,
            "target": 25,
            "unit": "mastered"
        },
        {
            "icon": "🏆",
            "title": "Quiz Challenger",
            "detail": "Complete 10 quizzes",
            "current": quiz_attempts,
            "target": 10,
            "unit": "quizzes"
        },
        {
            "icon": "⚡",
            "title": "Quiz Ace",
            "detail": "Score 90% or higher",
            "current": quiz_best,
            "target": 90,
            "unit": "%"
        },
        {
            "icon": "◷",
            "title": "Study Finisher",
            "detail": "Complete 10 study tasks",
            "current": study_tasks_completed,
            "target": 10,
            "unit": "tasks"
        },
        {
            "icon": "🔥",
            "title": "7 Day Streak",
            "detail": "Learn 7 days in a row",
            "current": streak,
            "target": 7,
            "unit": "days"
        },
        {
            "icon": "👑",
            "title": "XP Champion",
            "detail": "Reach 1,000 total XP",
            "current": total_xp,
            "target": 1000,
            "unit": "XP"
        }
    ]

    for achievement in achievements:

        target = max(
            1,
            int(
                achievement["target"]
            )
        )

        current = max(
            0,
            int(
                achievement["current"]
            )
        )

        achievement["unlocked"] = (
            current >= target
        )

        achievement["progress_percent"] = min(
            100,
            round(
                current
                /
                target
                *
                100
            )
        )

    unlocked_achievements = sum(
        1
        for achievement in achievements
        if achievement["unlocked"]
    )

    locked_achievements = [
        achievement
        for achievement in achievements
        if not achievement["unlocked"]
    ]

    next_achievement = (
        max(
            locked_achievements,
            key=lambda achievement:
                achievement[
                    "progress_percent"
                ]
        )
        if locked_achievements
        else None
    )

    plan_key = subscription.get(
        "plan",
        "free"
    )

    plan_labels = {
        "free": "Free Plan",
        "pro_monthly": "Pro Monthly",
        "pro_yearly": "Pro Yearly"
    }

    plan_prices = {
        "free": "$0",
        "pro_monthly": "$5.99 / month",
        "pro_yearly": "$44.99 / year"
    }

    plan_label = plan_labels.get(
        plan_key,
        "Free Plan"
    )

    plan_price = plan_prices.get(
        plan_key,
        "$0"
    )

    weekly_activity = gamification.get(
        "weekly_activity",
        []
    )

    weekly_xp_total = sum(
        int(
            day.get(
                "xp",
                0
            )
        )
        for day in weekly_activity
    )

    return render_template(
        "profile.html",

        user=user,

        gamification=gamification,

        subscription=subscription,

        plan_label=plan_label,

        plan_price=plan_price,

        total_words=total_words,

        mastered_words=mastered_words,

        favorite_words=favorite_words,

        mastery_percent=mastery_percent,

        quiz_attempts=quiz_attempts,

        quiz_average=quiz_average,

        quiz_best=quiz_best,

        study_tasks_total=study_tasks_total,

        study_tasks_completed=
            study_tasks_completed,

        achievements=achievements,

        unlocked_achievements=
            unlocked_achievements,

        next_achievement=
            next_achievement,

        weekly_activity=
            weekly_activity,

        weekly_xp_total=
            weekly_xp_total
    )

# ===========================
# PROGRESS ANALYTICS
# ===========================

@app.route("/analytics")
def analytics():

    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]

    record_daily_checkin(user_id)
    gamification = get_gamification_state(user_id)

    conn = get_db()
    cursor = conn.cursor()

    vocab = cursor.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='New' THEN 1 ELSE 0 END) AS new_count,
            SUM(CASE WHEN status='Learning' THEN 1 ELSE 0 END) AS learning_count,
            SUM(CASE WHEN status='Mastered' THEN 1 ELSE 0 END) AS mastered_count
        FROM vocabulary
        WHERE user_id = %s
    """, (user_id,)).fetchone()

    vocabulary_total = int(vocab["total"] or 0)
    vocabulary_new = int(vocab["new_count"] or 0)
    vocabulary_learning = int(vocab["learning_count"] or 0)
    vocabulary_mastered = int(vocab["mastered_count"] or 0)

    mastery_percent = (
        round(vocabulary_mastered / vocabulary_total * 100)
        if vocabulary_total else 0
    )

    quiz_summary = cursor.execute("""
        SELECT
            COUNT(*) AS attempts,
            COALESCE(ROUND(AVG(percentage)), 0) AS average_score,
            COALESCE(MAX(percentage), 0) AS best_score
        FROM quiz_results
        WHERE user_id = %s
    """, (user_id,)).fetchone()

    quiz_attempts = int(quiz_summary["attempts"] or 0)
    quiz_average = int(quiz_summary["average_score"] or 0)
    quiz_best = int(quiz_summary["best_score"] or 0)

    category_rows = cursor.execute("""
        SELECT
            category,
            COUNT(*) AS attempts,
            COALESCE(ROUND(AVG(percentage)), 0) AS average_score,
            COALESCE(MAX(percentage), 0) AS best_score
        FROM quiz_results
        WHERE user_id = %s
        GROUP BY category
    """, (user_id,)).fetchall()

    raw_categories = {
        row["category"]: {
            "attempts": int(row["attempts"] or 0),
            "average": int(row["average_score"] or 0),
            "best": int(row["best_score"] or 0)
        }
        for row in category_rows
    }

    categories = [
        ("grammar", "Grammar", "✎"),
        ("vocabulary", "Vocabulary", "📚"),
        ("ielts", "IELTS", "◎"),
        ("sat", "SAT English", "✦"),
        ("sat_math", "SAT Math", "∑")
    ]

    skill_analytics = []

    for key, label, icon in categories:

        data = raw_categories.get(
            key,
            {"attempts": 0, "average": 0, "best": 0}
        )

        skill_analytics.append({
            "key": key,
            "label": label,
            "icon": icon,
            "attempts": data["attempts"],
            "average": data["average"],
            "best": data["best"]
        })

    tested = [
        skill for skill in skill_analytics
        if skill["attempts"] > 0
    ]

    strongest_skill = (
        max(tested, key=lambda skill: skill["average"])
        if tested else None
    )

    weakest_skill = (
        min(tested, key=lambda skill: skill["average"])
        if tested else None
    )

    recent_quizzes = cursor.execute("""
        SELECT
            category,
            difficulty,
            score,
            total_questions,
            percentage,
            created_at
        FROM quiz_results
        WHERE user_id = %s
        ORDER BY id DESC
        LIMIT 8
    """, (user_id,)).fetchall()

    study = cursor.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed
        FROM study_tasks
        WHERE user_id = %s
    """, (user_id,)).fetchone()

    study_tasks_total = int(study["total"] or 0)
    study_tasks_completed = int(study["completed"] or 0)

    study_completion_percent = (
        round(study_tasks_completed / study_tasks_total * 100)
        if study_tasks_total else 0
    )

    conn.close()

    if weakest_skill:

        insight_title = "Your next focus: " + weakest_skill["label"]
        insight_text = (
            "Your current average in "
            + weakest_skill["label"]
            + " is "
            + str(weakest_skill["average"])
            + "%. Focused practice here is the clearest next step."
        )

    elif vocabulary_total:

        insight_title = "Create more measurable progress"
        insight_text = (
            "You already have vocabulary activity. Complete a few quizzes "
            "so LinguaMind can compare your skills and find your strongest "
            "and weakest areas."
        )

    else:

        insight_title = "Start building your learning data"
        insight_text = (
            "Add vocabulary, complete a quiz and finish a Study Plan task. "
            "Your analytics will become more useful after each action."
        )

    weekly_activity = gamification.get("weekly_activity", [])
    weekly_xp_total = sum(
        int(day.get("xp", 0))
        for day in weekly_activity
    )

    return render_template(
        "analytics.html",
        gamification=gamification,
        vocabulary_total=vocabulary_total,
        vocabulary_new=vocabulary_new,
        vocabulary_learning=vocabulary_learning,
        vocabulary_mastered=vocabulary_mastered,
        mastery_percent=mastery_percent,
        quiz_attempts=quiz_attempts,
        quiz_average=quiz_average,
        quiz_best=quiz_best,
        skill_analytics=skill_analytics,
        strongest_skill=strongest_skill,
        weakest_skill=weakest_skill,
        recent_quizzes=recent_quizzes,
        study_tasks_total=study_tasks_total,
        study_tasks_completed=study_tasks_completed,
        study_completion_percent=study_completion_percent,
        weekly_activity=weekly_activity,
        weekly_xp_total=weekly_xp_total,
        insight_title=insight_title,
        insight_text=insight_text
    )
    # ===========================
# QUIZ RESULTS API
# ===========================

@app.route(
    "/api/quiz_results",
    methods=["GET", "POST", "DELETE"]
)
def quiz_results_api():

    if "user_id" not in session:
        return jsonify({
            "error": "Login required."
        }), 401

    user_id = session["user_id"]


    # ===========================
    # SAVE QUIZ RESULT
    # ===========================

    if request.method == "POST":

        data = request.get_json(
            silent=True
        ) or {}

        category = str(
            data.get("category", "")
        ).strip().lower()

        difficulty = str(
            data.get("difficulty", "")
        ).strip().lower()

        try:
            score = int(
                data.get("score", 0)
            )

            total_questions = int(
                data.get(
                    "total_questions",
                    0
                )
            )

        except (TypeError, ValueError):

            return jsonify({
                "error": "Invalid quiz result."
            }), 400


        allowed_categories = {
            "grammar",
            "vocabulary",
            "ielts",
            "sat",
            "sat_math"
        }

        allowed_difficulties = {
            "easy",
            "medium",
            "hard",
            "mixed"
        }


        if category not in allowed_categories:

            return jsonify({
                "error": "Invalid category."
            }), 400


        if difficulty not in allowed_difficulties:

            return jsonify({
                "error": "Invalid difficulty."
            }), 400


        if total_questions <= 0:

            return jsonify({
                "error": "Invalid question count."
            }), 400


        if score < 0:
            score = 0


        if score > total_questions:
            score = total_questions


        percentage = round(
            (
                score /
                total_questions
            ) * 100
        )


        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO quiz_results (
                user_id,
                category,
                difficulty,
                score,
                total_questions,
                percentage
            )
            VALUES (%s, %s, %s, %s, %s, %s)
RETURNING id
            """,
            (
                user_id,
                category,
                difficulty,
                score,
                total_questions,
                percentage
            )
        )

        result_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()

        quiz_xp = 35 + max(0, min(25, round(percentage / 4)))
        xp_awarded = award_xp(
            user_id,
            quiz_xp,
            "quiz_completed",
            "quiz_result:" + str(result_id)
        )


        return jsonify({
            "success": True,
            "result_id": result_id,
            "percentage": percentage,
            "xp_awarded": xp_awarded
        })


    # ===========================
    # LOAD QUIZ HISTORY
    # ===========================

    if request.method == "GET":

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                id,
                category,
                difficulty,
                score,
                total_questions,
                percentage,
                created_at
            FROM quiz_results
            WHERE user_id = %s
            ORDER BY id DESC
            LIMIT 50
            """,
            (user_id,)
        )

        rows = cursor.fetchall()
        conn.close()

        results = []

        for row in rows:

            results.append({
                "id": row["id"],
                "category": row["category"],
                "difficulty": row["difficulty"],
                "score": row["score"],
                "total_questions":
                    row["total_questions"],
                "percentage":
                    row["percentage"],
                "created_at":
                    row["created_at"]
            })


        return jsonify({
            "results": results
        })


    # ===========================
    # CLEAR ALL QUIZ HISTORY
    # ===========================

    if request.method == "DELETE":

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            """
            DELETE FROM quiz_results
            WHERE user_id = %s
            """,
            (user_id,)
        )

        conn.commit()
        conn.close()


        return jsonify({
            "success": True
        })


# ===========================
# DELETE ONE QUIZ RESULT
# ===========================

@app.route(
    "/api/quiz_results/<int:result_id>",
    methods=["DELETE"]
)
def delete_quiz_result(result_id):

    if "user_id" not in session:

        return jsonify({
            "error": "Login required."
        }), 401


    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        DELETE FROM quiz_results
        WHERE id = %s
        AND user_id = %s
        """,
        (
            result_id,
            session["user_id"]
        )
    )

    conn.commit()
    conn.close()


    return jsonify({
        "success": True
    })
    # ===========================
# SCAN TEXT / OCR
# ===========================

@app.route("/scan_text")
def scan_text():

    if "user_id" not in session:
        return redirect(url_for("login"))

    return render_template("scan_text.html")


@app.route("/api/scan_text", methods=["POST"])
def scan_text_api():

    if "user_id" not in session:
        return jsonify({
            "error": "Login required."
        }), 401

    image = request.files.get("image")

    if not image or not image.filename:
        return jsonify({
            "error": "Please choose an image."
        }), 400

    mime_type = (
        image.mimetype or ""
    ).lower()

    allowed_types = {
        "image/jpeg",
        "image/png",
        "image/webp"
    }

    if mime_type not in allowed_types:
        return jsonify({
            "error": "Only JPG, PNG and WEBP images are supported."
        }), 400

    image_bytes = image.read()

    if not image_bytes:
        return jsonify({
            "error": "The image is empty."
        }), 400

    max_size = 10 * 1024 * 1024

    if len(image_bytes) > max_size:
        return jsonify({
            "error": "Image is too large. Maximum size is 10 MB."
        }), 400

    try:

        image_base64 = base64.b64encode(
            image_bytes
        ).decode("utf-8")

        client = genai.Client(
            api_key=GEMINI_API_KEY
        )

        prompt = """
You are an accurate OCR system for LinguaMind.

Extract every readable piece of text from this image.

Rules:
- Return only the extracted text.
- Do not explain anything.
- Do not use markdown code blocks.
- Preserve paragraphs and line breaks when possible.
- Preserve punctuation.
- Preserve the original language.
- Do not translate.
- If there is a heading, keep it as a heading line.
- If no readable text exists, return exactly: NO_TEXT_FOUND
"""

        response = client.interactions.create(
            model="gemini-3.6-flash",
            input=[
                {
                    "type": "text",
                    "text": prompt
                },
                {
                    "type": "image",
                    "data": image_base64,
                    "mime_type": mime_type
                }
            ]
        )

        extracted_text = (
            response.output_text or ""
        ).strip()

        if (
            not extracted_text
            or extracted_text == "NO_TEXT_FOUND"
        ):
            return jsonify({
                "error": "No readable text was found in the image."
            }), 422

        return jsonify({
            "success": True,
            "text": extracted_text
        })

    except Exception as e:

        print(
            "SCAN TEXT ERROR:",
            repr(e)
        )

        return jsonify({
            "error": "Could not read this image. Please try another image."
        }), 500
        

# ===========================
# SCAN TEXT API
# ===========================
# ===========================
# TEXT TO SPEECH / MP3
# ===========================

@app.route("/api/text_to_speech", methods=["POST"])
def text_to_speech_api():

    if "user_id" not in session:
        return jsonify({
            "error": "Login required."
        }), 401

    pro_block = require_pro_api(
        "MP3 Generation"
    )

    if pro_block is not None:
        return pro_block

    data = request.get_json(
        silent=True
    ) or {}

    text = str(
        data.get("text", "")
    ).strip()

    language = str(
        data.get("language", "en")
    ).strip().lower()

    if not text:
        return jsonify({
            "error": "There is no text to convert."
        }), 400

    if len(text) > 10000:
        return jsonify({
            "error": "Text is too long. Maximum 10,000 characters."
        }), 400

    allowed_languages = {
        "en",
        "uz",
        "ru"
    }

    if language not in allowed_languages:
        language = "en"

    try:

        mp3_file = io.BytesIO()

        tts = gTTS(
            text=text,
            lang=language,
            slow=False
        )

        tts.write_to_fp(
            mp3_file
        )

        mp3_file.seek(0)

        return send_file(
            mp3_file,
            mimetype="audio/mpeg",
            as_attachment=False,
            download_name="linguamind_audio.mp3"
        )

    except Exception as e:

        print(
            "TEXT TO SPEECH ERROR:",
            repr(e)
        )

        return jsonify({
            "error": "Could not generate MP3. Please try again."
        }), 500
        # ===========================
# AI VISION
# ===========================

@app.route("/api/ai_vision", methods=["POST"])
def ai_vision_api():

    if "user_id" not in session:
        return jsonify({
            "error": "Login required."
        }), 401

    pro_block = require_pro_api(
        "AI Vision"
    )

    if pro_block is not None:
        return pro_block

    image = request.files.get("image")

    prompt = str(
        request.form.get("prompt", "")
    ).strip()

    if not image or not image.filename:
        return jsonify({
            "error": "Please choose an image first."
        }), 400

    if not prompt:
        prompt = (
            "Explain this image clearly. "
            "If there is text, help me understand it "
            "as an English learner."
        )

    mime_type = (
        image.mimetype or ""
    ).lower()

    allowed_types = {
        "image/jpeg",
        "image/png",
        "image/webp"
    }

    if mime_type not in allowed_types:
        return jsonify({
            "error": "Only JPG, PNG and WEBP images are supported."
        }), 400

    image_bytes = image.read()

    if not image_bytes:
        return jsonify({
            "error": "The image is empty."
        }), 400

    if len(image_bytes) > 10 * 1024 * 1024:
        return jsonify({
            "error": "Image is too large. Maximum size is 10 MB."
        }), 400

    try:

        image_base64 = base64.b64encode(
            image_bytes
        ).decode("utf-8")

        teacher_prompt = f"""
You are LinguaMind AI Vision Teacher.

The user uploaded an image and asked:

{prompt}

Your job:
- Analyze the image carefully.
- If the image contains English text, explain it clearly.
- If the user asks for translation, translate accurately.
- If the user asks about grammar, identify mistakes and explain corrections.
- If the user asks for vocabulary, explain important words with simple examples.
- If the user asks for quiz questions, create useful questions from the image.
- Use clear formatting.
- Be helpful for an English learner.
- Do not invent text that is not visible in the image.
"""

        client = genai.Client(
            api_key=GEMINI_API_KEY
        )

        response = client.interactions.create(
            model="gemini-3.6-flash",

            input=[
                {
                    "type": "image",
                    "data": image_base64,
                    "mime_type": mime_type
                },
                {
                    "type": "text",
                    "text": teacher_prompt
                }
            ]
        )

        reply = (
            response.output_text or ""
        ).strip()

        if not reply:
            return jsonify({
                "error": "AI could not generate a response."
            }), 500

        return jsonify({
            "success": True,
            "reply": reply
        })

    except Exception as e:

        print(
            "AI VISION ERROR:",
            repr(e)
        )

        return jsonify({
            "error": "AI Vision could not analyze this image."
        }), 500

# ===========================
# AI STUDY PLANNER
# ===========================

def get_study_plan_window(
    period_type,
    start_day
):

    if period_type == "daily":
        return start_day

    if period_type == "weekly":
        return start_day + timedelta(days=6)

    if period_type == "monthly":
        return start_day + timedelta(days=29)

    return start_day + timedelta(days=364)


def clean_study_task_date(
    raw_date,
    start_day,
    period_type
):

    try:
        task_day = date.fromisoformat(
            str(raw_date).strip()
        )

    except (
        TypeError,
        ValueError
    ):
        return None

    if task_day < start_day:
        return None

    if period_type == "daily":

        if task_day != start_day:
            return None

    elif period_type == "weekly":

        if task_day > (
            start_day
            + timedelta(days=6)
        ):
            return None

    else:

        # Monthly/yearly plans keep the long roadmap in JSON,
        # while actionable tasks cover only the next 7 days.
        if task_day > (
            start_day
            + timedelta(days=6)
        ):
            return None

    return task_day


@app.route("/study_plan")
def study_plan():

    if "user_id" not in session:
        return redirect(
            url_for("login")
        )

    user_id = session["user_id"]

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT *
        FROM study_profiles
        WHERE user_id = %s
        LIMIT 1
        """,
        (user_id,)
    )

    profile = cursor.fetchone()

    cursor.execute(
        """
        SELECT *
        FROM study_plans
        WHERE user_id = %s
        AND status = 'active'
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,)
    )

    active_plan = cursor.fetchone()

    tasks = []
    plan_data = {}
    completed_tasks = 0
    total_tasks = 0
    plan_progress = 0
    exam_days_left = None

    if active_plan:

        cursor.execute(
            """
            SELECT *
            FROM study_tasks
            WHERE user_id = %s
            AND plan_id = %s
            ORDER BY
                task_date ASC,
                sort_order ASC,
                id ASC
            """,
            (
                user_id,
                active_plan["id"]
            )
        )

        tasks = cursor.fetchall()

        total_tasks = len(tasks)

        completed_tasks = sum(
            1
            for task in tasks
            if task["status"] == "completed"
        )

        if total_tasks > 0:

            plan_progress = round(
                (
                    completed_tasks
                    /
                    total_tasks
                )
                *
                100
            )

        try:

            plan_data = json.loads(
                active_plan["plan_json"]
                or
                "{}"
            )

        except (
            TypeError,
            json.JSONDecodeError
        ):

            plan_data = {}

        if active_plan["exam_date"]:

            try:

                exam_day = date.fromisoformat(
                    active_plan["exam_date"]
                )

                exam_days_left = (
                    exam_day
                    -
                    date.today()
                ).days

            except ValueError:

                exam_days_left = None

    conn.close()

    return render_template(
        "study_plan.html",
        profile=profile,
        active_plan=active_plan,
        tasks=tasks,
        plan_data=plan_data,
        total_tasks=total_tasks,
        completed_tasks=completed_tasks,
        plan_progress=plan_progress,
        exam_days_left=exam_days_left,
        today_iso=date.today().isoformat()
    )


@app.route(
    "/api/study_plan/generate",
    methods=["POST"]
)
def generate_study_plan_api():

    if "user_id" not in session:

        return jsonify({
            "error":
                "Login required."
        }), 401

    pro_block = require_pro_api(
        "AI Study Coach"
    )

    if pro_block is not None:
        return pro_block

    if not GEMINI_API_KEY:

        return jsonify({
            "error":
                "Gemini API key was not found."
        }), 500

    user_id = session["user_id"]

    data = request.get_json(
        silent=True
    ) or {}

    period_type = str(
        data.get(
            "period_type",
            "weekly"
        )
    ).strip().lower()

    main_goal = str(
        data.get(
            "main_goal",
            "general_english"
        )
    ).strip().lower()

    current_level = str(
        data.get(
            "current_level",
            "not_sure"
        )
    ).strip()

    weak_areas = str(
        data.get(
            "weak_areas",
            ""
        )
    ).strip()[:1200]

    target_score = str(
        data.get(
            "target_score",
            ""
        )
    ).strip()[:80]

    exam_type = str(
        data.get(
            "exam_type",
            ""
        )
    ).strip()[:80]

    has_exam = bool(
        data.get(
            "has_exam",
            False
        )
    )

    allowed_periods = {
        "daily",
        "weekly",
        "monthly",
        "yearly"
    }

    allowed_goals = {
        "general_english",
        "ielts",
        "sat",
        "sat_full",
        "sat_english",
        "sat_math",
        "speaking",
        "vocabulary",
        "grammar"
    }

    allowed_levels = {
        "not_sure",
        "A1",
        "A2",
        "B1",
        "B2",
        "C1"
    }

    if period_type not in allowed_periods:
        period_type = "weekly"

    if main_goal not in allowed_goals:
        main_goal = "general_english"

    if current_level not in allowed_levels:
        current_level = "not_sure"

    try:

        daily_minutes = int(
            data.get(
                "daily_minutes",
                60
            )
        )

    except (
        TypeError,
        ValueError
    ):

        daily_minutes = 60

    daily_minutes = max(
        20,
        min(
            daily_minutes,
            240
        )
    )

    try:

        days_per_week = int(
            data.get(
                "days_per_week",
                6
            )
        )

    except (
        TypeError,
        ValueError
    ):

        days_per_week = 6

    days_per_week = max(
        1,
        min(
            days_per_week,
            7
        )
    )

    start_day = date.today()

    exam_date = None
    days_until_exam = None

    if has_exam:

        raw_exam_date = str(
            data.get(
                "exam_date",
                ""
            )
        ).strip()

        if not raw_exam_date:

            return jsonify({
                "error":
                    "Please choose your exam date."
            }), 400

        try:

            exam_day = date.fromisoformat(
                raw_exam_date
            )

        except ValueError:

            return jsonify({
                "error":
                    "Exam date is not valid."
            }), 400

        if exam_day < start_day:

            return jsonify({
                "error":
                    "Exam date cannot be in the past."
            }), 400

        exam_date = exam_day.isoformat()

        days_until_exam = (
            exam_day
            -
            start_day
        ).days

        if not exam_type:

            exam_type = (
                "SAT"
                if main_goal in {
                    "sat",
                    "sat_full",
                    "sat_english",
                    "sat_math"
                }
                else
                "IELTS"
                if main_goal == "ielts"
                else
                "Other"
            )

    else:

        exam_type = ""
        target_score = ""

    end_day = get_study_plan_window(
        period_type,
        start_day
    )

    conn = get_db()
    cursor = conn.cursor()

    # ===========================
    # USER PERFORMANCE CONTEXT
    # ===========================

    cursor.execute(
        """
        SELECT
            COUNT(*) AS total,
            COALESCE(
                SUM(
                    CASE
                        WHEN status = 'New'
                        THEN 1
                        ELSE 0
                    END
                ),
                0
            ) AS new_count,
            COALESCE(
                SUM(
                    CASE
                        WHEN status = 'Learning'
                        THEN 1
                        ELSE 0
                    END
                ),
                0
            ) AS learning_count,
            COALESCE(
                SUM(
                    CASE
                        WHEN status = 'Mastered'
                        THEN 1
                        ELSE 0
                    END
                ),
                0
            ) AS mastered_count
        FROM vocabulary
        WHERE user_id = %s
        """,
        (user_id,)
    )

    vocabulary_stats = cursor.fetchone()

    cursor.execute(
        """
        SELECT
            category,
            difficulty,
            score,
            total_questions,
            percentage,
            created_at
        FROM quiz_results
        WHERE user_id = %s
        ORDER BY id DESC
        LIMIT 12
        """,
        (user_id,)
    )

    recent_quizzes = cursor.fetchall()

    quiz_context_lines = []

    for quiz_row in recent_quizzes:

        quiz_context_lines.append(
            (
                f"- {quiz_row['category']} "
                f"{quiz_row['difficulty']}: "
                f"{quiz_row['percentage']}% "
                f"({quiz_row['score']}/"
                f"{quiz_row['total_questions']})"
            )
        )

    quiz_context = (
        "\n".join(
            quiz_context_lines
        )
        if quiz_context_lines
        else
        "No quiz history yet."
    )

    cursor.execute(
        """
        SELECT
            st.title,
            st.category,
            st.status,
            st.task_date
        FROM study_tasks AS st
        JOIN study_plans AS sp
            ON sp.id = st.plan_id
        WHERE st.user_id = %s
        AND sp.status = 'active'
        ORDER BY st.id DESC
        LIMIT 12
        """,
        (user_id,)
    )

    previous_tasks = cursor.fetchall()

    previous_task_lines = []

    for previous_task in previous_tasks:

        previous_task_lines.append(
            (
                f"- {previous_task['task_date']}: "
                f"{previous_task['title']} "
                f"[{previous_task['category']}] "
                f"= {previous_task['status']}"
            )
        )

    previous_task_context = (
        "\n".join(
            previous_task_lines
        )
        if previous_task_lines
        else
        "No previous study-plan tasks."
    )

    username = session.get(
        "username",
        "Learner"
    )

    goal_names = {
        "general_english":
            "General English",
        "ielts":
            "IELTS",
        "sat":
            "Full SAT (Reading & Writing + Math)",
        "sat_full":
            "Full SAT (Reading & Writing + Math)",
        "sat_english":
            "SAT Reading & Writing",
        "sat_math":
            "SAT Math",
        "speaking":
            "English Speaking",
        "vocabulary":
            "Vocabulary Growth",
        "grammar":
            "English Grammar"
    }

    goal_focus_rules = {
        "general_english":
            (
                "Build balanced English across speaking, grammar, "
                "vocabulary, reading and listening. Include speaking "
                "regularly, but keep the workload realistic."
            ),

        "ielts":
            (
                "Prepare specifically for IELTS. Balance Reading, "
                "Listening, Writing and Speaking, then adjust time "
                "toward the learner's weakest IELTS skills."
            ),

        "sat":
            (
                "Prepare for the full SAT. Include BOTH SAT Reading & "
                "Writing and SAT Math. Use timed work and error review "
                "when appropriate."
            ),

        "sat_full":
            (
                "Prepare for the full SAT. Include BOTH SAT Reading & "
                "Writing and SAT Math. Keep the two sections balanced "
                "unless performance data clearly shows a weakness."
            ),

        "sat_english":
            (
                "Focus primarily on Digital SAT Reading & Writing: "
                "Standard English Conventions, transitions, rhetorical "
                "synthesis, vocabulary in context and passage reasoning."
            ),

        "sat_math":
            (
                "Focus primarily on SAT Math: Algebra, Advanced Math, "
                "Problem-Solving & Data Analysis, Geometry and "
                "Trigonometry. Use category sat_math. Until a dedicated "
                "SAT Math quiz screen is added, guided SAT Math practice "
                "must use action_type ai_teacher."
            ),

        "speaking":
            (
                "Focus on active spoken English. Create frequent guided "
                "conversation sessions, pronunciation-friendly practice, "
                "useful vocabulary and short grammar corrections without "
                "turning every session into a grammar lecture."
            ),

        "vocabulary":
            (
                "Focus on useful vocabulary growth, spaced review, "
                "collocations, phrases and using words in context."
            ),

        "grammar":
            (
                "Focus on practical grammar mastery with explanation, "
                "guided practice, quizzes and error correction."
            )
    }

    period_instructions = {
        "daily":
            """
Create ONLY today's actionable schedule.
Use 2-5 focused tasks.
The sum of task minutes must not exceed the learner's daily time budget.
Milestones may contain one item called "Today's outcome".
""",

        "weekly":
            """
Create a 7-day plan.
Use only the learner's requested number of study days;
the other days should be recovery/rest days and should NOT appear as tasks.
Create 2-5 tasks per active study day.
The sum of minutes on each active day must not exceed the daily time budget.
Milestones should describe the week's main outcomes.
""",

        "monthly":
            """
Create four weekly milestones for the month.
Do NOT create 30 days of individual tasks.
Create detailed actionable tasks ONLY for the next 7 days,
so LinguaMind can work with the learner immediately.
The long-term monthly direction belongs in milestones.
""",

        "yearly":
            """
Create exactly 12 meaningful monthly milestones.
Do NOT create 365 daily tasks.
Create detailed actionable tasks ONLY for the next 7 days.
The yearly roadmap should show realistic progression,
while immediate tasks should tell the learner exactly what to do now.
"""
    }

    exam_context = (
        (
            f"YES. Exam: {exam_type}. "
            f"Exam date: {exam_date}. "
            f"Days remaining: {days_until_exam}. "
            f"Target score: {target_score or 'not specified'}."
        )
        if has_exam
        else
        "No fixed exam date."
    )

    prompt = f"""
You are LinguaMind's adaptive AI Study Coach.

Your job is to create a realistic, supportive and highly practical
study plan that LinguaMind itself can follow with this learner.

LEARNER:
Name: {username}
Main goal: {goal_names[main_goal]}
Goal-specific coaching direction:
{goal_focus_rules[main_goal]}
Current level: {current_level}
Available study time per active day: {daily_minutes} minutes
Available study days per week: {days_per_week}
Self-reported weak areas: {weak_areas or "Not specified"}

EXAM:
{exam_context}

CURRENT LINGUAMIND DATA:
Vocabulary:
- Total words: {vocabulary_stats["total"]}
- New: {vocabulary_stats["new_count"]}
- Learning: {vocabulary_stats["learning_count"]}
- Mastered: {vocabulary_stats["mastered_count"]}

Recent quiz performance:
{quiz_context}

Previous plan/task behavior:
{previous_task_context}

PLAN TYPE:
{period_type.upper()}

PERIOD RULE:
{period_instructions[period_type]}

START DATE:
{start_day.isoformat()}

LONG-RANGE END DATE:
{end_day.isoformat()}

CORE COACHING RULES:
1. Do not overload the learner.
2. Never exceed {daily_minutes} total planned minutes on an active day.
3. Prefer consistency over exhausting sessions.
4. Give specific tasks, not vague advice.
5. Use LinguaMind tools whenever useful.
6. Include speaking practice regularly for General English and IELTS.
7. For IELTS, balance Speaking, Writing, Reading, Listening,
   Grammar and Vocabulary according to weaknesses.
8. SAT rules:
   - For Full SAT goals, include BOTH SAT Reading & Writing and SAT Math.
   - For SAT Reading & Writing goals, prioritize category "sat_english".
   - For SAT Math goals, prioritize category "sat_math".
   - SAT Math tasks must use action_type "ai_teacher" until the dedicated
     SAT Math practice screen is added.
   - Never silently replace SAT Math with English-only practice.
9. Use quiz performance to spend more time on weak areas.
10. Use vocabulary progress to decide whether review or new words are needed.
11. If an exam is close, reduce unnecessary new material and increase
    review, timed practice, error analysis and realistic mock work.
12. If the learner previously missed tasks, adapt gently instead of
    punishing them or doubling the workload.
13. Every task should be something LinguaMind can actively help with.
14. For speaking tasks use action_type "ai_teacher".
15. Keep descriptions short and actionable.
16. For monthly/yearly plans, milestones carry the long roadmap;
    immediate tasks cover only the next 7 days.
17. Task dates must be valid ISO dates YYYY-MM-DD.
18. Return ONLY valid JSON. No markdown fences and no extra prose.

ALLOWED category values:
general_english
grammar
vocabulary
speaking
ielts_reading
ielts_writing
ielts_listening
ielts_speaking
sat_english
sat_math
quiz
review

ALLOWED action_type values:
ai_teacher
quiz
vocabulary
scan_text
self_study

ALLOWED difficulty values:
easy
medium
hard
mixed

Return EXACTLY this JSON structure:

{{
  "title": "Short personal plan title",
  "summary": "2-3 sentence plan summary",
  "coach_message": "One supportive, specific message to the learner",
  "milestones": [
    {{
      "label": "Week 1 / August / Today etc.",
      "goal": "Clear measurable milestone"
    }}
  ],
  "tasks": [
    {{
      "date": "{start_day.isoformat()}",
      "period_label": "Day 1",
      "title": "Specific task title",
      "description": "Exactly what the learner should do",
      "category": "grammar",
      "minutes": 20,
      "action_type": "ai_teacher",
      "difficulty": "medium"
    }}
  ]
}}
"""

    try:

        client = genai.Client(
            api_key=GEMINI_API_KEY
        )

        response = client.interactions.create(
            model="gemini-3.6-flash",
            input=prompt
        )

        raw = (
            response.output_text
            or
            ""
        ).strip()

        json_start = raw.find("{")
        json_end = raw.rfind("}")

        if (
            json_start == -1
            or
            json_end == -1
            or
            json_end <= json_start
        ):

            raise ValueError(
                "Gemini did not return valid JSON."
            )

        plan_data = json.loads(
            raw[
                json_start:
                json_end + 1
            ]
        )

        title = str(
            plan_data.get(
                "title",
                "My LinguaMind Plan"
            )
        ).strip()[:120]

        summary = str(
            plan_data.get(
                "summary",
                ""
            )
        ).strip()[:1500]

        coach_message = str(
            plan_data.get(
                "coach_message",
                ""
            )
        ).strip()[:1000]

        if not title:
            title = "My LinguaMind Plan"

        if not summary:
            summary = (
                "A focused study plan based on your goal, "
                "available time and LinguaMind progress."
            )

        raw_milestones = plan_data.get(
            "milestones",
            []
        )

        clean_milestones = []

        if isinstance(
            raw_milestones,
            list
        ):

            max_milestones = (
                12
                if period_type == "yearly"
                else
                6
            )

            for milestone in raw_milestones[
                :max_milestones
            ]:

                if not isinstance(
                    milestone,
                    dict
                ):
                    continue

                milestone_label = str(
                    milestone.get(
                        "label",
                        ""
                    )
                ).strip()[:100]

                milestone_goal = str(
                    milestone.get(
                        "goal",
                        ""
                    )
                ).strip()[:600]

                if (
                    milestone_label
                    and
                    milestone_goal
                ):

                    clean_milestones.append({
                        "label":
                            milestone_label,
                        "goal":
                            milestone_goal
                    })

        raw_tasks = plan_data.get(
            "tasks",
            []
        )

        if not isinstance(
            raw_tasks,
            list
        ):

            raise ValueError(
                "Gemini returned an invalid task list."
            )

        allowed_categories = {
            "general_english",
            "grammar",
            "vocabulary",
            "speaking",
            "ielts_reading",
            "ielts_writing",
            "ielts_listening",
            "ielts_speaking",
            "sat_english",
            "sat_math",
            "quiz",
            "review"
        }

        allowed_actions = {
            "ai_teacher",
            "quiz",
            "vocabulary",
            "scan_text",
            "self_study"
        }

        allowed_difficulties = {
            "easy",
            "medium",
            "hard",
            "mixed"
        }

        clean_tasks = []

        daily_totals = {}

        for index, item in enumerate(
            raw_tasks
        ):

            if not isinstance(
                item,
                dict
            ):
                continue

            task_day = clean_study_task_date(
                item.get("date"),
                start_day,
                period_type
            )

            if not task_day:
                continue

            task_title = str(
                item.get(
                    "title",
                    ""
                )
            ).strip()[:160]

            task_description = str(
                item.get(
                    "description",
                    ""
                )
            ).strip()[:1000]

            category = str(
                item.get(
                    "category",
                    "general_english"
                )
            ).strip().lower()

            action_type = str(
                item.get(
                    "action_type",
                    "ai_teacher"
                )
            ).strip().lower()

            difficulty = str(
                item.get(
                    "difficulty",
                    "mixed"
                )
            ).strip().lower()

            period_label = str(
                item.get(
                    "period_label",
                    ""
                )
            ).strip()[:100]

            try:

                minutes = int(
                    item.get(
                        "minutes",
                        20
                    )
                )

            except (
                TypeError,
                ValueError
            ):

                minutes = 20

            minutes = max(
                5,
                min(
                    minutes,
                    daily_minutes
                )
            )

            if category not in allowed_categories:
                category = "general_english"

            if action_type not in allowed_actions:
                action_type = "ai_teacher"

            if difficulty not in allowed_difficulties:
                difficulty = "mixed"

            if category == "sat_math":
                action_type = "ai_teacher"

            if category in {
                "speaking",
                "ielts_speaking"
            }:
                action_type = "ai_teacher"

            if not task_title:
                continue

            task_date_iso = task_day.isoformat()

            used_minutes = daily_totals.get(
                task_date_iso,
                0
            )

            if (
                used_minutes
                +
                minutes
                >
                daily_minutes
            ):

                remaining_minutes = (
                    daily_minutes
                    -
                    used_minutes
                )

                if remaining_minutes < 5:
                    continue

                minutes = remaining_minutes

            daily_totals[
                task_date_iso
            ] = (
                used_minutes
                +
                minutes
            )

            clean_tasks.append({
                "date":
                    task_date_iso,
                "period_label":
                    period_label,
                "title":
                    task_title,
                "description":
                    task_description,
                "category":
                    category,
                "minutes":
                    minutes,
                "action_type":
                    action_type,
                "difficulty":
                    difficulty,
                "sort_order":
                    index
            })

        if not clean_tasks:

            raise ValueError(
                "Gemini did not create usable study tasks."
            )

        clean_plan_data = {
            "title":
                title,
            "summary":
                summary,
            "coach_message":
                coach_message,
            "milestones":
                clean_milestones,
            "tasks":
                clean_tasks
        }

        # ===========================
        # SAVE PROFILE
        # ===========================

        cursor.execute(
            """
            INSERT INTO study_profiles (
                user_id,
                main_goal,
                current_level,
                daily_minutes,
                days_per_week,
                has_exam,
                exam_type,
                exam_date,
                target_score,
                weak_areas
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(user_id)
            DO UPDATE SET
                main_goal = excluded.main_goal,
                current_level = excluded.current_level,
                daily_minutes = excluded.daily_minutes,
                days_per_week = excluded.days_per_week,
                has_exam = excluded.has_exam,
                exam_type = excluded.exam_type,
                exam_date = excluded.exam_date,
                target_score = excluded.target_score,
                weak_areas = excluded.weak_areas,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                user_id,
                main_goal,
                current_level,
                daily_minutes,
                days_per_week,
                1 if has_exam else 0,
                exam_type or None,
                exam_date,
                target_score or None,
                weak_areas or None
            )
        )

        # Archive previous plans rather than deleting history.
        cursor.execute(
            """
            UPDATE study_plans
            SET
                status = 'archived',
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s
            AND status = 'active'
            """,
            (user_id,)
        )

        cursor.execute(
            """
            INSERT INTO study_plans (
                user_id,
                period_type,
                title,
                summary,
                coach_message,
                goal,
                start_date,
                end_date,
                exam_date,
                target_score,
                plan_json,
                status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active')
RETURNING id
            """,
            (
                user_id,
                period_type,
                title,
                summary,
                coach_message,
                main_goal,
                start_day.isoformat(),
                end_day.isoformat(),
                exam_date,
                target_score or None,
                json.dumps(
                    clean_plan_data,
                    ensure_ascii=False
                )
            )
        )

        plan_id = cursor.fetchone()["id"]

        for task in clean_tasks:

            cursor.execute(
                """
                INSERT INTO study_tasks (
                    plan_id,
                    user_id,
                    task_date,
                    period_label,
                    title,
                    description,
                    category,
                    minutes,
                    action_type,
                    difficulty,
                    status,
                    sort_order
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                """,
                (
                    plan_id,
                    user_id,
                    task["date"],
                    task["period_label"],
                    task["title"],
                    task["description"],
                    task["category"],
                    task["minutes"],
                    task["action_type"],
                    task["difficulty"],
                    task["sort_order"]
                )
            )

        conn.commit()
        conn.close()

        session.pop(
            "active_study_task_id",
            None
        )

        return jsonify({
            "success":
                True,
            "plan_id":
                plan_id,
            "redirect":
                url_for(
                    "study_plan"
                )
        })

    except Exception as e:

        conn.rollback()
        conn.close()

        print(
            "AI STUDY PLAN ERROR:",
            repr(e)
        )

        return jsonify({
            "error":
                "LinguaMind could not create your plan right now. Please try again."
        }), 500


@app.route(
    "/api/study_tasks/<int:task_id>/toggle",
    methods=["POST"]
)
def toggle_study_task_api(
    task_id
):

    if "user_id" not in session:

        return jsonify({
            "error":
                "Login required."
        }), 401

    user_id = session["user_id"]

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            id,
            status
        FROM study_tasks
        WHERE id = %s
        AND user_id = %s
        LIMIT 1
        """,
        (
            task_id,
            user_id
        )
    )

    task = cursor.fetchone()

    if not task:

        conn.close()

        return jsonify({
            "error":
                "Task not found."
        }), 404

    if task["status"] == "completed":

        new_status = "pending"
        completed_at = None

    else:

        new_status = "completed"
        completed_at = datetime.now().isoformat(
            timespec="seconds"
        )

    cursor.execute(
        """
        UPDATE study_tasks
        SET
            status = %s,
            completed_at = %s
        WHERE id = %s
        AND user_id = %s
        """,
        (
            new_status,
            completed_at,
            task_id,
            user_id
        )
    )

    conn.commit()

    cursor.execute(
        """
        SELECT
            COUNT(*) AS total,
            COALESCE(
                SUM(
                    CASE
                        WHEN status = 'completed'
                        THEN 1
                        ELSE 0
                    END
                ),
                0
            ) AS done
        FROM study_tasks
        WHERE user_id = %s
        AND plan_id = (
            SELECT plan_id
            FROM study_tasks
            WHERE id = %s
            AND user_id = %s
            LIMIT 1
        )
        """,
        (
            user_id,
            task_id,
            user_id
        )
    )

    stats = cursor.fetchone()

    conn.close()

    if (
        new_status == "completed"
        and
        session.get(
            "active_study_task_id"
        )
        ==
        task_id
    ):

        session.pop(
            "active_study_task_id",
            None
        )

    total = (
        stats["total"]
        if stats
        else 0
    )

    done = (
        stats["done"]
        if stats
        else 0
    )

    percentage = (
        round(
            (
                done
                /
                total
            )
            *
            100
        )
        if total
        else 0
    )

    xp_awarded = 0

    if new_status == "completed":
        xp_awarded = award_xp(
            user_id,
            30,
            "study_task_completed",
            "study_task:" + str(task_id)
        )


    return jsonify({
        "success":
            True,
        "status":
            new_status,
        "done":
            done,
        "total":
            total,
        "percentage":
            percentage,
        "xp_awarded":
            xp_awarded
    })


@app.route(
    "/study_task/<int:task_id>/start"
)
def start_study_task(
    task_id
):

    if "user_id" not in session:

        return redirect(
            url_for("login")
        )

    pro_block = require_pro_page(
        "AI Study Coach"
    )

    if pro_block is not None:
        return pro_block

    user_id = session["user_id"]

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            st.id,
            st.action_type,
            st.category,
            sp.status AS plan_status
        FROM study_tasks AS st
        JOIN study_plans AS sp
            ON sp.id = st.plan_id
        WHERE st.id = %s
        AND st.user_id = %s
        LIMIT 1
        """,
        (
            task_id,
            user_id
        )
    )

    task = cursor.fetchone()
    conn.close()

    if (
        not task
        or
        task["plan_status"] != "active"
    ):

        return redirect(
            url_for("study_plan")
        )

    session[
        "active_study_task_id"
    ] = task_id

    action_type = task[
        "action_type"
    ]

    if action_type == "quiz":

        return redirect(
            url_for("quiz")
        )

    if action_type == "vocabulary":

        return redirect(
            url_for("vocabulary")
        )

    if action_type == "scan_text":

        return redirect(
            url_for("scan_text")
        )

    # AI Teacher is also the guided destination
    # for speaking, SAT Math and self-study tasks.
    return redirect(
        url_for("ai_teacher")
    )



# ===========================
# LOGOUT
# ===========================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("home"))


# ===========================
# RUN APP
# ===========================

@app.route("/favorites")
def favorites():

    if "user_id" not in session:
        return redirect(url_for("login"))

    db = get_db()

    words = db.execute(
        """
        SELECT *
        FROM vocabulary
        WHERE user_id = %s
        AND favorite = 1
        ORDER BY created_at DESC, id DESC
        """,
        (session["user_id"],)
    ).fetchall()

    db.close()

    return render_template(
        "favorites.html",
        words=words
    )


@app.route("/favorites/remove/<int:word_id>")
def remove_favorite(word_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    db = get_db()

    db.execute(
        """
        UPDATE vocabulary
        SET favorite = 0
        WHERE id = %s
        AND user_id = %s
        """,
        (
            word_id,
            session["user_id"]
        )
    )

    db.commit()
    db.close()

    return redirect(
        url_for("favorites")
    )

@app.route("/api/generate_quiz", methods=["POST"])
def generate_quiz_api():

    if "user_id" not in session:
        return jsonify({
            "error": "Login required."
        }), 401


    data = request.get_json(
        silent=True
    ) or {}


    category = str(
        data.get(
            "category",
            "grammar"
        )
    ).strip().lower()


    difficulty = str(
        data.get(
            "difficulty",
            "medium"
        )
    ).strip().lower()


    try:

        count = int(
            data.get(
                "count",
                5
            )
        )

    except (
        TypeError,
        ValueError
    ):

        count = 5


    # Frontend questions are generated
    # in small fast batches.
    count = max(
        1,
        min(
            count,
            10
        )
    )


    allowed_categories = {
        "grammar",
        "vocabulary",
        "ielts",
        "sat",
        "sat_math"
    }


    allowed_difficulties = {
        "easy",
        "medium",
        "hard",
        "mixed"
    }


    if category not in allowed_categories:

        return jsonify({
            "error": "Invalid category."
        }), 400


    if difficulty not in allowed_difficulties:

        return jsonify({
            "error": "Invalid difficulty."
        }), 400


    avoid_questions = data.get(
        "avoid_questions",
        []
    )


    if not isinstance(
        avoid_questions,
        list
    ):

        avoid_questions = []


    avoid_questions = [
        str(question).strip()
        for question in avoid_questions[-30:]
        if str(question).strip()
    ]


    category_prompts = {

        "grammar":
            "English grammar: tenses, conditionals, "
            "articles, prepositions, agreement, passive voice, "
            "sentence structure and common errors.",

        "vocabulary":
            "English vocabulary: meaning, synonym, antonym, "
            "collocation, phrasal verbs and vocabulary in context.",

        "ielts":
            "IELTS English: academic vocabulary, linking words, "
            "formal grammar, paraphrasing and academic style.",

        "sat":
            "Digital SAT Reading and Writing: transitions, "
            "Standard English Conventions, punctuation, "
            "vocabulary in context, concision and logic.",

        "sat_math":
            "Digital SAT Math: Algebra, Advanced Math, "
            "Problem-Solving and Data Analysis, Geometry and "
            "Trigonometry. Use realistic SAT-style word problems "
            "and equations. Questions must be solvable from the "
            "information given and have exactly one correct answer."
    }


    difficulty_prompts = {

        "easy":
            "Easy. Approximately A2-B1.",

        "medium":
            "Medium. Approximately B1-B2.",

        "hard":
            "Hard. Approximately B2-C1. "
            "Use realistic distractors and careful reasoning.",

        "mixed":
            "Mix easy, medium and hard questions."
    }


    avoid_text = ""

    if avoid_questions:

        avoid_text = (
            "\nDO NOT repeat these previous questions:\n"
            +
            "\n".join(
                "- " + question
                for question in avoid_questions
            )
        )


    nonce = os.urandom(
        6
    ).hex()


    prompt = f"""
Generate {count} fresh multiple-choice practice questions.

Category:
{category_prompts[category]}

Difficulty:
{difficulty_prompts[difficulty]}

Session:
{nonce}

Rules:
- exactly {count} questions
- exactly 4 options per question
- exactly one correct answer
- answer must be integer 0, 1, 2 or 3
- explanations must be short but show the essential reasoning
- for sat_math, verify every numerical answer before returning JSON
- for sat_math, vary Algebra, Advanced Math, Data Analysis, Geometry and Trigonometry
- questions must be original
- do not repeat questions
- vary the correct answer position
- realistic wrong answers
- return ONLY JSON
- no markdown
{avoid_text}

Format:

{{
  "questions": [
    {{
      "difficulty": "medium",
      "question": "Question",
      "options": [
        "A",
        "B",
        "C",
        "D"
      ],
      "answer": 0,
      "explanation": "Short explanation."
    }}
  ]
}}
"""


    try:

        client = genai.Client(
            api_key=GEMINI_API_KEY
        )


        response = client.interactions.create(
            model="gemini-3.6-flash",
            input=prompt
        )


        raw = (
            response.output_text
            or
            ""
        ).strip()


        start = raw.find(
            "{"
        )

        end = raw.rfind(
            "}"
        )


        if (
            start == -1
            or
            end == -1
        ):

            raise ValueError(
                "No JSON returned."
            )


        parsed = json.loads(
            raw[
                start:
                end + 1
            ]
        )


        questions = parsed.get(
            "questions",
            []
        )


        clean_questions = []


        for item in questions:

            if not isinstance(
                item,
                dict
            ):

                continue


            question = str(
                item.get(
                    "question",
                    ""
                )
            ).strip()


            options = item.get(
                "options",
                []
            )


            answer = item.get(
                "answer"
            )


            explanation = str(
                item.get(
                    "explanation",
                    ""
                )
            ).strip()


            item_difficulty = str(
                item.get(
                    "difficulty",
                    difficulty
                )
            ).strip().lower()


            if difficulty != "mixed":

                item_difficulty = difficulty


            if item_difficulty not in {
                "easy",
                "medium",
                "hard"
            }:

                item_difficulty = "medium"


            if (
                not question
                or
                not isinstance(
                    options,
                    list
                )
                or
                len(options) != 4
                or
                not isinstance(
                    answer,
                    int
                )
                or
                answer not in {
                    0,
                    1,
                    2,
                    3
                }
            ):

                continue


            clean_options = [
                str(option).strip()
                for option in options
            ]


            if any(
                not option
                for option in clean_options
            ):

                continue


            if not explanation:

                explanation = (
                    "Review the correct option "
                    "and compare it with the other choices."
                )


            clean_questions.append({

                "difficulty":
                    item_difficulty,

                "question":
                    question,

                "options":
                    clean_options,

                "answer":
                    answer,

                "explanation":
                    explanation
            })


            if len(
                clean_questions
            ) >= count:

                break


        if not clean_questions:

            raise ValueError(
                "No valid questions generated."
            )


        return jsonify({
            "questions":
                clean_questions
        })


    except Exception as e:

        print(
            "AI QUIZ ERROR:",
            repr(e)
        )


        return jsonify({
            "error":
                "Could not generate AI quiz."
        }), 500


# ===========================
# PREMIUM ERROR PAGES
# ===========================

@app.errorhandler(404)
def page_not_found(error):

    return render_template(
        "404.html"
    ), 404


@app.errorhandler(500)
def internal_server_error(error):

    app.logger.error(
        "LinguaMind 500 error: %s",
        error
    )

    return render_template(
        "500.html"
    ), 500


# Local preview routes.
# These exist only so the premium error pages can be checked safely
# before LinguaMind is deployed publicly.

@app.route("/dev/preview/404")
def dev_preview_404():

    is_local_dev = (
        app.debug
        and
        request.remote_addr in {
            "127.0.0.1",
            "::1"
        }
    )

    if not is_local_dev:
        return redirect(
            url_for("home")
        )

    return render_template(
        "404.html"
    ), 404


@app.route("/dev/preview/500")
def dev_preview_500():

    is_local_dev = (
        app.debug
        and
        request.remote_addr in {
            "127.0.0.1",
            "::1"
        }
    )

    if not is_local_dev:
        return redirect(
            url_for("home")
        )

    return render_template(
        "500.html"
    ), 500

# ===========================
# PRODUCTION DATABASE SETUP
# ===========================

# ===========================
# PRODUCTION DATABASE SETUP
# ===========================



# =========================================================
# INSTAGRAM PROMO
# =========================================================

@app.route("/instagram-promo")
def instagram_promo():

    if "user_id" not in session:
        return redirect(
            url_for("login")
        )

    user_id = session["user_id"]

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            id,
            full_name,
            email,
            instagram_promo_claimed,
            instagram_promo_claimed_at,
            instagram_username,
            instagram_promo_status,
            instagram_promo_requested_at,
            subscription_status,
            subscription_expires_at
        FROM users
        WHERE id = %s
        LIMIT 1
    """, (user_id,))

    user = cursor.fetchone()
    conn.close()

    if not user:
        return redirect(
            url_for("dashboard")
        )

    return render_template(
        "instagram_promo.html",
        user=user
    )


@app.route(
    "/instagram-promo/request",
    methods=["POST"]
)
def request_instagram_promo():

    if "user_id" not in session:
        return redirect(
            url_for("login")
        )

    user_id = session["user_id"]

    instagram_username = str(
        request.form.get(
            "instagram_username",
            ""
        )
    ).strip()

    if instagram_username.startswith("@"):
        instagram_username = instagram_username[1:]

    if not instagram_username:
        return redirect(
            url_for("instagram_promo")
        )

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            instagram_promo_claimed,
            instagram_promo_status
        FROM users
        WHERE id = %s
        LIMIT 1
    """, (user_id,))

    user = cursor.fetchone()

    if not user:
        conn.close()
        return jsonify({
            "error": "User not found."
        }), 404

    if user["instagram_promo_claimed"]:
        conn.close()
        return redirect(
            url_for("instagram_promo")
        )

    if user["instagram_promo_status"] == "pending":
        conn.close()
        return redirect(
            url_for("instagram_promo")
        )

    cursor.execute("""
        UPDATE users
        SET
            instagram_username = %s,
            instagram_promo_status = 'pending',
            instagram_promo_requested_at = CURRENT_TIMESTAMP
        WHERE id = %s
          AND instagram_promo_claimed = FALSE
    """, (
        instagram_username,
        user_id
    ))

    conn.commit()
    conn.close()

    return redirect(
        url_for("instagram_promo")
    )


# =========================================================
# ADMIN PANEL
# =========================================================

ADMIN_EMAIL = "baxtiyorxamidov941@gmail.com"


def get_current_admin():

    user_id = session.get("user_id")

    if not user_id:
        return None

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            id,
            full_name,
            email,
            is_admin
        FROM users
        WHERE id = %s
        LIMIT 1
    """, (user_id,))

    admin = cursor.fetchone()
    conn.close()

    if not admin:
        return None

    if not admin["is_admin"]:
        return None

    if admin["email"].lower() != ADMIN_EMAIL:
        return None

    return admin


@app.route("/admin")
def admin_dashboard():

    admin = get_current_admin()

    if not admin:
        return redirect(
            url_for("dashboard")
        )

    search = str(
        request.args.get("search", "")
    ).strip()

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM users
    """)
    total_users = cursor.fetchone()["total"]

    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM users
        WHERE subscription_status = 'active'
          AND plan IN (
              'pro_monthly',
              'pro_yearly'
          )
    """)
    pro_users = cursor.fetchone()["total"]

    free_users = total_users - pro_users

    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM users
        WHERE is_admin = TRUE
          AND LOWER(email) = LOWER(%s)
    """, (ADMIN_EMAIL,))
    admin_users = cursor.fetchone()["total"]

    if search:
        like_search = "%" + search + "%"

        cursor.execute("""
            SELECT
                id,
                full_name,
                email,
                plan,
                subscription_status,
                subscription_started_at,
                subscription_expires_at,
                is_admin,
                created_at,
                instagram_promo_claimed,
                instagram_promo_claimed_at,
                instagram_username,
                instagram_promo_status,
                instagram_promo_requested_at
            FROM users
            WHERE
                full_name ILIKE %s
                OR email ILIKE %s
            ORDER BY id DESC
        """, (
            like_search,
            like_search
        ))

    else:
        cursor.execute("""
            SELECT
                id,
                full_name,
                email,
                plan,
                subscription_status,
                subscription_started_at,
                subscription_expires_at,
                is_admin,
                created_at,
                instagram_promo_claimed,
                instagram_promo_claimed_at,
                instagram_username,
                instagram_promo_status,
                instagram_promo_requested_at
            FROM users
            ORDER BY id DESC
        """)

    users = cursor.fetchall()
    conn.close()

    return render_template(
        "admin.html",
        admin=admin,
        users=users,
        search=search,
        total_users=total_users,
        pro_users=pro_users,
        free_users=free_users,
        admin_users=admin_users
    )


# =========================================================
# ADMIN - GIVE 1 MONTH PRO
# =========================================================

@app.route(
    "/admin/user/<int:user_id>/give-pro",
    methods=["POST"]
)
def admin_give_pro(user_id):

    admin = get_current_admin()

    if not admin:
        return jsonify({
            "error": "Admin access required."
        }), 403

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            id,
            subscription_status,
            subscription_expires_at
        FROM users
        WHERE id = %s
        LIMIT 1
    """, (user_id,))

    user = cursor.fetchone()

    if not user:
        conn.close()
        return jsonify({
            "error": "User not found."
        }), 404

    now = datetime.utcnow().replace(
        microsecond=0
    )

    start_from = now
    current_expiry = user["subscription_expires_at"]

    if (
        user["subscription_status"] == "active"
        and current_expiry
    ):
        try:
            expiry_dt = datetime.fromisoformat(
                str(current_expiry)
            )
            if expiry_dt > now:
                start_from = expiry_dt
        except ValueError:
            pass

    new_expiry = add_calendar_month(start_from)

    cursor.execute("""
        UPDATE users
        SET
            plan = 'pro_monthly',
            subscription_status = 'active',
            subscription_started_at = %s,
            subscription_expires_at = %s
        WHERE id = %s
    """, (
        now.isoformat(),
        new_expiry.isoformat(),
        user_id
    ))

    conn.commit()
    conn.close()

    return redirect(
        url_for("admin_dashboard")
    )


# =========================================================
# ADMIN - GIVE 1 YEAR PRO
# =========================================================

@app.route(
    "/admin/user/<int:user_id>/give-pro-year",
    methods=["POST"]
)
def admin_give_pro_year(user_id):

    admin = get_current_admin()

    if not admin:
        return jsonify({
            "error": "Admin access required."
        }), 403

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            id,
            subscription_status,
            subscription_expires_at
        FROM users
        WHERE id = %s
        LIMIT 1
    """, (user_id,))

    user = cursor.fetchone()

    if not user:
        conn.close()
        return jsonify({
            "error": "User not found."
        }), 404

    now = datetime.utcnow().replace(
        microsecond=0
    )

    start_from = now
    current_expiry = user["subscription_expires_at"]

    if (
        user["subscription_status"] == "active"
        and current_expiry
    ):
        try:
            expiry_dt = datetime.fromisoformat(
                str(current_expiry)
            )
            if expiry_dt > now:
                start_from = expiry_dt
        except ValueError:
            pass

    new_expiry = add_calendar_year(start_from)

    cursor.execute("""
        UPDATE users
        SET
            plan = 'pro_yearly',
            subscription_status = 'active',
            subscription_started_at = %s,
            subscription_expires_at = %s
        WHERE id = %s
    """, (
        now.isoformat(),
        new_expiry.isoformat(),
        user_id
    ))

    conn.commit()
    conn.close()

    return redirect(
        url_for("admin_dashboard")
    )


# =========================================================
# ADMIN - APPROVE INSTAGRAM PROMO
# =========================================================

@app.route(
    "/admin/user/<int:user_id>/instagram-approve",
    methods=["POST"]
)
def admin_instagram_approve(user_id):

    admin = get_current_admin()

    if not admin:
        return jsonify({
            "error": "Admin access required."
        }), 403

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            id,
            plan,
            instagram_promo_claimed,
            instagram_promo_status,
            instagram_username,
            subscription_status,
            subscription_expires_at
        FROM users
        WHERE id = %s
        LIMIT 1
    """, (user_id,))

    user = cursor.fetchone()

    if not user:
        conn.close()
        return jsonify({
            "error": "User not found."
        }), 404

    if user["instagram_promo_claimed"]:
        conn.close()
        return redirect(
            url_for("admin_dashboard")
        )

    if user["instagram_promo_status"] != "pending":
        conn.close()
        return redirect(
            url_for("admin_dashboard")
        )

    now = datetime.utcnow().replace(
        microsecond=0
    )

    start_from = now
    current_expiry = user["subscription_expires_at"]

    if (
        user["subscription_status"] == "active"
        and current_expiry
    ):
        try:
            expiry_dt = datetime.fromisoformat(
                str(current_expiry)
            )
            if expiry_dt > now:
                start_from = expiry_dt
        except ValueError:
            pass

    new_expiry = add_calendar_month(start_from)

    new_plan = "pro_monthly"

    if (
        user["subscription_status"] == "active"
        and user["plan"] in {
            "pro_monthly",
            "pro_yearly"
        }
    ):
        new_plan = user["plan"]

    cursor.execute("""
        UPDATE users
        SET
            plan = %s,
            subscription_status = 'active',
            subscription_started_at = %s,
            subscription_expires_at = %s,
            instagram_promo_status = 'approved',
            instagram_promo_claimed = TRUE,
            instagram_promo_claimed_at = CURRENT_TIMESTAMP
        WHERE id = %s
          AND instagram_promo_claimed = FALSE
          AND instagram_promo_status = 'pending'
    """, (
        new_plan,
        now.isoformat(),
        new_expiry.isoformat(),
        user_id
    ))

    conn.commit()
    conn.close()

    return redirect(
        url_for("admin_dashboard")
    )


# =========================================================
# ADMIN - REJECT INSTAGRAM PROMO
# =========================================================

@app.route(
    "/admin/user/<int:user_id>/instagram-reject",
    methods=["POST"]
)
def admin_instagram_reject(user_id):

    admin = get_current_admin()

    if not admin:
        return jsonify({
            "error": "Admin access required."
        }), 403

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE users
        SET
            instagram_promo_status = 'rejected'
        WHERE id = %s
          AND instagram_promo_claimed = FALSE
          AND instagram_promo_status = 'pending'
    """, (user_id,))

    conn.commit()
    conn.close()

    return redirect(
        url_for("admin_dashboard")
    )


# =========================================================
# ADMIN - REMOVE PRO
# =========================================================

@app.route(
    "/admin/user/<int:user_id>/remove-pro",
    methods=["POST"]
)
def admin_remove_pro(user_id):

    admin = get_current_admin()

    if not admin:
        return jsonify({
            "error": "Admin access required."
        }), 403

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE users
        SET
            plan = 'free',
            subscription_status = 'inactive',
            subscription_started_at = NULL,
            subscription_expires_at = NULL
        WHERE id = %s
    """, (user_id,))

    conn.commit()
    conn.close()

    return redirect(
        url_for("admin_dashboard")
    )



# =========================================================
# SECURITY - RESPONSE HEADERS
# =========================================================

@app.after_request
def add_security_headers(response):

    response.headers["X-Content-Type-Options"] = "nosniff"

    response.headers["X-Frame-Options"] = "DENY"

    response.headers["Referrer-Policy"] = (
        "strict-origin-when-cross-origin"
    )

    response.headers["Permissions-Policy"] = (
        "camera=(self), "
        "microphone=(self), "
        "geolocation=(), "
        "payment=()"
    )

    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "font-src 'self' data: https:; "
        "connect-src 'self' https:; "
        "media-src 'self' data: blob: https:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self';"
    )

    if app.config.get("SESSION_COOKIE_SECURE"):
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )

    return response



# =========================================================
# INFRASTRUCTURE HEALTH
# =========================================================

@app.route("/health")
@limiter.exempt
def health_check():
    db_ok = False
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 AS ok")
        row = cursor.fetchone()
        conn.close()
        db_ok = bool(row and int(row["ok"]) == 1)
    except Exception:
        db_ok = False

    status_code = 200 if db_ok else 503

    return jsonify({
        "status": "ok" if db_ok else "degraded",
        "database": db_ok,
        "rate_limit_storage": (
            "redis" if RATE_LIMIT_STORAGE_URI.startswith(("redis://", "rediss://")) else "memory"
        )
    }), status_code


if __name__ == "__main__":
    app.run(
        debug=False,
        port=8000
    )