from flask import (
    Flask, request, redirect, render_template_string,
    jsonify, g, session
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
import os
import re
import secrets
from datetime import datetime
import psycopg

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# ==========================================
# ADMINISTRATOR & KONFIGURACIA
# ==========================================

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    "b7f74e7e963982aa94bd28f1f38842b38ab69f2748736abbfa74abf75c8290c5"
)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.environ.get(
    "ADMIN_PASSWORD_HASH",
    "scrypt:32768:8:1$IQmPlVdN4QAOMulj$4c9c02f9b3b8f02afc8f149091d0e050e376061af8f84ee901dc0753ea07a38d315f19ed0b4ad4277b6f3d9efba71d7b6fe727ec090a48fe583c60595f87e791"
)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
_DB_READY = False


def open_db():
    if not DATABASE_URL:
        raise RuntimeError(
            "Chýba premenná prostredia DATABASE_URL (connection string z Neonu)."
        )
    return psycopg.connect(DATABASE_URL)


def ensure_schema(conn):
    global _DB_READY
    if _DB_READY:
        return

    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id SERIAL PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id SERIAL PRIMARY KEY,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                visitor_id TEXT
            )
        """)

        cur.execute("""
            ALTER TABLE posts
            ADD COLUMN IF NOT EXISTS visitor_id TEXT
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS likes (
                id SERIAL PRIMARY KEY,
                post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                visitor_id TEXT NOT NULL,
                UNIQUE(post_id, visitor_id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id SERIAL PRIMARY KEY,
                post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                visitor_id TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                visitor_id TEXT PRIMARY KEY,
                nickname TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS polls (
                id SERIAL PRIMARY KEY,
                post_id INTEGER NOT NULL UNIQUE REFERENCES posts(id) ON DELETE CASCADE
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS poll_options (
                id SERIAL PRIMARY KEY,
                poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
                option_text TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS poll_votes (
                id SERIAL PRIMARY KEY,
                poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
                option_id INTEGER NOT NULL REFERENCES poll_options(id) ON DELETE CASCADE,
                visitor_id TEXT NOT NULL,
                UNIQUE(poll_id, visitor_id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_rooms (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT ''
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id SERIAL PRIMARY KEY,
                room_id INTEGER NOT NULL REFERENCES chat_rooms(id) ON DELETE CASCADE,
                visitor_id TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS direct_messages (
                id SERIAL PRIMARY KEY,
                sender_account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                recipient_account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_read INTEGER NOT NULL DEFAULT 0
            )
        """)

        cur.execute("SELECT COUNT(*) FROM chat_rooms")
        room_count = cur.fetchone()[0]
        if room_count == 0:
            default_rooms = [
                ("💬 Všeobecný pokec", "Voľná debata o všetkom možnom"),
                ("🎮 Hry a zábava", "Pokec o hrách, filmoch a seriáloch"),
                ("🌙 Nočná zmena", "Pre tých, ktorí ešte nespia")
            ]
            cur.executemany(
                "INSERT INTO chat_rooms (name, description) VALUES (%s, %s)",
                default_rooms
            )

    conn.commit()
    _DB_READY = True


def get_db():
    conn = open_db()
    ensure_schema(conn)
    return conn


def init_db():
    if not DATABASE_URL:
        return
    try:
        with get_db():
            pass
    except Exception as exc:
        app.logger.warning("Inicializácia DB pri štarte sa odložila: %s", exc)


init_db()


# ==========================================
# IDENTIFIKACIA POUZIVATELA A UCET
# ==========================================

def get_current_account():
    account_id = session.get("account_id")
    if not account_id:
        return None

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username FROM accounts WHERE id = %s",
                (account_id,)
            )
            row = cur.fetchone()

    if not row:
        session.pop("account_id", None)
        return None

    return {"id": row[0], "username": row[1]}


def get_unread_dm_count(account_id):
    if not account_id:
        return 0
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM direct_messages WHERE recipient_account_id = %s AND is_read = 0",
                (account_id,)
            )
            row = cur.fetchone()
    return row[0] if row else 0


def migrate_visitor_to_account(old_visitor_id, account_id, username):
    account_key = f"acc:{account_id}"

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT nickname FROM users WHERE visitor_id = %s",
                (old_visitor_id,)
            )
            old_user = cur.fetchone()

            preferred_nickname = username
            if old_user and old_user[0] and old_user[0] != "Anonymný používateľ":
                preferred_nickname = old_user[0][:20]

            cur.execute("""
                INSERT INTO users (visitor_id, nickname)
                VALUES (%s, %s)
                ON CONFLICT(visitor_id) DO UPDATE SET nickname = EXCLUDED.nickname
            """, (account_key, preferred_nickname))

            if old_visitor_id and old_visitor_id != account_key:
                cur.execute(
                    "UPDATE posts SET visitor_id = %s WHERE visitor_id = %s",
                    (account_key, old_visitor_id)
                )
                cur.execute(
                    "UPDATE comments SET visitor_id = %s WHERE visitor_id = %s",
                    (account_key, old_visitor_id)
                )
                cur.execute(
                    "UPDATE chat_messages SET visitor_id = %s WHERE visitor_id = %s",
                    (account_key, old_visitor_id)
                )

                cur.execute(
                    "SELECT id, post_id FROM likes WHERE visitor_id = %s",
                    (old_visitor_id,)
                )
                old_likes = cur.fetchall()
                for like_id, post_id in old_likes:
                    cur.execute(
                        "SELECT 1 FROM likes WHERE post_id = %s AND visitor_id = %s",
                        (post_id, account_key)
                    )
                    exists = cur.fetchone()
                    if exists:
                        cur.execute("DELETE FROM likes WHERE id = %s", (like_id,))
                    else:
                        cur.execute("UPDATE likes SET visitor_id = %s WHERE id = %s", (account_key, like_id))

                cur.execute(
                    "SELECT id, poll_id FROM poll_votes WHERE visitor_id = %s",
                    (old_visitor_id,)
                )
                old_votes = cur.fetchall()
                for vote_id, poll_id in old_votes:
                    cur.execute(
                        "SELECT 1 FROM poll_votes WHERE poll_id = %s AND visitor_id = %s",
                        (poll_id, account_key)
                    )
                    exists = cur.fetchone()
                    if exists:
                        cur.execute("DELETE FROM poll_votes WHERE id = %s", (vote_id,))
                    else:
                        cur.execute("UPDATE poll_votes SET visitor_id = %s WHERE id = %s", (account_key, vote_id))

        conn.commit()

    return account_key


@app.before_request
def identify_visitor():
    cookie_visitor_id = request.cookies.get("visitor_id")

    if not cookie_visitor_id or len(cookie_visitor_id) > 100 or cookie_visitor_id.startswith("acc:"):
        cookie_visitor_id = secrets.token_urlsafe(32)
        g.new_visitor = True
    else:
        g.new_visitor = False

    g.cookie_visitor_id = cookie_visitor_id
    g.account = get_current_account()

    if g.account:
        g.visitor_id = f"acc:{g.account['id']}"
        default_nick = g.account["username"][:20]
    else:
        g.visitor_id = cookie_visitor_id
        default_nick = "Anonymný používateľ"

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (visitor_id, nickname)
                VALUES (%s, %s)
                ON CONFLICT (visitor_id) DO NOTHING
            """, (g.visitor_id, default_nick))
        conn.commit()


@app.after_request
def save_visitor_cookie(response):
    if getattr(g, "new_visitor", False):
        response.set_cookie(
            "visitor_id",
            g.cookie_visitor_id,
            max_age=365 * 24 * 60 * 60,
            httponly=True,
            samesite="Lax",
            secure=request.is_secure
        )
    return response


def get_nickname():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT nickname
                FROM users
                WHERE visitor_id = %s
            """, (g.visitor_id,))
            result = cur.fetchone()

    if result:
        return result[0]

    return g.account["username"][:20] if g.account else "Anonymný používateľ"


def parse_account_id_from_visitor(visitor_id):
    if visitor_id and visitor_id.startswith("acc:"):
        raw = visitor_id[4:]
        if raw.isdigit():
            return int(raw)
    return None


# ==========================================
# HTML SABLONY PRE UCTY
# ==========================================

AUTH_HTML = """
<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} - Moja komunita</title>
<style>
* { box-sizing: border-box; }
body { background: #101827; color: white; font-family: Arial, sans-serif; margin: 0; padding: 20px; }
.container { max-width: 480px; margin: auto; }
header { text-align: center; padding: 20px 10px; }
h1 { color: #42a5ff; font-size: 30px; margin: 0 0 8px 0; }
.subtitle { color: #9aabc0; margin: 6px 0; font-size: 14px; }
.panel { background: #1d2b40; padding: 24px; border-radius: 15px; border: 1px solid #344761; }
input { width: 100%; margin-top: 10px; padding: 12px; background: #101827; border: 1px solid #344761; border-radius: 10px; color: white; font-size: 15px; }
button { width: 100%; margin-top: 16px; padding: 14px; background: #2689e8; border: none; border-radius: 9px; color: white; font-size: 16px; font-weight: bold; cursor: pointer; }
button:hover { background: #1472cb; }
.error { background: rgba(217, 54, 80, 0.18); border: 1px solid #d93650; color: #ff99aa; padding: 10px 12px; border-radius: 8px; margin-bottom: 12px; font-size: 14px; }
.switch-link { text-align: center; margin-top: 18px; font-size: 14px; color: #9aabc0; }
.switch-link a, .back-link { color: #42a5ff; text-decoration: none; }
.back-link { display: inline-block; margin-bottom: 15px; font-size: 14px; }
</style>
</head>
<body>
<div class="container">
    <a href="/" class="back-link">← Späť na nástenku</a>
    <header>
        <h1>{{ title }}</h1>
        <p class="subtitle">{{ subtitle }}</p>
    </header>
    <div class="panel">
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <form method="POST">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            <label class="subtitle" for="username">Používateľské meno (3–20 znakov)</label>
            <input id="username" type="text" name="username" maxlength="20" value="{{ username_val }}" placeholder="napr. marek_99" required>
            <label class="subtitle" for="password" style="display:block; margin-top:12px;">Heslo (aspoň 6 znakov)</label>
            <input id="password" type="password" name="password" placeholder="Zadaj heslo" required>
            {% if mode == "register" %}
                <label class="subtitle" for="password2" style="display:block; margin-top:12px;">Potvrď heslo</label>
                <input id="password2" type="password" name="password2" placeholder="Zopakuj heslo" required>
            {% endif %}
            <button type="submit">{{ button_text }}</button>
        </form>
        <div class="switch-link">
            {% if mode == "login" %}
                Nemáš ešte účet? <a href="/register">Zaregistruj sa</a>
            {% else %}
                Máš už účet? <a href="/login">Prihlás sa</a>
            {% endif %}
        </div>
    </div>
</div>
</body>
</html>
"""


def get_auth_csrf():
    if "auth_csrf" not in session:
        session["auth_csrf"] = secrets.token_urlsafe(32)
    return session["auth_csrf"]


def valid_auth_csrf():
    return secrets.compare_digest(
        request.form.get("csrf_token", ""),
        session.get("auth_csrf", "")
    )


# ==========================================
# HTML PRISPEVKOV
# ==========================================

POSTS_HTML = """
{% if posts %}
    {% for post in posts %}
        <div class="post" data-post-id="{{ post.id }}">
            {% if post.message %}
                <div class="post-message">{{ post.message }}</div>
            {% endif %}

            {% if post.poll %}
                <div class="poll-box" data-poll-id="{{ post.poll.id }}">
                    {% for opt in post.poll.options %}
                        {% set is_selected = (post.poll.user_vote == opt.id) %}
                        <button
                            type="button"
                            class="poll-option {% if is_selected %}selected{% endif %} {% if post.poll.user_vote %}voted{% endif %}"
                            onclick="votePoll({{ post.poll.id }}, {{ opt.id }}, this)"
                        >
                            <div class="poll-bar" style="width: {{ opt.percent if post.poll.user_vote else 0 }}%;"></div>
                            <span class="poll-text">{% if is_selected %}✓ {% endif %}{{ opt.text }}</span>
                            {% if post.poll.user_vote %}
                                <span class="poll-stats">{{ opt.percent }}% ({{ opt.votes }})</span>
                            {% endif %}
                        </button>
                    {% endfor %}
                    <div class="poll-total">
                        Spolu hlasov: {{ post.poll.total_votes }}
                        {% if not post.poll.user_vote %}· Klikni pre hlasovanie{% endif %}
                    </div>
                </div>
            {% endif %}

            <div class="post-info">
                {% if post.author_account_id %}
                    <a href="/messages/{{ post.author_account_id }}" class="post-author-link" title="Poslať súkromnú správu">
                        <strong class="post-author">{{ post.nickname }}</strong>
                    </a>
                {% else %}
                    <strong class="post-author">{{ post.nickname }}</strong>
                {% endif %}
                · {{ post.created_at }}
            </div>

            <div class="like-area">
                <button
                    type="button"
                    class="like-button {% if post.liked %}liked{% endif %}"
                    data-post-id="{{ post.id }}"
                    aria-pressed="{{ 'true' if post.liked else 'false' }}"
                    onclick="toggleLike(this)"
                >
                    <span class="heart">{% if post.liked %}♥{% else %}♡{% endif %}</span>
                    <span>Páči sa mi</span>
                    <span class="like-count">{{ post.like_count }}</span>
                </button>

                <button
                    type="button"
                    class="comment-toggle-button"
                    onclick="toggleComments({{ post.id }})"
                >
                    <span>💬 Komentáre</span>
                    <span class="comment-count">{{ post.comment_count }}</span>
                </button>
            </div>

            <div class="comments-section" id="comments-section-{{ post.id }}">
                {% if post.comments %}
                    <div class="comments-list">
                        {% for c in post.comments %}
                            <div class="comment-item">
                                <div class="comment-meta">
                                    {% if c.author_account_id %}
                                        <a href="/messages/{{ c.author_account_id }}" class="post-author-link" title="Poslať súkromnú správu">
                                            <strong class="post-author">{{ c.nickname }}</strong>
                                        </a>
                                    {% else %}
                                        <strong class="post-author">{{ c.nickname }}</strong>
                                    {% endif %}
                                    · {{ c.created_at }}
                                </div>
                                <div class="comment-text">{{ c.message }}</div>
                            </div>
                        {% endfor %}
                    </div>
                {% else %}
                    <div class="comments-empty">Zatiaľ žiadne komentáre. Napíš prvý!</div>
                {% endif %}

                <form class="comment-form" onsubmit="submitComment(event, {{ post.id }}, this)">
                    <input
                        type="text"
                        class="comment-input"
                        maxlength="300"
                        placeholder="Napíš komentár..."
                        required
                    >
                    <button type="submit" class="comment-send-btn">Odoslať</button>
                </form>
            </div>
        </div>
    {% endfor %}
{% else %}
    <div class="empty">Zatiaľ tu nikto nič nepridal. Buď prvý!</div>
{% endif %}
"""


# ==========================================
# HLAVNA STRANKA
# ==========================================

HTML = """
<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Moja komunita</title>
<style>
* { box-sizing: border-box; }
body { background: #101827; color: white; font-family: Arial, sans-serif; margin: 0; padding: 20px; }
.container { max-width: 650px; margin: auto; }
header { text-align: center; padding: 20px 10px; }
h1 { color: #42a5ff; font-size: 32px; margin: 0 0 8px 0; }
.subtitle { color: #9aabc0; margin: 6px 0; }

.account-bar {
    background: #152133; border: 1px solid #344761; border-radius: 12px;
    padding: 12px 16px; margin-bottom: 14px; display: flex;
    justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; font-size: 14px;
}
.account-actions { display: flex; gap: 10px; align-items: center; }
.account-link { color: #42a5ff; text-decoration: none; font-weight: bold; font-size: 13px; }
.logout-inline-btn {
    background: #293c55; color: #d6e4f5; border: 1px solid #3d5678;
    border-radius: 7px; padding: 6px 11px; font-size: 12px; cursor: pointer;
}

.nav-tabs { display: flex; gap: 8px; justify-content: center; margin-bottom: 22px; flex-wrap: wrap; }
.nav-tab {
    flex: 1; min-width: 140px; text-align: center; padding: 11px 10px; background: #1d2b40;
    border: 1px solid #344761; border-radius: 10px; color: #b6c5d8;
    text-decoration: none; font-weight: bold; font-size: 14px;
    display: inline-flex; justify-content: center; align-items: center; gap: 6px;
}
.nav-tab.active { background: #2689e8; border-color: #2689e8; color: white; }
.unread-badge {
    background: #ff405e; color: white; border-radius: 999px;
    padding: 1px 7px; font-size: 11px; font-weight: bold;
}

.panel { background: #1d2b40; padding: 20px; border-radius: 15px; margin-bottom: 25px; }
textarea {
    width: 100%; height: 100px; padding: 13px; border: 1px solid #344761;
    border-radius: 10px; background: #101827; color: white; resize: vertical; font-size: 16px;
}
input[type=text] {
    width: 100%; margin-top: 10px; padding: 12px; background: #101827;
    border: 1px solid #344761; border-radius: 10px; color: white; font-size: 15px;
}
button { cursor: pointer; }
.submit-button, .nickname-button {
    width: 100%; margin-top: 14px; padding: 14px; background: #2689e8;
    border: none; border-radius: 9px; color: white; font-size: 16px;
}
.submit-button:hover, .nickname-button:hover { background: #1472cb; }

.poll-toggle-btn {
    margin-top: 12px; padding: 9px 14px; background: #293c55;
    border: 1px solid #3d5678; border-radius: 8px; color: #d6e4f5; font-size: 14px;
}
.poll-creator {
    display: none; margin-top: 12px; padding: 14px; background: #152133;
    border: 1px solid #344761; border-radius: 10px;
}
.poll-creator.open { display: block; }
.poll-option-row { display: flex; gap: 8px; align-items: center; }
.poll-option-row input { margin-top: 8px; }
.remove-option-btn {
    margin-top: 8px; padding: 11px 13px; background: #3a2330;
    border: 1px solid #63344b; border-radius: 8px; color: #ff8fa3; font-size: 14px;
}
.add-option-btn {
    margin-top: 10px; padding: 8px 12px; background: transparent;
    border: 1px dashed #42a5ff; border-radius: 8px; color: #42a5ff; font-size: 13px;
}

.poll-box { margin-top: 14px; display: flex; flex-direction: column; gap: 8px; }
.poll-option {
    position: relative; width: 100%; text-align: left; padding: 11px 14px;
    background: #101827; border: 1px solid #344761; border-radius: 9px;
    color: white; font-size: 14px; overflow: hidden; display: flex;
    justify-content: space-between; align-items: center; gap: 10px;
}
.poll-option.selected { border-color: #42a5ff; font-weight: bold; }
.poll-bar {
    position: absolute; left: 0; top: 0; bottom: 0;
    background: rgba(38, 137, 232, 0.25); z-index: 0;
}
.poll-option.selected .poll-bar { background: rgba(66, 165, 255, 0.38); }
.poll-text, .poll-stats { position: relative; z-index: 1; }
.poll-stats { color: #b6c5d8; font-size: 13px; white-space: nowrap; }
.poll-total { font-size: 12px; color: #8ca0b8; margin-top: 2px; }

.post { background: #1d2b40; padding: 18px; border-radius: 12px; margin-bottom: 13px; overflow-wrap: anywhere; }
.post-message { white-space: pre-wrap; font-size: 16px; line-height: 1.5; }
.post-info { color: #8ca0b8; font-size: 12px; margin-top: 12px; }
.post-author { color: #42a5ff; }
.post-author-link { text-decoration: none; }
.post-author-link:hover .post-author { text-decoration: underline; }
.error { color: #ff7777; margin-bottom: 12px; }
.empty { text-align: center; color: #8ca0b8; padding: 25px; }
.counter { text-align: right; color: #8ca0b8; font-size: 12px; margin-top: 5px; }
.refresh-info { color: #71869f; font-size: 12px; text-align: center; margin-bottom: 15px; }
.nickname-status { color: #8ca0b8; font-size: 13px; margin-top: 10px; }

.like-area {
    margin-top: 14px; border-top: 1px solid #344761; padding-top: 12px;
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
}
.like-button, .comment-toggle-button {
    display: inline-flex; align-items: center; gap: 8px; background: transparent;
    border: none; color: #b6c5d8; font-size: 14px; padding: 5px 8px; border-radius: 8px;
}
.like-button:hover, .comment-toggle-button:hover { background: #293c55; }
.heart { font-size: 24px; color: #aab8ca; }
.like-button.liked { color: #ff6b81; }
.like-button.liked .heart { color: #ff405e; }
.like-count, .comment-count { font-weight: bold; color: white; }

.comments-section {
    display: none; margin-top: 12px; padding: 12px;
    background: #152133; border: 1px solid #2e415c; border-radius: 10px;
}
.comments-section.open { display: block; }
.comments-list { display: flex; flex-direction: column; gap: 8px; margin-bottom: 10px; }
.comment-item {
    background: #101827; border: 1px solid #28384f; border-radius: 8px; padding: 9px 12px;
}
.comment-meta { font-size: 11px; color: #8ca0b8; margin-bottom: 4px; }
.comment-text { font-size: 14px; line-height: 1.4; white-space: pre-wrap; color: #e6eef8; }
.comments-empty { font-size: 13px; color: #7e93ad; margin-bottom: 10px; }
.comment-form { display: flex; gap: 8px; align-items: center; }
.comment-form input.comment-input {
    margin-top: 0; padding: 9px 12px; font-size: 14px; border-radius: 8px; flex: 1;
}
.comment-send-btn {
    padding: 9px 14px; background: #2689e8; border: none; border-radius: 8px;
    color: white; font-size: 13px; font-weight: bold; white-space: nowrap;
}
.comment-send-btn:hover { background: #1472cb; }

footer { text-align: center; margin-top: 30px; padding-bottom: 20px; font-size: 13px; }
footer a { color: #71869f; text-decoration: none; }
footer a:hover { color: #42a5ff; }
</style>
</head>
<body>
<div class="container">

<header>
    <h1>MOJA KOMUNITA</h1>
    <p class="subtitle">Komunitná nástenka a chat pre všetkých</p>
</header>

<div class="account-bar">
    {% if account %}
        <span>Prihlásený ako: <strong style="color: #42a5ff;">@{{ account.username }}</strong></span>
        <div class="account-actions">
            <form method="POST" action="/logout" style="margin: 0;">
                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                <button type="submit" class="logout-inline-btn">Odhlásiť sa</button>
            </form>
        </div>
    {% else %}
        <span>Režim: <strong>Anonymný návštevník</strong></span>
        <div class="account-actions">
            <a href="/login" class="account-link">Prihlásiť sa</a>
            <span>·</span>
            <a href="/register" class="account-link">Registrácia</a>
        </div>
    {% endif %}
</div>

<nav class="nav-tabs">
    <a href="/" class="nav-tab active">📋 Nástenka</a>
    <a href="/chat" class="nav-tab">💬 Chat</a>
    <a href="/messages" class="nav-tab">
        ✉️ Súkromné
        {% if unread_dm_count > 0 %}<span class="unread-badge">{{ unread_dm_count }}</span>{% endif %}
    </a>
</nav>

<div class="panel">
    <h2>👤 Tvoja prezývka</h2>
    <p class="subtitle">Ostatní návštevníci uvidia túto prezývku pri tvojich príspevkoch aj v chate.</p>
    <form id="nickname-form">
        <input type="text" id="nickname-input" maxlength="20" value="{{ nickname }}" placeholder="Zadaj svoju prezývku" required>
        <button class="nickname-button" type="submit">Uložiť prezývku</button>
    </form>
    <div id="nickname-status" class="nickname-status"></div>
</div>

<div class="panel">
    <h2>Vytvor príspevok</h2>
    <p class="subtitle">Napíš správu alebo vytvor anketu.</p>

    {% if message %}<div class="error">{{ message }}</div>{% endif %}

    <form method="POST" action="/">
        <textarea id="post-textarea" name="post" maxlength="500" placeholder="Čo máš na srdci? (alebo otázka k ankete)"></textarea>
        <div class="counter" id="char-counter">0 / 500 znakov</div>

        <button type="button" class="poll-toggle-btn" id="toggle-poll-btn">📊 Pridať anketu</button>

        <div class="poll-creator" id="poll-creator">
            <p class="subtitle" style="margin-top: 0;">Možnosti hlasovania (2 až 5 možností):</p>
            <div id="poll-options-container">
                <div class="poll-option-row">
                    <input type="text" name="poll_options[]" maxlength="80" placeholder="Možnosť 1">
                </div>
                <div class="poll-option-row">
                    <input type="text" name="poll_options[]" maxlength="80" placeholder="Možnosť 2">
                </div>
            </div>
            <button type="button" class="add-option-btn" id="add-option-btn">+ Pridať ďalšiu možnosť</button>
        </div>

        <button class="submit-button" type="submit">Pridať príspevok</button>
    </form>
</div>

<h2>Najnovšie príspevky</h2>
<div class="refresh-info">Príspevky sa automaticky aktualizujú každých 10 sekúnd.</div>
<div id="posts-container">{{ posts_html|safe }}</div>

<footer><a href="/admin">Administrácia</a></footer>
</div>

<script>
const textarea = document.getElementById("post-textarea");
const charCounter = document.getElementById("char-counter");
textarea.addEventListener("input", () => {
    charCounter.textContent = `${textarea.value.length} / 500 znakov`;
});

const togglePollBtn = document.getElementById("toggle-poll-btn");
const pollCreator = document.getElementById("poll-creator");
const pollOptionsContainer = document.getElementById("poll-options-container");
const addOptionBtn = document.getElementById("add-option-btn");

togglePollBtn.addEventListener("click", () => {
    const isOpen = pollCreator.classList.toggle("open");
    togglePollBtn.textContent = isOpen ? "✕ Zrušiť anketu" : "📊 Pridať anketu";
    if (!isOpen) {
        pollOptionsContainer.querySelectorAll("input").forEach(inp => inp.value = "");
    }
});

addOptionBtn.addEventListener("click", () => {
    const count = pollOptionsContainer.querySelectorAll(".poll-option-row").length;
    if (count >= 5) return;

    const row = document.createElement("div");
    row.className = "poll-option-row";

    const input = document.createElement("input");
    input.type = "text";
    input.name = "poll_options[]";
    input.maxLength = 80;
    input.placeholder = `Možnosť ${count + 1}`;

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "remove-option-btn";
    removeBtn.textContent = "✕";
    removeBtn.onclick = () => {
        row.remove();
        addOptionBtn.style.display =
            pollOptionsContainer.querySelectorAll(".poll-option-row").length >= 5 ? "none" : "inline-block";
    };

    row.appendChild(input);
    row.appendChild(removeBtn);
    pollOptionsContainer.appendChild(row);
    if (count + 1 >= 5) addOptionBtn.style.display = "none";
});

const openCommentPosts = new Set();

function toggleComments(postId) {
    const section = document.getElementById(`comments-section-${postId}`);
    if (!section) return;
    const isOpen = section.classList.toggle("open");
    if (isOpen) {
        openCommentPosts.add(String(postId));
    } else {
        openCommentPosts.delete(String(postId));
    }
}

async function submitComment(event, postId, form) {
    event.preventDefault();
    const input = form.querySelector(".comment-input");
    const btn = form.querySelector(".comment-send-btn");
    const text = input.value.trim();
    if (!text) return;

    btn.disabled = true;
    try {
        const response = await fetch(`/post/${postId}/comment`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message: text })
        });
        if (response.ok) {
            input.value = "";
            openCommentPosts.add(String(postId));
            await refreshPosts(true);
        }
    } finally {
        btn.disabled = false;
    }
}

async function refreshPosts(force = false) {
    const activeEl = document.activeElement;
    if (!force && activeEl && activeEl.classList.contains("comment-input")) {
        return;
    }

    try {
        const draftComments = {};
        document.querySelectorAll(".post").forEach(postEl => {
            const pid = postEl.dataset.postId;
            const inp = postEl.querySelector(".comment-input");
            if (inp && inp.value) {
                draftComments[pid] = inp.value;
            }
        });

        const response = await fetch("/posts", { cache: "no-store" });
        if (!response.ok) return;
        document.getElementById("posts-container").innerHTML = await response.text();

        openCommentPosts.forEach(pid => {
            const section = document.getElementById(`comments-section-${pid}`);
            if (section) section.classList.add("open");
        });

        Object.entries(draftComments).forEach(([pid, val]) => {
            const postEl = document.querySelector(`.post[data-post-id="${pid}"]`);
            const inp = postEl ? postEl.querySelector(".comment-input") : null;
            if (inp) inp.value = val;
        });
    } catch (e) {}
}
setInterval(() => refreshPosts(false), 10000);

async function votePoll(pollId, optionId, button) {
    const buttons = button.closest(".poll-box").querySelectorAll("button");
    buttons.forEach(b => b.disabled = true);
    try {
        const response = await fetch(`/poll/${pollId}/vote/${optionId}`, { method: "POST" });
        if (response.ok) await refreshPosts(true);
    } finally {
        buttons.forEach(b => b.disabled = false);
    }
}

async function toggleLike(button) {
    if (button.disabled) return;
    button.disabled = true;
    try {
        const response = await fetch("/like/" + button.dataset.postId, { method: "POST" });
        if (!response.ok) return;
        const data = await response.json();
        button.classList.toggle("liked", data.liked);
        button.setAttribute("aria-pressed", data.liked ? "true" : "false");
        button.querySelector(".heart").textContent = data.liked ? "♥" : "♡";
        button.querySelector(".like-count").textContent = data.count;
    } finally {
        button.disabled = false;
    }
}

document.getElementById("nickname-form").addEventListener("submit", async function(e) {
    e.preventDefault();
    const input = document.getElementById("nickname-input");
    const status = document.getElementById("nickname-status");
    const nickname = input.value.trim();
    if (!nickname) return;

    const response = await fetch("/nickname", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nickname })
    });
    const data = await response.json();
    if (!response.ok) {
        status.textContent = data.error || "Chyba pri ukladaní.";
        return;
    }
    status.textContent = "✓ Prezývka bola úspešne uložená.";
    await refreshPosts(true);
});
</script>
</body>
</html>
"""


# ==========================================
# HTML PRE CHATOVACIE MIESTNOSTI
# ==========================================

CHAT_ROOMS_HTML = """
<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chatovacie miestnosti - Moja komunita</title>
<style>
* { box-sizing: border-box; }
body { background: #101827; color: white; font-family: Arial, sans-serif; margin: 0; padding: 20px; }
.container { max-width: 650px; margin: auto; }
header { text-align: center; padding: 20px 10px; }
h1 { color: #42a5ff; font-size: 32px; margin: 0 0 8px 0; }
.subtitle { color: #9aabc0; margin: 6px 0; }
.nav-tabs { display: flex; gap: 8px; justify-content: center; margin-bottom: 22px; flex-wrap: wrap; }
.nav-tab {
    flex: 1; min-width: 140px; text-align: center; padding: 11px 10px; background: #1d2b40;
    border: 1px solid #344761; border-radius: 10px; color: #b6c5d8;
    text-decoration: none; font-weight: bold; font-size: 14px;
    display: inline-flex; justify-content: center; align-items: center; gap: 6px;
}
.nav-tab.active { background: #2689e8; border-color: #2689e8; color: white; }
.unread-badge {
    background: #ff405e; color: white; border-radius: 999px;
    padding: 1px 7px; font-size: 11px; font-weight: bold;
}
.room-card {
    display: block; background: #1d2b40; padding: 18px 20px; border-radius: 14px;
    margin-bottom: 14px; border: 1px solid #344761; text-decoration: none; color: white;
}
.room-card:hover { border-color: #42a5ff; }
.room-header { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
.room-title { font-size: 19px; font-weight: bold; color: #42a5ff; }
.room-badge {
    background: #101827; border: 1px solid #344761; color: #9aabc0;
    padding: 4px 10px; border-radius: 20px; font-size: 12px; white-space: nowrap;
}
.room-desc { color: #b6c5d8; font-size: 14px; margin-top: 8px; }
.user-info-bar {
    background: #152133; border: 1px solid #344761; padding: 12px 16px;
    border-radius: 10px; margin-bottom: 20px; font-size: 14px; color: #9aabc0;
    display: flex; justify-content: space-between; align-items: center;
}
.user-info-bar a { color: #42a5ff; text-decoration: none; font-size: 13px; }
</style>
</head>
<body>
<div class="container">
<header>
    <h1>MOJA KOMUNITA</h1>
    <p class="subtitle">Vyber si miestnosť a zapoj sa do debaty</p>
</header>
<nav class="nav-tabs">
    <a href="/" class="nav-tab">📋 Nástenka</a>
    <a href="/chat" class="nav-tab active">💬 Chat</a>
    <a href="/messages" class="nav-tab">
        ✉️ Súkromné
        {% if unread_dm_count > 0 %}<span class="unread-badge">{{ unread_dm_count }}</span>{% endif %}
    </a>
</nav>
<div class="user-info-bar">
    <span>
        Vystupuješ ako: <strong style="color: white;">{{ nickname }}</strong>
        {% if account %}<small style="color: #42a5ff;">(@{{ account.username }})</small>{% endif %}
    </span>
    <a href="/">Zmeniť prezývku</a>
</div>
{% for room in rooms %}
    <a href="/chat/{{ room.id }}" class="room-card">
        <div class="room-header">
            <span class="room-title">{{ room.name }}</span>
            <span class="room-badge">{{ room.message_count }} správ</span>
        </div>
        {% if room.description %}<div class="room-desc">{{ room.description }}</div>{% endif %}
    </a>
{% else %}
    <p class="subtitle" style="text-align: center;">Zatiaľ nie sú vytvorené žiadne miestnosti.</p>
{% endfor %}
</div>
</body>
</html>
"""

CHAT_ROOM_DETAIL_HTML = """
<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ room.name }} - Chat</title>
<style>
* { box-sizing: border-box; }
body {
    background: #101827; color: white; font-family: Arial, sans-serif;
    margin: 0; padding: 15px; height: 100dvh; display: flex; flex-direction: column;
}
.chat-wrapper { max-width: 650px; width: 100%; margin: 0 auto; display: flex; flex-direction: column; height: 100%; }
.chat-header {
    background: #1d2b40; padding: 14px 18px; border-radius: 14px;
    border: 1px solid #344761; display: flex; justify-content: space-between;
    align-items: center; gap: 10px; margin-bottom: 12px;
}
.back-btn { color: #42a5ff; text-decoration: none; font-weight: bold; font-size: 14px; }
.room-info { text-align: center; }
.room-info h2 { margin: 0; font-size: 18px; color: white; }
.room-info small { color: #8ca0b8; font-size: 12px; }
.messages-box {
    flex: 1; background: #152133; border: 1px solid #344761; border-radius: 14px;
    padding: 15px; overflow-y: auto; display: flex; flex-direction: column;
    gap: 10px; margin-bottom: 12px;
}
.msg {
    max-width: 80%; padding: 10px 14px; border-radius: 14px; background: #1d2b40;
    border: 1px solid #2c3f59; align-self: flex-start; overflow-wrap: anywhere;
}
.msg.mine { align-self: flex-end; background: #1a4b82; border-color: #2689e8; }
.msg-meta { font-size: 11px; color: #8ca0b8; margin-bottom: 4px; }
.msg.mine .msg-meta { color: #b8dcff; text-align: right; }
.msg-author { font-weight: bold; color: #42a5ff; }
.msg.mine .msg-author { color: white; }
.msg-text { font-size: 15px; line-height: 1.4; white-space: pre-wrap; }
.chat-form { display: flex; gap: 10px; }
.chat-input {
    flex: 1; padding: 13px 15px; background: #1d2b40; border: 1px solid #344761;
    border-radius: 12px; color: white; font-size: 15px;
}
.send-btn {
    padding: 13px 20px; background: #2689e8; border: none; border-radius: 12px;
    color: white; font-weight: bold; font-size: 15px; cursor: pointer;
}
.empty-chat { text-align: center; color: #71869f; margin: auto; font-size: 14px; }
</style>
</head>
<body>
<div class="chat-wrapper">
<div class="chat-header">
    <a href="/chat" class="back-btn">← Miestnosti</a>
    <div class="room-info">
        <h2>{{ room.name }}</h2>
        <small>Tvoja prezývka: {{ nickname }}</small>
    </div>
    <div style="width: 75px;"></div>
</div>

<div class="messages-box" id="messages-box">
    <div class="empty-chat">Načítavam správy...</div>
</div>

<form class="chat-form" id="chat-form">
    <input type="text" id="chat-input" class="chat-input" maxlength="400" placeholder="Napíš správu do miestnosti..." autocomplete="off" required>
    <button type="submit" class="send-btn" id="send-btn">Odoslať</button>
</form>
</div>

<script>
const roomId = {{ room.id }};
const messagesBox = document.getElementById("messages-box");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");
let lastRenderedJson = "";

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

async function loadMessages(forceScroll = false) {
    try {
        const response = await fetch(`/chat/${roomId}/messages`, { cache: "no-store" });
        if (!response.ok) return;
        const messages = await response.json();
        const currentJson = JSON.stringify(messages);
        if (currentJson === lastRenderedJson) return;
        lastRenderedJson = currentJson;

        const isNearBottom = messagesBox.scrollHeight - messagesBox.scrollTop - messagesBox.clientHeight < 90;

        if (messages.length === 0) {
            messagesBox.innerHTML = '<div class="empty-chat">Zatiaľ tu nie sú žiadne správy. Napíš prvú!</div>';
            return;
        }

        messagesBox.innerHTML = messages.map(m => `
            <div class="msg ${m.is_mine ? 'mine' : ''}">
                <div class="msg-meta">
                    <span class="msg-author">${escapeHtml(m.nickname)}</span> · ${escapeHtml(m.created_at)}
                </div>
                <div class="msg-text">${escapeHtml(m.message)}</div>
            </div>
        `).join("");

        if (forceScroll || isNearBottom) {
            messagesBox.scrollTop = messagesBox.scrollHeight;
        }
    } catch (e) {}
}

chatForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = chatInput.value.trim();
    if (!text) return;
    sendBtn.disabled = true;
    try {
        const response = await fetch(`/chat/${roomId}/send`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message: text })
        });
        if (response.ok) {
            chatInput.value = "";
            await loadMessages(true);
        }
    } finally {
        sendBtn.disabled = false;
        chatInput.focus();
    }
});

loadMessages(true);
setInterval(() => loadMessages(false), 4000);
</script>
</body>
</html>
"""


# ==========================================
# HTML PRE SUKROMNE SPRAVY
# ==========================================

DM_INBOX_HTML = """
<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Súkromné správy - Moja komunita</title>
<style>
* { box-sizing: border-box; }
body { background: #101827; color: white; font-family: Arial, sans-serif; margin: 0; padding: 20px; }
.container { max-width: 650px; margin: auto; }
header { text-align: center; padding: 20px 10px; }
h1 { color: #42a5ff; font-size: 32px; margin: 0 0 8px 0; }
.subtitle { color: #9aabc0; margin: 6px 0; }
.nav-tabs { display: flex; gap: 8px; justify-content: center; margin-bottom: 22px; flex-wrap: wrap; }
.nav-tab {
    flex: 1; min-width: 140px; text-align: center; padding: 11px 10px; background: #1d2b40;
    border: 1px solid #344761; border-radius: 10px; color: #b6c5d8;
    text-decoration: none; font-weight: bold; font-size: 14px;
    display: inline-flex; justify-content: center; align-items: center; gap: 6px;
}
.nav-tab.active { background: #2689e8; border-color: #2689e8; color: white; }
.unread-badge {
    background: #ff405e; color: white; border-radius: 999px;
    padding: 1px 7px; font-size: 11px; font-weight: bold;
}
.panel { background: #1d2b40; padding: 20px; border-radius: 15px; border: 1px solid #344761; margin-bottom: 20px; }
.start-form { display: flex; gap: 10px; margin-top: 10px; }
input[type=text] {
    flex: 1; padding: 12px 14px; background: #101827; border: 1px solid #344761;
    border-radius: 10px; color: white; font-size: 15px;
}
.primary-btn {
    padding: 12px 18px; background: #2689e8; border: none; border-radius: 10px;
    color: white; font-weight: bold; font-size: 14px; cursor: pointer; text-decoration: none;
    display: inline-block;
}
.primary-btn:hover { background: #1472cb; }
.error { background: rgba(217, 54, 80, 0.18); border: 1px solid #d93650; color: #ff99aa; padding: 10px 12px; border-radius: 8px; margin-top: 12px; font-size: 14px; }
.conv-card {
    display: flex; justify-content: space-between; align-items: center; gap: 12px;
    background: #1d2b40; padding: 16px 18px; border-radius: 12px;
    border: 1px solid #344761; margin-bottom: 12px; text-decoration: none; color: white;
}
.conv-card:hover { border-color: #42a5ff; }
.conv-name { font-size: 16px; font-weight: bold; color: #42a5ff; }
.conv-handle { color: #8ca0b8; font-size: 12px; font-weight: normal; }
.conv-preview { color: #b6c5d8; font-size: 14px; margin-top: 5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 430px; }
.conv-meta { text-align: right; font-size: 12px; color: #8ca0b8; }
.empty { text-align: center; color: #8ca0b8; padding: 25px; background: #152133; border: 1px solid #344761; border-radius: 12px; }
.auth-cta { display: flex; gap: 12px; justify-content: center; margin-top: 16px; flex-wrap: wrap; }
.secondary-link {
    padding: 12px 18px; background: #293c55; border: 1px solid #3d5678; border-radius: 10px;
    color: #d6e4f5; text-decoration: none; font-weight: bold; font-size: 14px;
}
</style>
</head>
<body>
<div class="container">
<header>
    <h1>MOJA KOMUNITA</h1>
    <p class="subtitle">Súkromné 1-na-1 správy medzi účtami</p>
</header>

<nav class="nav-tabs">
    <a href="/" class="nav-tab">📋 Nástenka</a>
    <a href="/chat" class="nav-tab">💬 Chat</a>
    <a href="/messages" class="nav-tab active">
        ✉️ Súkromné
        {% if unread_dm_count > 0 %}<span class="unread-badge">{{ unread_dm_count }}</span>{% endif %}
    </a>
</nav>

{% if not account %}
    <div class="panel" style="text-align: center;">
        <h2 style="margin-top: 0;">Súkromné správy sú pre registrované účty</h2>
        <p class="subtitle">Prihlás sa alebo si vytvor bezplatný účet, aby si mohol písať súkromné správy ostatným členom komunity.</p>
        <div class="auth-cta">
            <a href="/login" class="primary-btn">Prihlásiť sa</a>
            <a href="/register" class="secondary-link">Zaregistrovať sa</a>
        </div>
    </div>
{% else %}
    <div class="panel">
        <h2 style="margin-top: 0;">✉️ Nová súkromná konverzácia</h2>
        <p class="subtitle">Zadaj používateľské meno (napr. <code>marek_99</code>) alebo prezývku člena komunity.</p>
        <form method="POST" action="/messages/start" class="start-form">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            <input type="text" name="target_user" maxlength="20" placeholder="Používateľské meno alebo prezývka..." required>
            <button type="submit" class="primary-btn">Otvoriť čet</button>
        </form>
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
    </div>

    <h2>Tvoje konverzácie</h2>
    {% if conversations %}
        {% for conv in conversations %}
            <a href="/messages/{{ conv.partner_id }}" class="conv-card">
                <div style="min-width: 0;">
                    <div class="conv-name">
                        {{ conv.partner_nickname }}
                        <span class="conv-handle">@{{ conv.partner_username }}</span>
                    </div>
                    <div class="conv-preview">{{ conv.last_message }}</div>
                </div>
                <div class="conv-meta">
                    <div>{{ conv.last_time }}</div>
                    {% if conv.unread > 0 %}
                        <div style="margin-top: 6px;"><span class="unread-badge">{{ conv.unread }} nové</span></div>
                    {% endif %}
                </div>
            </a>
        {% endfor %}
    {% else %}
        <div class="empty">
            Zatiaľ nemáš žiadne súkromné správy (0 správ). Začni novú konverzáciu cez formulár vyššie alebo klikni na meno autora pri príspevku na nástenke.
        </div>
    {% endif %}
{% endif %}
</div>
</body>
</html>
"""

DM_THREAD_HTML = """
<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Čet s {{ partner.nickname }} - Súkromné správy</title>
<style>
* { box-sizing: border-box; }
body {
    background: #101827; color: white; font-family: Arial, sans-serif;
    margin: 0; padding: 15px; height: 100dvh; display: flex; flex-direction: column;
}
.chat-wrapper { max-width: 650px; width: 100%; margin: 0 auto; display: flex; flex-direction: column; height: 100%; }
.chat-header {
    background: #1d2b40; padding: 14px 18px; border-radius: 14px;
    border: 1px solid #344761; display: flex; justify-content: space-between;
    align-items: center; gap: 10px; margin-bottom: 12px;
}
.back-btn { color: #42a5ff; text-decoration: none; font-weight: bold; font-size: 14px; }
.room-info { text-align: center; }
.room-info h2 { margin: 0; font-size: 18px; color: white; }
.room-info small { color: #8ca0b8; font-size: 12px; }
.messages-box {
    flex: 1; background: #152133; border: 1px solid #344761; border-radius: 14px;
    padding: 15px; overflow-y: auto; display: flex; flex-direction: column;
    gap: 10px; margin-bottom: 12px;
}
.msg {
    max-width: 80%; padding: 10px 14px; border-radius: 14px; background: #1d2b40;
    border: 1px solid #2c3f59; align-self: flex-start; overflow-wrap: anywhere;
}
.msg.mine { align-self: flex-end; background: #1a4b82; border-color: #2689e8; }
.msg-meta { font-size: 11px; color: #8ca0b8; margin-bottom: 4px; }
.msg.mine .msg-meta { color: #b8dcff; text-align: right; }
.msg-text { font-size: 15px; line-height: 1.4; white-space: pre-wrap; }
.chat-form { display: flex; gap: 10px; }
.chat-input {
    flex: 1; padding: 13px 15px; background: #1d2b40; border: 1px solid #344761;
    border-radius: 12px; color: white; font-size: 15px;
}
.send-btn {
    padding: 13px 20px; background: #2689e8; border: none; border-radius: 12px;
    color: white; font-weight: bold; font-size: 15px; cursor: pointer;
}
.empty-chat { text-align: center; color: #71869f; margin: auto; font-size: 14px; }
</style>
</head>
<body>
<div class="chat-wrapper">
<div class="chat-header">
    <a href="/messages" class="back-btn">← Správy</a>
    <div class="room-info">
        <h2>{{ partner.nickname }}</h2>
        <small>@{{ partner.username }}</small>
    </div>
    <div style="width: 65px;"></div>
</div>

<div class="messages-box" id="messages-box">
    <div class="empty-chat">Načítavam konverzáciu...</div>
</div>

<form class="chat-form" id="dm-form">
    <input type="text" id="dm-input" class="chat-input" maxlength="500" placeholder="Napíš súkromnú správu..." autocomplete="off" required>
    <button type="submit" class="send-btn" id="send-btn">Odoslať</button>
</form>
</div>

<script>
const partnerId = {{ partner.id }};
const messagesBox = document.getElementById("messages-box");
const dmForm = document.getElementById("dm-form");
const dmInput = document.getElementById("dm-input");
const sendBtn = document.getElementById("send-btn");
let lastRenderedJson = "";

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

async function loadThread(forceScroll = false) {
    try {
        const response = await fetch(`/messages/${partnerId}/data`, { cache: "no-store" });
        if (!response.ok) return;
        const messages = await response.json();
        const currentJson = JSON.stringify(messages);
        if (currentJson === lastRenderedJson) return;
        lastRenderedJson = currentJson;

        const isNearBottom = messagesBox.scrollHeight - messagesBox.scrollTop - messagesBox.clientHeight < 90;

        if (messages.length === 0) {
            messagesBox.innerHTML = '<div class="empty-chat">Zatiaľ tu nie sú žiadne správy. Napíš prvú správu!</div>';
            return;
        }

        messagesBox.innerHTML = messages.map(m => `
            <div class="msg ${m.is_mine ? 'mine' : ''}">
                <div class="msg-meta">${escapeHtml(m.created_at)}</div>
                <div class="msg-text">${escapeHtml(m.message)}</div>
            </div>
        `).join("");

        if (forceScroll || isNearBottom) {
            messagesBox.scrollTop = messagesBox.scrollHeight;
        }
    } catch (e) {}
}

dmForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = dmInput.value.trim();
    if (!text) return;
    sendBtn.disabled = true;
    try {
        const response = await fetch(`/messages/${partnerId}/send`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message: text })
        });
        if (response.ok) {
            dmInput.value = "";
            await loadThread(true);
        }
    } finally {
        sendBtn.disabled = false;
        dmInput.focus();
    }
});

loadThread(true);
setInterval(() => loadThread(false), 4000);
</script>
</body>
</html>
"""


# ==========================================
# REGISTRACIA, PRIHLASENIE A ODHLASENIE
# ==========================================

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,20}$")


@app.route("/register", methods=["GET", "POST"])
def register():
    error = ""
    username_val = ""

    if request.method == "POST":
        if not valid_auth_csrf():
            error = "Neplatná požiadavka. Obnov stránku."
        else:
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            password2 = request.form.get("password2", "")
            username_val = username

            if not USERNAME_RE.match(username):
                error = "Používateľské meno musí mať 3–20 znakov (písmená, čísla, _, ., -)."
            elif len(password) < 6:
                error = "Heslo musí mať aspoň 6 znakov."
            elif password != password2:
                error = "Heslá sa nezhodujú."
            else:
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT id FROM accounts WHERE LOWER(username) = LOWER(%s)",
                            (username,)
                        )
                        existing = cur.fetchone()
                        if existing:
                            error = "Toto používateľské meno je už obsadené."
                        else:
                            created_at = datetime.now().strftime("%d.%m.%Y %H:%M")
                            cur.execute(
                                "INSERT INTO accounts (username, password_hash, created_at) VALUES (%s, %s, %s) RETURNING id",
                                (username, generate_password_hash(password), created_at)
                            )
                            account_id = cur.fetchone()[0]
                    conn.commit()

                if not error:
                    migrate_visitor_to_account(g.cookie_visitor_id, account_id, username)
                    session["account_id"] = account_id
                    return redirect("/")

    return render_template_string(
        AUTH_HTML,
        mode="register",
        title="Registrácia účtu",
        subtitle="Vytvor si účet a zachovaj si svoju identitu",
        button_text="Vytvoriť účet",
        error=error,
        username_val=username_val,
        csrf_token=get_auth_csrf()
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    username_val = ""

    if request.method == "POST":
        if not valid_auth_csrf():
            error = "Neplatná požiadavka. Obnov stránku."
        else:
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            username_val = username

            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, username, password_hash FROM accounts WHERE LOWER(username) = LOWER(%s)",
                        (username,)
                    )
                    acc = cur.fetchone()

            if not acc or not check_password_hash(acc[2], password):
                error = "Nesprávne používateľské meno alebo heslo."
            else:
                account_id, real_username = acc[0], acc[1]
                migrate_visitor_to_account(g.cookie_visitor_id, account_id, real_username)
                session["account_id"] = account_id
                return redirect("/")

    return render_template_string(
        AUTH_HTML,
        mode="login",
        title="Prihlásenie",
        subtitle="Prihlás sa do svojho komunitného účtu",
        button_text="Prihlásiť sa",
        error=error,
        username_val=username_val,
        csrf_token=get_auth_csrf()
    )


@app.route("/logout", methods=["POST"])
def logout():
    if not valid_auth_csrf():
        return redirect("/")
    session.pop("account_id", None)
    return redirect("/")


# ==========================================
# ZISKANIE PRISPEVKOV VRATANE ANKIET A KOMENTAROV
# ==========================================

def get_posts():
    visitor_id = g.visitor_id

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    p.id,
                    p.message,
                    p.created_at,
                    p.visitor_id,
                    COALESCE(u.nickname, 'Anonymný používateľ') AS nickname,
                    (SELECT COUNT(*) FROM likes l WHERE l.post_id = p.id) AS like_count,
                    EXISTS (
                        SELECT 1 FROM likes l
                        WHERE l.post_id = p.id AND l.visitor_id = %s
                    ) AS liked
                FROM posts p
                LEFT JOIN users u ON p.visitor_id = u.visitor_id
                ORDER BY p.id DESC
            """, (visitor_id,))
            rows = cur.fetchall()

            cur.execute("""
                SELECT
                    c.id,
                    c.post_id,
                    c.message,
                    c.created_at,
                    c.visitor_id,
                    COALESCE(u.nickname, 'Anonymný používateľ') AS nickname
                FROM comments c
                LEFT JOIN users u ON c.visitor_id = u.visitor_id
                ORDER BY c.id ASC
            """)
            comment_rows = cur.fetchall()

            comments_by_post = {}
            for c_row in comment_rows:
                c_post_id = c_row[1]
                c_author_acc_id = parse_account_id_from_visitor(c_row[4])
                if g.account and c_author_acc_id == g.account["id"]:
                    c_author_acc_id = None

                comments_by_post.setdefault(c_post_id, []).append({
                    "id": c_row[0],
                    "message": c_row[2],
                    "created_at": c_row[3],
                    "nickname": c_row[5],
                    "author_account_id": c_author_acc_id
                })

            posts = []
            for row in rows:
                post_id = row[0]
                author_acc_id = parse_account_id_from_visitor(row[3])
                if g.account and author_acc_id == g.account["id"]:
                    author_acc_id = None

                cur.execute("SELECT id FROM polls WHERE post_id = %s", (post_id,))
                poll_row = cur.fetchone()
                poll_data = None

                if poll_row:
                    poll_id = poll_row[0]
                    cur.execute(
                        "SELECT option_id FROM poll_votes WHERE poll_id = %s AND visitor_id = %s",
                        (poll_id, visitor_id)
                    )
                    user_vote_row = cur.fetchone()
                    user_vote = user_vote_row[0] if user_vote_row else None

                    cur.execute("""
                        SELECT
                            po.id,
                            po.option_text,
                            (SELECT COUNT(*) FROM poll_votes pv WHERE pv.option_id = po.id) AS votes
                        FROM poll_options po
                        WHERE po.poll_id = %s
                        ORDER BY po.id ASC
                    """, (poll_id,))
                    option_rows = cur.fetchall()

                    total_votes = sum(opt[2] for opt in option_rows)
                    options = [
                        {
                            "id": opt[0],
                            "text": opt[1],
                            "votes": opt[2],
                            "percent": round((opt[2] / total_votes) * 100) if total_votes > 0 else 0
                        }
                        for opt in option_rows
                    ]

                    poll_data = {
                        "id": poll_id,
                        "options": options,
                        "total_votes": total_votes,
                        "user_vote": user_vote
                    }

                post_comments = comments_by_post.get(post_id, [])

                posts.append({
                    "id": row[0],
                    "message": row[1],
                    "created_at": row[2],
                    "nickname": row[4],
                    "author_account_id": author_acc_id,
                    "like_count": row[5],
                    "liked": bool(row[6]),
                    "poll": poll_data,
                    "comments": post_comments,
                    "comment_count": len(post_comments)
                })

    return posts


# ==========================================
# HLAVNA STRANKA
# ==========================================

@app.route("/", methods=["GET", "POST"])
def home():
    message = ""

    if request.method == "POST":
        post = request.form.get("post", "").strip()
        raw_options = request.form.getlist("poll_options[]")
        poll_options = [opt.strip()[:80] for opt in raw_options if opt.strip()]

        if len(post) > 500:
            message = "Text je príliš dlhý."
        elif len(poll_options) == 1:
            message = "Anketa musí obsahovať aspoň 2 možnosti."
        elif len(poll_options) > 5:
            message = "Anketa môže mať maximálne 5 možností."
        elif not post and not poll_options:
            message = "Napíš text alebo vytvor anketu."

        if not message:
            created_at = datetime.now().strftime("%d.%m.%Y %H:%M")
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO posts (message, created_at, visitor_id)
                        VALUES (%s, %s, %s)
                        RETURNING id
                    """, (post, created_at, g.visitor_id))
                    post_id = cur.fetchone()[0]

                    if poll_options:
                        cur.execute(
                            "INSERT INTO polls (post_id) VALUES (%s) RETURNING id",
                            (post_id,)
                        )
                        poll_id = cur.fetchone()[0]
                        for opt_text in poll_options:
                            cur.execute(
                                "INSERT INTO poll_options (poll_id, option_text) VALUES (%s, %s)",
                                (poll_id, opt_text)
                            )
                conn.commit()

            return redirect("/")

    posts = get_posts()
    posts_html = render_template_string(POSTS_HTML, posts=posts)
    unread_dm_count = get_unread_dm_count(g.account["id"]) if g.account else 0

    return render_template_string(
        HTML,
        posts_html=posts_html,
        message=message,
        nickname=get_nickname(),
        account=g.account,
        unread_dm_count=unread_dm_count,
        csrf_token=get_auth_csrf()
    )


# ==========================================
# CHATOVACIE MIESTNOSTI - ROUTES
# ==========================================

@app.route("/chat")
def chat_rooms():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    r.id,
                    r.name,
                    r.description,
                    (SELECT COUNT(*) FROM chat_messages m WHERE m.room_id = r.id) AS message_count
                FROM chat_rooms r
                ORDER BY r.id ASC
            """)
            rows = cur.fetchall()

    rooms = [
        {"id": r[0], "name": r[1], "description": r[2], "message_count": r[3]}
        for r in rows
    ]
    unread_dm_count = get_unread_dm_count(g.account["id"]) if g.account else 0
    return render_template_string(
        CHAT_ROOMS_HTML,
        rooms=rooms,
        nickname=get_nickname(),
        account=g.account,
        unread_dm_count=unread_dm_count
    )


@app.route("/chat/<int:room_id>")
def chat_room_detail(room_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, description FROM chat_rooms WHERE id = %s", (room_id,))
            row = cur.fetchone()
    if not row:
        return redirect("/chat")
    return render_template_string(
        CHAT_ROOM_DETAIL_HTML,
        room={"id": row[0], "name": row[1], "description": row[2]},
        nickname=get_nickname()
    )


@app.route("/chat/<int:room_id>/messages")
def chat_room_messages(room_id):
    visitor_id = g.visitor_id
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM (
                    SELECT
                        m.id,
                        m.message,
                        m.created_at,
                        m.visitor_id,
                        COALESCE(u.nickname, 'Anonymný používateľ') AS nickname
                    FROM chat_messages m
                    LEFT JOIN users u ON m.visitor_id = u.visitor_id
                    WHERE m.room_id = %s
                    ORDER BY m.id DESC
                    LIMIT 100
                ) sub
                ORDER BY id ASC
            """, (room_id,))
            rows = cur.fetchall()

    return jsonify([
        {
            "id": r[0],
            "message": r[1],
            "created_at": r[2],
            "is_mine": (r[3] == visitor_id),
            "nickname": r[4]
        }
        for r in rows
    ])


@app.route("/chat/<int:room_id>/send", methods=["POST"])
def chat_room_send(room_id):
    data = request.get_json(silent=True) or {}
    text = str(data.get("message", "")).strip()
    if not text or len(text) > 400:
        return jsonify({"error": "Neplatná správa."}), 400

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM chat_rooms WHERE id = %s", (room_id,))
            room = cur.fetchone()
            if not room:
                return jsonify({"error": "Miestnosť neexistuje."}), 404

            created_at = datetime.now().strftime("%H:%M")
            cur.execute("""
                INSERT INTO chat_messages (room_id, visitor_id, message, created_at)
                VALUES (%s, %s, %s, %s)
            """, (room_id, g.visitor_id, text, created_at))
        conn.commit()

    return jsonify({"success": True})


# ==========================================
# SUKROMNE SPRAVY
# ==========================================

def build_conversations_list(my_account_id):
    if not my_account_id:
        return []

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT
                    CASE
                        WHEN sender_account_id = %s THEN recipient_account_id
                        ELSE sender_account_id
                    END AS partner_id
                FROM direct_messages
                WHERE sender_account_id = %s OR recipient_account_id = %s
            """, (my_account_id, my_account_id, my_account_id))
            partner_rows = cur.fetchall()

            conversations = []
            for (partner_id,) in partner_rows:
                if not partner_id or partner_id == my_account_id:
                    continue

                cur.execute(
                    "SELECT id, username FROM accounts WHERE id = %s",
                    (partner_id,)
                )
                acc_row = cur.fetchone()
                if not acc_row:
                    continue

                cur.execute(
                    "SELECT nickname FROM users WHERE visitor_id = %s",
                    (f"acc:{partner_id}",)
                )
                nick_row = cur.fetchone()
                partner_nickname = nick_row[0] if (nick_row and nick_row[0]) else acc_row[1]

                cur.execute("""
                    SELECT message, created_at, id
                    FROM direct_messages
                    WHERE (sender_account_id = %s AND recipient_account_id = %s)
                       OR (sender_account_id = %s AND recipient_account_id = %s)
                    ORDER BY id DESC
                    LIMIT 1
                """, (my_account_id, partner_id, partner_id, my_account_id))
                last_msg = cur.fetchone()

                cur.execute("""
                    SELECT COUNT(*)
                    FROM direct_messages
                    WHERE sender_account_id = %s AND recipient_account_id = %s AND is_read = 0
                """, (partner_id, my_account_id))
                unread = cur.fetchone()[0]

                conversations.append({
                    "partner_id": partner_id,
                    "partner_username": acc_row[1],
                    "partner_nickname": partner_nickname,
                    "last_message": last_msg[0] if last_msg else "",
                    "last_time": last_msg[1] if last_msg else "",
                    "last_id": last_msg[2] if last_msg else 0,
                    "unread": unread
                })

        conversations.sort(key=lambda c: c["last_id"], reverse=True)
        return conversations


@app.route("/messages")
def dm_inbox():
    error = request.args.get("error", "")
    acc_id = g.account["id"] if g.account else None
    conversations = build_conversations_list(acc_id) if acc_id else []
    unread_dm_count = get_unread_dm_count(acc_id) if acc_id else 0

    return render_template_string(
        DM_INBOX_HTML,
        account=g.account,
        conversations=conversations,
        unread_dm_count=unread_dm_count,
        error=error,
        csrf_token=get_auth_csrf()
    )


@app.route("/messages/start", methods=["POST"])
def dm_start():
    if not g.account:
        return redirect("/login")
    if not valid_auth_csrf():
        return redirect("/messages?error=Neplatn%C3%A1+po%C5%BEiadavka.")

    query = request.form.get("target_user", "").strip().lstrip("@")
    if not query:
        return redirect("/messages?error=Zadaj+pou%C5%BE%C3%ADvate%C4%BEsk%C3%A9+meno+alebo+prez%C3%BDvku.")

    acc = None
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM accounts WHERE LOWER(username) = LOWER(%s)",
                (query,)
            )
            acc = cur.fetchone()

            if not acc:
                cur.execute("""
                    SELECT visitor_id
                    FROM users
                    WHERE visitor_id LIKE 'acc:%%' AND LOWER(nickname) = LOWER(%s)
                    LIMIT 1
                """, (query,))
                user_match = cur.fetchone()
                if user_match:
                    acc_id = parse_account_id_from_visitor(user_match[0])
                    if acc_id:
                        acc = (acc_id,)

    if not acc:
        return redirect("/messages?error=Pou%C5%BE%C3%ADvate%C4%BE+s+t%C3%BDmto+menom+alebo+prez%C3%BDvkou+sa+nena%C5%A1iel.")

    if acc[0] == g.account["id"]:
        return redirect("/messages?error=Nem%C3%B4%C5%BEe%C5%A1+posla%C5%A5+s%C3%BAkromn%C3%BA+spr%C3%A1vu+s%C3%A1m+sebe.")

    return redirect(f"/messages/{acc[0]}")


@app.route("/messages/<int:partner_id>")
def dm_thread(partner_id):
    if not g.account:
        return redirect("/login")
    if partner_id == g.account["id"]:
        return redirect("/messages")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username FROM accounts WHERE id = %s",
                (partner_id,)
            )
            acc_row = cur.fetchone()
            if not acc_row:
                return redirect("/messages?error=Pou%C5%BE%C3%ADvate%C4%BE+neexistuje.")

            cur.execute(
                "SELECT nickname FROM users WHERE visitor_id = %s",
                (f"acc:{partner_id}",)
            )
            nick_row = cur.fetchone()
            partner_nickname = nick_row[0] if (nick_row and nick_row[0]) else acc_row[1]

            cur.execute("""
                UPDATE direct_messages
                SET is_read = 1
                WHERE sender_account_id = %s AND recipient_account_id = %s AND is_read = 0
            """, (partner_id, g.account["id"]))
        conn.commit()

    return render_template_string(
        DM_THREAD_HTML,
        partner={"id": acc_row[0], "username": acc_row[1], "nickname": partner_nickname}
    )


@app.route("/messages/<int:partner_id>/data")
def dm_thread_data(partner_id):
    if not g.account:
        return jsonify({"error": "Neprihlásený používateľ."}), 401

    my_id = g.account["id"]
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE direct_messages
                SET is_read = 1
                WHERE sender_account_id = %s AND recipient_account_id = %s AND is_read = 0
            """, (partner_id, my_id))

            cur.execute("""
                SELECT * FROM (
                    SELECT id, sender_account_id, message, created_at
                    FROM direct_messages
                    WHERE (sender_account_id = %s AND recipient_account_id = %s)
                       OR (sender_account_id = %s AND recipient_account_id = %s)
                    ORDER BY id DESC
                    LIMIT 100
                ) sub
                ORDER BY id ASC
            """, (my_id, partner_id, partner_id, my_id))
            rows = cur.fetchall()
        conn.commit()

    return jsonify([
        {
            "id": r[0],
            "is_mine": (r[1] == my_id),
            "message": r[2],
            "created_at": r[3]
        }
        for r in rows
    ])


@app.route("/messages/<int:partner_id>/send", methods=["POST"])
def dm_thread_send(partner_id):
    if not g.account:
        return jsonify({"error": "Neprihlásený používateľ."}), 401
    if partner_id == g.account["id"]:
        return jsonify({"error": "Nemôžeš písať sám sebe."}), 400

    data = request.get_json(silent=True) or {}
    text = str(data.get("message", "")).strip()
    if not text or len(text) > 500:
        return jsonify({"error": "Neplatná správa."}), 400

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM accounts WHERE id = %s", (partner_id,))
            partner = cur.fetchone()
            if not partner:
                return jsonify({"error": "Používateľ neexistuje."}), 404

            created_at = datetime.now().strftime("%d.%m. %H:%M")
            cur.execute("""
                INSERT INTO direct_messages (sender_account_id, recipient_account_id, message, created_at, is_read)
                VALUES (%s, %s, %s, %s, 0)
            """, (g.account["id"], partner_id, text, created_at))
        conn.commit()

    return jsonify({"success": True})


# ==========================================
# HLASOVANIE, PREZYVKA, PRISPEVKY, LAJKY, KOMENTARE
# ==========================================

@app.route("/poll/<int:poll_id>/vote/<int:option_id>", methods=["POST"])
def vote_poll(poll_id, option_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM poll_options WHERE id = %s AND poll_id = %s",
                (option_id, poll_id)
            )
            option = cur.fetchone()
            if not option:
                return jsonify({"error": "Neplatná možnosť ankety."}), 404

            cur.execute("""
                INSERT INTO poll_votes (poll_id, option_id, visitor_id)
                VALUES (%s, %s, %s)
                ON CONFLICT(poll_id, visitor_id)
                DO UPDATE SET option_id = EXCLUDED.option_id
            """, (poll_id, option_id, g.visitor_id))
        conn.commit()

    return jsonify({"success": True})


@app.route("/nickname", methods=["POST"])
def change_nickname():
    data = request.get_json(silent=True) or {}
    nickname = str(data.get("nickname", "")).strip()
    if not nickname or len(nickname) > 20:
        return jsonify({"error": "Prezývka musí mať 1 až 20 znakov."}), 400

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET nickname = %s WHERE visitor_id = %s",
                (nickname, g.visitor_id)
            )
        conn.commit()

    return jsonify({"success": True, "nickname": nickname})


@app.route("/posts")
def posts_update():
    return render_template_string(POSTS_HTML, posts=get_posts())


@app.route("/post/<int:post_id>/comment", methods=["POST"])
def add_comment(post_id):
    data = request.get_json(silent=True) or {}
    text = str(data.get("message", "")).strip()
    if not text or len(text) > 300:
        return jsonify({"error": "Komentár musí mať 1 až 300 znakov."}), 400

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM posts WHERE id = %s", (post_id,))
            if not cur.fetchone():
                return jsonify({"error": "Príspevok neexistuje."}), 404

            created_at = datetime.now().strftime("%d.%m.%Y %H:%M")
            cur.execute("""
                INSERT INTO comments (post_id, visitor_id, message, created_at)
                VALUES (%s, %s, %s, %s)
            """, (post_id, g.visitor_id, text, created_at))
        conn.commit()

    return jsonify({"success": True})


@app.route("/like/<int:post_id>", methods=["POST"])
def toggle_like(post_id):
    visitor_id = g.visitor_id
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM posts WHERE id = %s", (post_id,))
            post = cur.fetchone()
            if not post:
                return jsonify({"error": "Príspevok neexistuje."}), 404

            cur.execute(
                "SELECT id FROM likes WHERE post_id = %s AND visitor_id = %s",
                (post_id, visitor_id)
            )
            existing_like = cur.fetchone()

            if existing_like:
                cur.execute("DELETE FROM likes WHERE id = %s", (existing_like[0],))
                liked = False
            else:
                cur.execute(
                    "INSERT INTO likes (post_id, visitor_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (post_id, visitor_id)
                )
                liked = True

            cur.execute("SELECT COUNT(*) FROM likes WHERE post_id = %s", (post_id,))
            count = cur.fetchone()[0]
        conn.commit()

    return jsonify({"liked": liked, "count": count})


# ==========================================
# ADMINISTRATORSKA STRANKA
# ==========================================

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Administrácia</title>
<style>
* { box-sizing: border-box; }
body { background: #101827; color: white; font-family: Arial, sans-serif; margin: 0; padding: 20px; }
.container { max-width: 750px; margin: auto; }
.panel, .post { background: #1d2b40; padding: 20px; border-radius: 12px; margin-bottom: 15px; }
h1 { color: #42a5ff; }
input {
    width: 100%; padding: 13px; margin: 8px 0; background: #101827;
    color: white; border: 1px solid #344761; border-radius: 8px;
}
button { cursor: pointer; border: none; border-radius: 8px; padding: 12px; color: white; }
.login-button, .create-button { width: 100%; background: #2689e8; }
.delete-button { background: #d93650; margin-top: 8px; }
.small-delete-btn { background: #d93650; padding: 4px 9px; font-size: 12px; border-radius: 6px; }
.logout-button { background: #475b73; }
.error { color: #ff7777; }
.info { color: #9aabc0; }
.back-link { display: inline-block; margin-bottom: 15px; color: #42a5ff; text-decoration: none; }
.admin-poll, .admin-comments { background: #152133; padding: 10px 14px; border-radius: 8px; margin-top: 10px; font-size: 14px; }
.admin-comment-row {
    display: flex; justify-content: space-between; align-items: center; gap: 10px;
    padding: 8px 0; border-bottom: 1px solid #28384f;
}
.admin-comment-row:last-child { border-bottom: none; }
.room-admin-row {
    display: flex; justify-content: space-between; align-items: center;
    background: #152133; padding: 12px 15px; border-radius: 8px; margin-top: 10px; gap: 10px;
}
</style>
</head>
<body>
<div class="container">
<a class="back-link" href="/">← Späť na nástenku</a>
<h1>🔐 Administrácia</h1>

{% if not logged_in %}
    <div class="panel">
        <h2>Prihlásenie administrátora</h2>
        {% if error %}<p class="error">{{ error }}</p>{% endif %}
        <form method="POST" action="/admin">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            <input type="text" name="username" placeholder="Používateľské meno" required>
            <input type="password" name="password" placeholder="Heslo" required>
            <button class="login-button" type="submit">Prihlásiť sa</button>
        </form>
    </div>
{% else %}
    <div class="panel">
        <h2>Vitaj v administrácii!</h2>
        <p class="info">Tu môžeš spravovať chatovacie miestnosti aj príspevky komunity.</p>
        <form method="POST" action="/admin/logout">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            <button class="logout-button" type="submit">Odhlásiť sa</button>
        </form>
    </div>

    <div class="panel">
        <h2>💬 Chatovacie miestnosti</h2>
        <form method="POST" action="/admin/rooms/create">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            <input type="text" name="name" maxlength="40" placeholder="Názov novej miestnosti (napr. 🎵 Hudba)" required>
            <input type="text" name="description" maxlength="100" placeholder="Krátky popis miestnosti">
            <button class="create-button" type="submit">+ Vytvoriť miestnosť</button>
        </form>

        {% for room in rooms %}
            <div class="room-admin-row">
                <div>
                    <strong>{{ room[1] }}</strong><br>
                    <small class="info">{{ room[2] }}</small>
                </div>
                <form method="POST" action="/admin/rooms/delete/{{ room[0] }}" onsubmit="return confirm('Vymazať miestnosť aj so všetkými správami v nej?');">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                    <button class="delete-button" type="submit">🗑 Vymazať</button>
                </form>
            </div>
        {% endfor %}
    </div>

    <h2>Príspevky komunity</h2>
    {% for post in posts %}
        <div class="post">
            {% if post.message %}<p>{{ post.message }}</p>{% endif %}
            {% if post.poll %}
                <div class="admin-poll">
                    <strong>📊 Anketa ({{ post.poll.total_votes }} hlasov):</strong>
                    <ul>
                        {% for opt in post.poll.options %}
                            <li>{{ opt.text }} — {{ opt.percent }}% ({{ opt.votes }})</li>
                        {% endfor %}
                    </ul>
                </div>
            {% endif %}
            <p class="info">
                Autor: {{ post.nickname }}<br>
                Dátum: {{ post.created_at }}<br>
                Lajky: {{ post.like_count }} · Komentáre: {{ post.comment_count }}
            </p>
            {% if post.comments %}
                <div class="admin-comments">
                    <strong>💬 Komentáre:</strong>
                    {% for c in post.comments %}
                        <div class="admin-comment-row">
                            <div>
                                <small class="info">{{ c.nickname }} ({{ c.created_at }}):</small><br>
                                <span>{{ c.message }}</span>
                            </div>
                            <form method="POST" action="/admin/comments/delete/{{ c.id }}" onsubmit="return confirm('Vymazať tento komentár?');">
                                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                                <button class="small-delete-btn" type="submit">🗑</button>
                            </form>
                        </div>
                    {% endfor %}
                </div>
            {% endif %}
            <form method="POST" action="/admin/delete/{{ post.id }}" onsubmit="return confirm('Naozaj chceš vymazať tento príspevok?');">
                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                <button class="delete-button" type="submit">🗑 Vymazať príspevok</button>
            </form>
        </div>
    {% else %}
        <p class="info">Zatiaľ tu nie sú žiadne príspevky.</p>
    {% endfor %}
{% endif %}
</div>
</body>
</html>
"""


def admin_csrf_token():
    if "admin_csrf" not in session:
        session["admin_csrf"] = secrets.token_urlsafe(32)
    return session["admin_csrf"]


def valid_admin_csrf():
    return secrets.compare_digest(
        request.form.get("csrf_token", ""),
        session.get("admin_csrf", "")
    )


@app.route("/admin", methods=["GET", "POST"])
def admin():
    error = ""
    if request.method == "POST":
        if not valid_admin_csrf():
            error = "Neplatná požiadavka. Obnov stránku."
        else:
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
                session.clear()
                session["is_admin"] = True
                return redirect("/admin")
            error = "Nesprávne meno alebo heslo."

    logged_in = session.get("is_admin", False)
    posts = get_posts() if logged_in else []
    rooms = []
    if logged_in:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, description FROM chat_rooms ORDER BY id ASC")
                rooms = cur.fetchall()

    return render_template_string(
        ADMIN_HTML,
        logged_in=logged_in,
        error=error,
        posts=posts,
        rooms=rooms,
        csrf_token=admin_csrf_token()
    )


@app.route("/admin/rooms/create", methods=["POST"])
def admin_create_room():
    if not session.get("is_admin"):
        return redirect("/admin")
    if not valid_admin_csrf():
        return "Neplatná požiadavka.", 400
    name = request.form.get("name", "").strip()[:40]
    description = request.form.get("description", "").strip()[:100]
    if name:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO chat_rooms (name, description) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING",
                    (name, description)
                )
            conn.commit()
    return redirect("/admin")


@app.route("/admin/rooms/delete/<int:room_id>", methods=["POST"])
def admin_delete_room(room_id):
    if not session.get("is_admin"):
        return redirect("/admin")
    if not valid_admin_csrf():
        return "Neplatná požiadavka.", 400
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chat_messages WHERE room_id = %s", (room_id,))
            cur.execute("DELETE FROM chat_rooms WHERE id = %s", (room_id,))
        conn.commit()
    return redirect("/admin")


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    if not session.get("is_admin"):
        return redirect("/admin")
    if not valid_admin_csrf():
        return "Neplatná požiadavka.", 400
    session.clear()
    return redirect("/admin")


@app.route("/admin/comments/delete/<int:comment_id>", methods=["POST"])
def admin_delete_comment(comment_id):
    if not session.get("is_admin"):
        return redirect("/admin")
    if not valid_admin_csrf():
        return "Neplatná požiadavka.", 400

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM comments WHERE id = %s", (comment_id,))
        conn.commit()

    return redirect("/admin")


@app.route("/admin/delete/<int:post_id>", methods=["POST"])
def admin_delete_post(post_id):
    if not session.get("is_admin"):
        return redirect("/admin")
    if not valid_admin_csrf():
        return "Neplatná požiadavka.", 400

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM polls WHERE post_id = %s", (post_id,))
            poll = cur.fetchone()
            if poll:
                poll_id = poll[0]
                cur.execute("DELETE FROM poll_votes WHERE poll_id = %s", (poll_id,))
                cur.execute("DELETE FROM poll_options WHERE poll_id = %s", (poll_id,))
                cur.execute("DELETE FROM polls WHERE id = %s", (poll_id,))
            cur.execute("DELETE FROM comments WHERE post_id = %s", (post_id,))
            cur.execute("DELETE FROM likes WHERE post_id = %s", (post_id,))
            cur.execute("DELETE FROM posts WHERE id = %s", (post_id,))
        conn.commit()

    return redirect("/admin")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
