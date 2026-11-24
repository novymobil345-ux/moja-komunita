from flask import (
    Flask, request, redirect, render_template_string,
    jsonify, g, session, url_for
)
from werkzeug.security import check_password_hash, generate_password_hash
import sqlite3
import os
import re
import uuid
import secrets
from datetime import datetime
from PIL import Image, ImageOps, UnidentifiedImageError

app = Flask(__name__)

from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# ==========================================
# ADMINISTRATOR & KONFIGURACIA
# ==========================================

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    "b7f74e7e963982aa94bd28f1f38842b38ab69f2748736abbfa74abf75c8290c5"
)

app.config.update(
    MAX_CONTENT_LENGTH=15 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.environ.get(
    "ADMIN_PASSWORD_HASH",
    "scrypt:32768:8:1$IQmPlVdN4QAOMulj$4c9c02f9b3b8f02afc8f149091d0e050e376061af8f84ee901dc0753ea07a38d315f19ed0b4ad4277b6f3d9efba71d7b6fe727ec090a48fe583c60595f87e791"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "komunita.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


# ==========================================
# DATABAZA
# ==========================================

def init_db():
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        columns = [
            row[1] for row in
            conn.execute("PRAGMA table_info(posts)").fetchall()
        ]

        if "image" not in columns:
            conn.execute("ALTER TABLE posts ADD COLUMN image TEXT")

        if "visitor_id" not in columns:
            conn.execute("ALTER TABLE posts ADD COLUMN visitor_id TEXT")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS likes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                visitor_id TEXT NOT NULL,
                UNIQUE(post_id, visitor_id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                visitor_id TEXT PRIMARY KEY,
                nickname TEXT NOT NULL
            )
        """)

        # TABULKY PRE ANKETY
        conn.execute("""
            CREATE TABLE IF NOT EXISTS polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL UNIQUE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS poll_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                poll_id INTEGER NOT NULL,
                option_text TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS poll_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                poll_id INTEGER NOT NULL,
                option_id INTEGER NOT NULL,
                visitor_id TEXT NOT NULL,
                UNIQUE(poll_id, visitor_id)
            )
        """)

        # TABULKY PRE CHATOVACIE MIESTNOSTI
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT ''
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL,
                visitor_id TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        room_count = conn.execute("SELECT COUNT(*) FROM chat_rooms").fetchone()[0]
        if room_count == 0:
            default_rooms = [
                ("💬 Všeobecný pokec", "Voľná debata o všetkom možnom"),
                ("🎮 Hry a zábava", "Pokec o hrách, filmoch a seriáloch"),
                ("🌙 Nočná zmena", "Pre tých, ktorí ešte nespia")
            ]
            conn.executemany(
                "INSERT INTO chat_rooms (name, description) VALUES (?, ?)",
                default_rooms
            )


init_db()


# ==========================================
# IDENTIFIKACIA POUZIVATELA A UCET
# ==========================================

def get_current_account():
    account_id = session.get("account_id")
    if not account_id:
        return None

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, username FROM accounts WHERE id = ?",
            (account_id,)
        ).fetchone()

    if not row:
        session.pop("account_id", None)
        return None

    return {"id": row[0], "username": row[1]}


def migrate_visitor_to_account(old_visitor_id, account_id, username):
    """Presunie anonymnú aktivitu na účet a zjednotí identitu."""
    account_key = f"acc:{account_id}"

    with get_db() as conn:
        old_user = conn.execute(
            "SELECT nickname FROM users WHERE visitor_id = ?",
            (old_visitor_id,)
        ).fetchone()

        preferred_nickname = username
        if old_user and old_user[0] and old_user[0] != "Anonymný používateľ":
            preferred_nickname = old_user[0][:20]

        conn.execute("""
            INSERT INTO users (visitor_id, nickname)
            VALUES (?, ?)
            ON CONFLICT(visitor_id) DO UPDATE SET nickname = excluded.nickname
        """, (account_key, preferred_nickname))

        if old_visitor_id and old_visitor_id != account_key:
            conn.execute(
                "UPDATE posts SET visitor_id = ? WHERE visitor_id = ?",
                (account_key, old_visitor_id)
            )
            conn.execute(
                "UPDATE chat_messages SET visitor_id = ? WHERE visitor_id = ?",
                (account_key, old_visitor_id)
            )

            # Lajky – ak už má účet lajk na danom príspevku, starý vymažeme
            old_likes = conn.execute(
                "SELECT id, post_id FROM likes WHERE visitor_id = ?",
                (old_visitor_id,)
            ).fetchall()
            for like_id, post_id in old_likes:
                exists = conn.execute(
                    "SELECT 1 FROM likes WHERE post_id = ? AND visitor_id = ?",
                    (post_id, account_key)
                ).fetchone()
                if exists:
                    conn.execute("DELETE FROM likes WHERE id = ?", (like_id,))
                else:
                    conn.execute("UPDATE likes SET visitor_id = ? WHERE id = ?", (account_key, like_id))

            # Hlasy v anketách – ak už účet hlasoval, starý hlas vymažeme
            old_votes = conn.execute(
                "SELECT id, poll_id FROM poll_votes WHERE visitor_id = ?",
                (old_visitor_id,)
            ).fetchall()
            for vote_id, poll_id in old_votes:
                exists = conn.execute(
                    "SELECT 1 FROM poll_votes WHERE poll_id = ? AND visitor_id = ?",
                    (poll_id, account_key)
                ).fetchone()
                if exists:
                    conn.execute("DELETE FROM poll_votes WHERE id = ?", (vote_id,))
                else:
                    conn.execute("UPDATE poll_votes SET visitor_id = ? WHERE id = ?", (account_key, vote_id))

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
        conn.execute("""
            INSERT OR IGNORE INTO users
            (visitor_id, nickname)
            VALUES (?, ?)
        """, (g.visitor_id, default_nick))


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
        result = conn.execute("""
            SELECT nickname
            FROM users
            WHERE visitor_id = ?
        """, (g.visitor_id,)).fetchone()

    if result:
        return result[0]

    return g.account["username"][:20] if g.account else "Anonymný používateľ"


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
* {
    box-sizing: border-box;
}

body {
    background: #101827;
    color: white;
    font-family: Arial, sans-serif;
    margin: 0;
    padding: 20px;
}

.container {
    max-width: 480px;
    margin: auto;
}

header {
    text-align: center;
    padding: 20px 10px;
}

h1 {
    color: #42a5ff;
    font-size: 30px;
    margin: 0 0 8px 0;
}

.subtitle {
    color: #9aabc0;
    margin: 6px 0;
    font-size: 14px;
}

.panel {
    background: #1d2b40;
    padding: 24px;
    border-radius: 15px;
    border: 1px solid #344761;
}

input {
    width: 100%;
    margin-top: 10px;
    padding: 12px;
    background: #101827;
    border: 1px solid #344761;
    border-radius: 10px;
    color: white;
    font-size: 15px;
}

button {
    width: 100%;
    margin-top: 16px;
    padding: 14px;
    background: #2689e8;
    border: none;
    border-radius: 9px;
    color: white;
    font-size: 16px;
    font-weight: bold;
    cursor: pointer;
}

button:hover {
    background: #1472cb;
}

.error {
    background: rgba(217, 54, 80, 0.18);
    border: 1px solid #d93650;
    color: #ff99aa;
    padding: 10px 12px;
    border-radius: 8px;
    margin-bottom: 12px;
    font-size: 14px;
}

.switch-link {
    text-align: center;
    margin-top: 18px;
    font-size: 14px;
    color: #9aabc0;
}

.switch-link a,
.back-link {
    color: #42a5ff;
    text-decoration: none;
}

.back-link {
    display: inline-block;
    margin-bottom: 15px;
    font-size: 14px;
}
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
        {% if error %}
            <div class="error">{{ error }}</div>
        {% endif %}

        <form method="POST">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">

            <label class="subtitle" for="username">Používateľské meno (3–20 znakov)</label>
            <input
                id="username"
                type="text"
                name="username"
                maxlength="20"
                value="{{ username_val }}"
                placeholder="napr. marek_99"
                required
            >

            <label class="subtitle" for="password" style="display:block; margin-top:12px;">Heslo (aspoň 6 znakov)</label>
            <input
                id="password"
                type="password"
                name="password"
                placeholder="Zadaj heslo"
                required
            >

            {% if mode == "register" %}
                <label class="subtitle" for="password2" style="display:block; margin-top:12px;">Potvrď heslo</label>
                <input
                    id="password2"
                    type="password"
                    name="password2"
                    placeholder="Zopakuj heslo"
                    required
                >
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

            {% if post.image %}
                <img
                    class="post-image"
                    src="{{ url_for('static', filename='uploads/' + post.image) }}"
                    alt="Fotografia v anonymnom príspevku"
                    loading="lazy"
                >
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
                            <div
                                class="poll-bar"
                                style="width: {{ opt.percent if post.poll.user_vote else 0 }}%;"
                            ></div>
                            <span class="poll-text">
                                {% if is_selected %}✓ {% endif %}{{ opt.text }}
                            </span>
                            {% if post.poll.user_vote %}
                                <span class="poll-stats">
                                    {{ opt.percent }}% ({{ opt.votes }})
                                </span>
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
                <strong class="post-author">{{ post.nickname }}</strong>
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
            </div>
        </div>
    {% endfor %}
{% else %}
    <div class="empty">
        Zatiaľ tu nikto nič nepridal. Buď prvý!
    </div>
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
* {
    box-sizing: border-box;
}

body {
    background: #101827;
    color: white;
    font-family: Arial, sans-serif;
    margin: 0;
    padding: 20px;
}

.container {
    max-width: 650px;
    margin: auto;
}

header {
    text-align: center;
    padding: 20px 10px;
}

h1 {
    color: #42a5ff;
    font-size: 32px;
    margin: 0 0 8px 0;
}

.subtitle {
    color: #9aabc0;
    margin: 6px 0;
}

/* UCET BAR */
.account-bar {
    background: #152133;
    border: 1px solid #344761;
    border-radius: 12px;
    padding: 12px 16px;
    margin-bottom: 14px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 10px;
    font-size: 14px;
}

.account-actions {
    display: flex;
    gap: 10px;
    align-items: center;
}

.account-link {
    color: #42a5ff;
    text-decoration: none;
    font-weight: bold;
    font-size: 13px;
}

.account-link:hover {
    text-decoration: underline;
}

.logout-inline-btn {
    background: #293c55;
    color: #d6e4f5;
    border: 1px solid #3d5678;
    border-radius: 7px;
    padding: 6px 11px;
    font-size: 12px;
    cursor: pointer;
}

.logout-inline-btn:hover {
    background: #334b6b;
}

/* NAVIGACIA */
.nav-tabs {
    display: flex;
    gap: 10px;
    justify-content: center;
    margin-bottom: 22px;
}

.nav-tab {
    flex: 1;
    text-align: center;
    padding: 12px;
    background: #1d2b40;
    border: 1px solid #344761;
    border-radius: 10px;
    color: #b6c5d8;
    text-decoration: none;
    font-weight: bold;
    transition: 0.15s;
}

.nav-tab:hover {
    border-color: #42a5ff;
    color: white;
}

.nav-tab.active {
    background: #2689e8;
    border-color: #2689e8;
    color: white;
}

.panel {
    background: #1d2b40;
    padding: 20px;
    border-radius: 15px;
    margin-bottom: 25px;
}

textarea {
    width: 100%;
    height: 100px;
    padding: 13px;
    border: 1px solid #344761;
    border-radius: 10px;
    background: #101827;
    color: white;
    resize: vertical;
    font-size: 16px;
}

input[type=file],
input[type=text] {
    width: 100%;
    margin-top: 10px;
    padding: 12px;
    background: #101827;
    border: 1px solid #344761;
    border-radius: 10px;
    color: white;
    font-size: 15px;
}

button {
    cursor: pointer;
}

.submit-button,
.nickname-button {
    width: 100%;
    margin-top: 14px;
    padding: 14px;
    background: #2689e8;
    border: none;
    border-radius: 9px;
    color: white;
    font-size: 16px;
}

.submit-button:hover,
.nickname-button:hover {
    background: #1472cb;
}

/* TVORBA ANKETY */

.poll-toggle-btn {
    margin-top: 12px;
    padding: 9px 14px;
    background: #293c55;
    border: 1px solid #3d5678;
    border-radius: 8px;
    color: #d6e4f5;
    font-size: 14px;
}

.poll-toggle-btn:hover {
    background: #334b6b;
}

.poll-creator {
    display: none;
    margin-top: 12px;
    padding: 14px;
    background: #152133;
    border: 1px solid #344761;
    border-radius: 10px;
}

.poll-creator.open {
    display: block;
}

.poll-option-row {
    display: flex;
    gap: 8px;
    align-items: center;
}

.poll-option-row input {
    margin-top: 8px;
}

.remove-option-btn {
    margin-top: 8px;
    padding: 11px 13px;
    background: #3a2330;
    border: 1px solid #63344b;
    border-radius: 8px;
    color: #ff8fa3;
    font-size: 14px;
}

.add-option-btn {
    margin-top: 10px;
    padding: 8px 12px;
    background: transparent;
    border: 1px dashed #42a5ff;
    border-radius: 8px;
    color: #42a5ff;
    font-size: 13px;
}

/* ZOBRAZENIE ANKETY V PRISPEVKU */

.poll-box {
    margin-top: 14px;
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.poll-option {
    position: relative;
    width: 100%;
    text-align: left;
    padding: 11px 14px;
    background: #101827;
    border: 1px solid #344761;
    border-radius: 9px;
    color: white;
    font-size: 14px;
    overflow: hidden;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    transition: border-color 0.15s;
}

.poll-option:hover {
    border-color: #42a5ff;
}

.poll-option.selected {
    border-color: #42a5ff;
    font-weight: bold;
}

.poll-bar {
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    background: rgba(38, 137, 232, 0.25);
    z-index: 0;
    transition: width 0.3s ease;
}

.poll-option.selected .poll-bar {
    background: rgba(66, 165, 255, 0.38);
}

.poll-text,
.poll-stats {
    position: relative;
    z-index: 1;
}

.poll-stats {
    color: #b6c5d8;
    font-size: 13px;
    white-space: nowrap;
}

.poll-total {
    font-size: 12px;
    color: #8ca0b8;
    margin-top: 2px;
}

/* PRISPEVKY */

.post {
    background: #1d2b40;
    padding: 18px;
    border-radius: 12px;
    margin-bottom: 13px;
    overflow-wrap: anywhere;
}

.post-message {
    white-space: pre-wrap;
    font-size: 16px;
    line-height: 1.5;
}

.post-image {
    width: 100%;
    max-height: 450px;
    object-fit: contain;
    border-radius: 10px;
    margin-top: 12px;
    background: #101827;
}

#preview-image {
    display: none;
    width: 100%;
    max-height: 280px;
    object-fit: contain;
    border-radius: 10px;
    margin-top: 12px;
    background: #101827;
}

.post-info {
    color: #8ca0b8;
    font-size: 12px;
    margin-top: 12px;
}

.post-author {
    color: #42a5ff;
}

.error {
    color: #ff7777;
    margin-bottom: 12px;
}

.empty {
    text-align: center;
    color: #8ca0b8;
    padding: 25px;
}

.counter {
    text-align: right;
    color: #8ca0b8;
    font-size: 12px;
    margin-top: 5px;
}

.refresh-info {
    color: #71869f;
    font-size: 12px;
    text-align: center;
    margin-bottom: 15px;
}

.nickname-status {
    color: #8ca0b8;
    font-size: 13px;
    margin-top: 10px;
}

.like-area {
    margin-top: 14px;
    border-top: 1px solid #344761;
    padding-top: 12px;
}

.like-button {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: transparent;
    border: none;
    color: #b6c5d8;
    font-size: 14px;
    padding: 5px 8px;
    border-radius: 8px;
}

.like-button:hover {
    background: #293c55;
}

.like-button:disabled {
    opacity: 0.6;
    cursor: wait;
}

.heart {
    font-size: 24px;
    color: #aab8ca;
    transition: transform 0.15s;
}

.like-button.liked {
    color: #ff6b81;
}

.like-button.liked .heart {
    color: #ff405e;
}

.like-button:active .heart {
    transform: scale(1.3);
}

.like-count {
    font-weight: bold;
    color: white;
}

footer {
    text-align: center;
    margin-top: 30px;
    padding-bottom: 20px;
    font-size: 13px;
}

footer a {
    color: #71869f;
    text-decoration: none;
}

footer a:hover {
    color: #42a5ff;
}
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
    <a href="/chat" class="nav-tab">💬 Chatovacie miestnosti</a>
</nav>

<div class="panel">
    <h2>👤 Tvoja prezývka</h2>
    <p class="subtitle">
        Ostatní návštevníci uvidia túto prezývku pri tvojich príspevkoch aj v chate.
    </p>

    <form id="nickname-form">
        <input
            type="text"
            id="nickname-input"
            maxlength="20"
            value="{{ nickname }}"
            placeholder="Zadaj svoju prezývku"
            required
        >
        <button class="nickname-button" type="submit">
            Uložiť prezývku
        </button>
    </form>

    <div id="nickname-status" class="nickname-status"></div>
</div>

<div class="panel">
    <h2>Vytvor príspevok</h2>
    <p class="subtitle">
        Napíš text, pridaj fotografiu alebo vytvor anketu.
    </p>

    {% if message %}
        <div class="error">{{ message }}</div>
    {% endif %}

    <form method="POST" action="/" enctype="multipart/form-data">
        <textarea
            id="post-textarea"
            name="post"
            maxlength="500"
            placeholder="Čo máš na srdci? (alebo otázka k ankete)"
        ></textarea>

        <div class="counter" id="char-counter">
            0 / 500 znakov
        </div>

        <!-- ANKETA -->
        <button type="button" class="poll-toggle-btn" id="toggle-poll-btn">
            📊 Pridať anketu
        </button>

        <div class="poll-creator" id="poll-creator">
            <p class="subtitle" style="margin-top: 0;">
                Možnosti hlasovania (2 až 5 možností):
            </p>
            <div id="poll-options-container">
                <div class="poll-option-row">
                    <input type="text" name="poll_options[]" maxlength="80" placeholder="Možnosť 1">
                </div>
                <div class="poll-option-row">
                    <input type="text" name="poll_options[]" maxlength="80" placeholder="Možnosť 2">
                </div>
            </div>
            <button type="button" class="add-option-btn" id="add-option-btn">
                + Pridať ďalšiu možnosť
            </button>
        </div>

        <h3>📸 Pridať fotografiu</h3>

        <input
            id="photo-input"
            type="file"
            name="photo"
            accept="image/jpeg,image/png,image/webp,image/gif"
        >

        <img id="preview-image" alt="Náhľad vybraného obrázka">

        <p class="subtitle">
            JPG, PNG, WEBP alebo animovaný GIF. Maximálne 15 MB.
        </p>

        <button class="submit-button" type="submit">
            Pridať príspevok
        </button>
    </form>
</div>

<h2>Najnovšie príspevky</h2>

<div class="refresh-info">
    Príspevky sa automaticky aktualizujú každých 5 sekúnd.
</div>

<div id="posts-container">
    {{ posts_html|safe }}
</div>

<footer>
    <a href="/admin">Administrácia</a>
</footer>

</div>

<script>
// POCITADLO ZNAKOV
const textarea = document.getElementById("post-textarea");
const charCounter = document.getElementById("char-counter");

function updateCounter() {
    const len = textarea.value.length;
    charCounter.textContent = `${len} / 500 znakov`;
}

textarea.addEventListener("input", updateCounter);
updateCounter();

// TVORBA ANKETY
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
        updateAddButtonVisibility();
    };

    row.appendChild(input);
    row.appendChild(removeBtn);
    pollOptionsContainer.appendChild(row);
    updateAddButtonVisibility();
});

function updateAddButtonVisibility() {
    const count = pollOptionsContainer.querySelectorAll(".poll-option-row").length;
    addOptionBtn.style.display = count >= 5 ? "none" : "inline-block";
}

// NAHLAD OBRAZKA
const photoInput = document.getElementById("photo-input");
const previewImage = document.getElementById("preview-image");

photoInput.addEventListener("change", function() {
    const file = this.files[0];
    if (!file) {
        previewImage.style.display = "none";
        previewImage.removeAttribute("src");
        return;
    }
    const reader = new FileReader();
    reader.onload = function(e) {
        previewImage.src = e.target.result;
        previewImage.style.display = "block";
    };
    reader.readAsDataURL(file);
});

// AUTOMATICKA AKTUALIZACIA PRISPEVKOV
async function refreshPosts() {
    try {
        const response = await fetch("/posts", { cache: "no-store" });
        if (!response.ok) return;
        const html = await response.text();
        document.getElementById("posts-container").innerHTML = html;
    } catch (error) {
        console.log("Chyba pri obnovovaní príspevkov.");
    }
}

setInterval(refreshPosts, 5000);

// HLASOVANIE V ANKETE
async function votePoll(pollId, optionId, button) {
    const box = button.closest(".poll-box");
    const buttons = box.querySelectorAll("button");
    buttons.forEach(b => b.disabled = true);

    try {
        const response = await fetch(`/poll/${pollId}/vote/${optionId}`, {
            method: "POST"
        });
        if (response.ok) {
            await refreshPosts();
        }
    } catch (error) {
        console.log("Chyba pri hlasovaní.");
    } finally {
        buttons.forEach(b => b.disabled = false);
    }
}

// LAJKOVANIE
async function toggleLike(button) {
    if (button.disabled) return;

    const postId = button.dataset.postId;
    button.disabled = true;

    try {
        const response = await fetch("/like/" + postId, {
            method: "POST"
        });

        if (!response.ok) {
            throw new Error("Nepodarilo sa uložiť lajk.");
        }

        const data = await response.json();

        button.classList.toggle("liked", data.liked);
        button.setAttribute("aria-pressed", data.liked ? "true" : "false");
        button.querySelector(".heart").textContent = data.liked ? "♥" : "♡";
        button.querySelector(".like-count").textContent = data.count;
    } catch (error) {
        console.log(error);
    } finally {
        button.disabled = false;
    }
}

// ZMENA PREZYVKY
document.getElementById("nickname-form").addEventListener(
    "submit",
    async function(event) {
        event.preventDefault();

        const input = document.getElementById("nickname-input");
        const status = document.getElementById("nickname-status");
        const submitBtn = this.querySelector("button");
        const nickname = input.value.trim();

        if (!nickname) {
            status.textContent = "Zadaj nejakú prezývku.";
            return;
        }

        submitBtn.disabled = true;

        try {
            const response = await fetch("/nickname", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ nickname: nickname })
            });

            const data = await response.json();

            if (!response.ok) {
                status.textContent = data.error || "Chyba pri ukladaní.";
                return;
            }

            status.textContent = "✓ Prezývka bola úspešne uložená.";
            await refreshPosts();
        } catch (error) {
            status.textContent = "Nepodarilo sa uložiť prezývku.";
        } finally {
            submitBtn.disabled = false;
        }
    }
);
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
* {
    box-sizing: border-box;
}

body {
    background: #101827;
    color: white;
    font-family: Arial, sans-serif;
    margin: 0;
    padding: 20px;
}

.container {
    max-width: 650px;
    margin: auto;
}

header {
    text-align: center;
    padding: 20px 10px;
}

h1 {
    color: #42a5ff;
    font-size: 32px;
    margin: 0 0 8px 0;
}

.subtitle {
    color: #9aabc0;
    margin: 6px 0;
}

.nav-tabs {
    display: flex;
    gap: 10px;
    justify-content: center;
    margin-bottom: 22px;
}

.nav-tab {
    flex: 1;
    text-align: center;
    padding: 12px;
    background: #1d2b40;
    border: 1px solid #344761;
    border-radius: 10px;
    color: #b6c5d8;
    text-decoration: none;
    font-weight: bold;
    transition: 0.15s;
}

.nav-tab:hover {
    border-color: #42a5ff;
    color: white;
}

.nav-tab.active {
    background: #2689e8;
    border-color: #2689e8;
    color: white;
}

.room-card {
    display: block;
    background: #1d2b40;
    padding: 18px 20px;
    border-radius: 14px;
    margin-bottom: 14px;
    border: 1px solid #344761;
    text-decoration: none;
    color: white;
    transition: 0.15s;
}

.room-card:hover {
    border-color: #42a5ff;
    transform: translateY(-2px);
}

.room-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
}

.room-title {
    font-size: 19px;
    font-weight: bold;
    color: #42a5ff;
}

.room-badge {
    background: #101827;
    border: 1px solid #344761;
    color: #9aabc0;
    padding: 4px 10px;
    border-radius: 20px;
    font-size: 12px;
    white-space: nowrap;
}

.room-desc {
    color: #b6c5d8;
    font-size: 14px;
    margin-top: 8px;
}

.user-info-bar {
    background: #152133;
    border: 1px solid #344761;
    padding: 12px 16px;
    border-radius: 10px;
    margin-bottom: 20px;
    font-size: 14px;
    color: #9aabc0;
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.user-info-bar a {
    color: #42a5ff;
    text-decoration: none;
    font-size: 13px;
}
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
    <a href="/chat" class="nav-tab active">💬 Chatovacie miestnosti</a>
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
        {% if room.description %}
            <div class="room-desc">{{ room.description }}</div>
        {% endif %}
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
* {
    box-sizing: border-box;
}

body {
    background: #101827;
    color: white;
    font-family: Arial, sans-serif;
    margin: 0;
    padding: 15px;
    height: 100dvh;
    display: flex;
    flex-direction: column;
}

.chat-wrapper {
    max-width: 650px;
    width: 100%;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    height: 100%;
}

.chat-header {
    background: #1d2b40;
    padding: 14px 18px;
    border-radius: 14px;
    border: 1px solid #344761;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    margin-bottom: 12px;
}

.back-btn {
    color: #42a5ff;
    text-decoration: none;
    font-weight: bold;
    font-size: 14px;
    white-space: nowrap;
}

.room-info {
    text-align: center;
}

.room-info h2 {
    margin: 0;
    font-size: 18px;
    color: white;
}

.room-info small {
    color: #8ca0b8;
    font-size: 12px;
}

.messages-box {
    flex: 1;
    background: #152133;
    border: 1px solid #344761;
    border-radius: 14px;
    padding: 15px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin-bottom: 12px;
}

.msg {
    max-width: 80%;
    padding: 10px 14px;
    border-radius: 14px;
    background: #1d2b40;
    border: 1px solid #2c3f59;
    align-self: flex-start;
    overflow-wrap: anywhere;
}

.msg.mine {
    align-self: flex-end;
    background: #1a4b82;
    border-color: #2689e8;
}

.msg-meta {
    font-size: 11px;
    color: #8ca0b8;
    margin-bottom: 4px;
}

.msg.mine .msg-meta {
    color: #b8dcff;
    text-align: right;
}

.msg-author {
    font-weight: bold;
    color: #42a5ff;
}

.msg.mine .msg-author {
    color: white;
}

.msg-text {
    font-size: 15px;
    line-height: 1.4;
    white-space: pre-wrap;
}

.chat-form {
    display: flex;
    gap: 10px;
}

.chat-input {
    flex: 1;
    padding: 13px 15px;
    background: #1d2b40;
    border: 1px solid #344761;
    border-radius: 12px;
    color: white;
    font-size: 15px;
}

.chat-input:focus {
    outline: none;
    border-color: #42a5ff;
}

.send-btn {
    padding: 13px 20px;
    background: #2689e8;
    border: none;
    border-radius: 12px;
    color: white;
    font-weight: bold;
    font-size: 15px;
    cursor: pointer;
}

.send-btn:hover {
    background: #1472cb;
}

.send-btn:disabled {
    opacity: 0.6;
    cursor: wait;
}

.empty-chat {
    text-align: center;
    color: #71869f;
    margin: auto;
    font-size: 14px;
}
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
    <input
        type="text"
        id="chat-input"
        class="chat-input"
        maxlength="400"
        placeholder="Napíš správu do miestnosti..."
        autocomplete="off"
        required
    >
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

        const isNearBottom =
            messagesBox.scrollHeight - messagesBox.scrollTop - messagesBox.clientHeight < 90;

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
    } catch (e) {
        console.log("Chyba pri načítaní chatu.");
    }
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
setInterval(() => loadMessages(false), 2000);
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
                    existing = conn.execute(
                        "SELECT id FROM accounts WHERE LOWER(username) = LOWER(?)",
                        (username,)
                    ).fetchone()

                    if existing:
                        error = "Toto používateľské meno je už obsadené."
                    else:
                        created_at = datetime.now().strftime("%d.%m.%Y %H:%M")
                        cursor = conn.execute(
                            "INSERT INTO accounts (username, password_hash, created_at) VALUES (?, ?, ?)",
                            (username, generate_password_hash(password), created_at)
                        )
                        account_id = cursor.lastrowid

                old_visitor_id = g.cookie_visitor_id
                migrate_visitor_to_account(old_visitor_id, account_id, username)
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
                acc = conn.execute(
                    "SELECT id, username, password_hash FROM accounts WHERE LOWER(username) = LOWER(?)",
                    (username,)
                ).fetchone()

            if not acc or not check_password_hash(acc[2], password):
                error = "Nesprávne používateľské meno alebo heslo."
            else:
                account_id, real_username = acc[0], acc[1]
                old_visitor_id = g.cookie_visitor_id
                migrate_visitor_to_account(old_visitor_id, account_id, real_username)
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
    session.pop("account_id", None)
    return redirect("/")


# ==========================================
# ZISKANIE PRISPEVKOV VRATANE ANKIET
# ==========================================

def get_posts():
    visitor_id = g.visitor_id

    with get_db() as conn:
        rows = conn.execute("""
            SELECT
                p.id,
                p.message,
                p.created_at,
                p.image,
                COALESCE(u.nickname, 'Anonymný používateľ') AS nickname,
                (
                    SELECT COUNT(*)
                    FROM likes l
                    WHERE l.post_id = p.id
                ) AS like_count,
                EXISTS (
                    SELECT 1
                    FROM likes l
                    WHERE l.post_id = p.id
                    AND l.visitor_id = ?
                ) AS liked
            FROM posts p
            LEFT JOIN users u
                ON p.visitor_id = u.visitor_id
            ORDER BY p.id DESC
        """, (visitor_id,)).fetchall()

        posts = []
        for row in rows:
            post_id = row[0]

            poll_row = conn.execute(
                "SELECT id FROM polls WHERE post_id = ?",
                (post_id,)
            ).fetchone()

            poll_data = None
            if poll_row:
                poll_id = poll_row[0]

                user_vote_row = conn.execute(
                    "SELECT option_id FROM poll_votes WHERE poll_id = ? AND visitor_id = ?",
                    (poll_id, visitor_id)
                ).fetchone()
                user_vote = user_vote_row[0] if user_vote_row else None

                option_rows = conn.execute("""
                    SELECT
                        po.id,
                        po.option_text,
                        (
                            SELECT COUNT(*)
                            FROM poll_votes pv
                            WHERE pv.option_id = po.id
                        ) AS votes
                    FROM poll_options po
                    WHERE po.poll_id = ?
                    ORDER BY po.id ASC
                """, (poll_id,)).fetchall()

                total_votes = sum(opt[2] for opt in option_rows)

                options = []
                for opt in option_rows:
                    percent = round((opt[2] / total_votes) * 100) if total_votes > 0 else 0
                    options.append({
                        "id": opt[0],
                        "text": opt[1],
                        "votes": opt[2],
                        "percent": percent
                    })

                poll_data = {
                    "id": poll_id,
                    "options": options,
                    "total_votes": total_votes,
                    "user_vote": user_vote
                }

            posts.append({
                "id": row[0],
                "message": row[1],
                "created_at": row[2],
                "image": row[3],
                "nickname": row[4],
                "like_count": row[5],
                "liked": row[6],
                "poll": poll_data
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
        photo = request.files.get("photo")
        raw_options = request.form.getlist("poll_options[]")
        poll_options = [opt.strip()[:80] for opt in raw_options if opt.strip()]

        image_name = None

        if len(post) > 500:
            message = "Text je príliš dlhý."
        elif len(poll_options) == 1:
            message = "Anketa musí obsahovať aspoň 2 možnosti."
        elif len(poll_options) > 5:
            message = "Anketa môže mať maximálne 5 možností."
        elif not post and (not photo or not photo.filename) and not poll_options:
            message = "Napíš text, vyber fotografiu alebo vytvor anketu."
        elif photo and photo.filename:
            try:
                if "." not in photo.filename:
                    raise ValueError()

                extension = photo.filename.rsplit(".", 1)[1].lower()

                allowed_formats = {
                    "jpg": "JPEG",
                    "jpeg": "JPEG",
                    "png": "PNG",
                    "webp": "WEBP",
                    "gif": "GIF"
                }

                if extension not in allowed_formats:
                    raise ValueError()

                img = Image.open(photo.stream)

                if img.format != allowed_formats[extension]:
                    raise ValueError()

                if img.width * img.height > 20000000:
                    raise ValueError()

                if img.format == "GIF":
                    frame_count = getattr(img, "n_frames", 1)

                    if frame_count > 150:
                        raise ValueError()

                    if img.width * img.height * frame_count > 100000000:
                        raise ValueError()

                    for frame in range(frame_count):
                        img.seek(frame)
                        img.load()

                    photo.stream.seek(0)
                    image_name = f"{uuid.uuid4()}.gif"
                    image_path = os.path.join(UPLOAD_FOLDER, image_name)

                    with open(image_path, "wb") as file:
                        file.write(photo.stream.read())

                else:
                    img.verify()
                    photo.stream.seek(0)

                    img = Image.open(photo.stream)
                    img = ImageOps.exif_transpose(img)

                    if img.mode not in ("RGB", "RGBA"):
                        img = img.convert("RGB")

                    img.thumbnail((1600, 1600))

                    image_name = f"{uuid.uuid4()}.webp"
                    image_path = os.path.join(UPLOAD_FOLDER, image_name)

                    img.save(image_path, "WEBP", quality=82)

            except (UnidentifiedImageError, OSError, ValueError, EOFError):
                message = "Fotografia alebo GIF je neplatný, príliš veľký alebo nepodporovaný."

        if not message:
            created_at = datetime.now().strftime("%d.%m.%Y %H:%M")

            with get_db() as conn:
                cursor = conn.execute("""
                    INSERT INTO posts
                    (message, created_at, image, visitor_id)
                    VALUES (?, ?, ?, ?)
                """, (post, created_at, image_name, g.visitor_id))

                post_id = cursor.lastrowid

                if poll_options:
                    poll_cursor = conn.execute(
                        "INSERT INTO polls (post_id) VALUES (?)",
                        (post_id,)
                    )
                    poll_id = poll_cursor.lastrowid

                    for opt_text in poll_options:
                        conn.execute(
                            "INSERT INTO poll_options (poll_id, option_text) VALUES (?, ?)",
                            (poll_id, opt_text)
                        )

            return redirect("/")

    posts = get_posts()
    posts_html = render_template_string(POSTS_HTML, posts=posts)

    return render_template_string(
        HTML,
        posts_html=posts_html,
        message=message,
        nickname=get_nickname(),
        account=g.account
    )


# ==========================================
# CHATOVACIE MIESTNOSTI - ROUTES
# ==========================================

@app.route("/chat")
def chat_rooms():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT
                r.id,
                r.name,
                r.description,
                (
                    SELECT COUNT(*)
                    FROM chat_messages m
                    WHERE m.room_id = r.id
                ) AS message_count
            FROM chat_rooms r
            ORDER BY r.id ASC
        """).fetchall()

    rooms = [
        {
            "id": r[0],
            "name": r[1],
            "description": r[2],
            "message_count": r[3]
        }
        for r in rows
    ]

    return render_template_string(
        CHAT_ROOMS_HTML,
        rooms=rooms,
        nickname=get_nickname(),
        account=g.account
    )


@app.route("/chat/<int:room_id>")
def chat_room_detail(room_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, description FROM chat_rooms WHERE id = ?",
            (room_id,)
        ).fetchone()

    if not row:
        return redirect("/chat")

    room = {"id": row[0], "name": row[1], "description": row[2]}

    return render_template_string(
        CHAT_ROOM_DETAIL_HTML,
        room=room,
        nickname=get_nickname()
    )


@app.route("/chat/<int:room_id>/messages")
def chat_room_messages(room_id):
    visitor_id = g.visitor_id

    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM (
                SELECT
                    m.id,
                    m.message,
                    m.created_at,
                    m.visitor_id,
                    COALESCE(u.nickname, 'Anonymný používateľ') AS nickname
                FROM chat_messages m
                LEFT JOIN users u
                    ON m.visitor_id = u.visitor_id
                WHERE m.room_id = ?
                ORDER BY m.id DESC
                LIMIT 100
            ) sub
            ORDER BY id ASC
        """, (room_id,)).fetchall()

    messages = [
        {
            "id": r[0],
            "message": r[1],
            "created_at": r[2],
            "is_mine": (r[3] == visitor_id),
            "nickname": r[4]
        }
        for r in rows
    ]

    return jsonify(messages)


@app.route("/chat/<int:room_id>/send", methods=["POST"])
def chat_room_send(room_id):
    data = request.get_json(silent=True) or {}
    text = str(data.get("message", "")).strip()

    if not text or len(text) > 400:
        return jsonify({"error": "Neplatná správa."}), 400

    with get_db() as conn:
        room = conn.execute(
            "SELECT id FROM chat_rooms WHERE id = ?",
            (room_id,)
        ).fetchone()

        if not room:
            return jsonify({"error": "Miestnosť neexistuje."}), 404

        created_at = datetime.now().strftime("%H:%M")
        conn.execute("""
            INSERT INTO chat_messages (room_id, visitor_id, message, created_at)
            VALUES (?, ?, ?, ?)
        """, (room_id, g.visitor_id, text, created_at))

    return jsonify({"success": True})


# ==========================================
# HLASOVANIE V ANKETE
# ==========================================

@app.route("/poll/<int:poll_id>/vote/<int:option_id>", methods=["POST"])
def vote_poll(poll_id, option_id):
    visitor_id = g.visitor_id

    with get_db() as conn:
        option = conn.execute(
            "SELECT id FROM poll_options WHERE id = ? AND poll_id = ?",
            (option_id, poll_id)
        ).fetchone()

        if not option:
            return jsonify({"error": "Neplatná možnosť ankety."}), 404

        conn.execute("""
            INSERT INTO poll_votes (poll_id, option_id, visitor_id)
            VALUES (?, ?, ?)
            ON CONFLICT(poll_id, visitor_id)
            DO UPDATE SET option_id = excluded.option_id
        """, (poll_id, option_id, visitor_id))

    return jsonify({"success": True})


# ==========================================
# ZMENA PREZÝVKY
# ==========================================

@app.route("/nickname", methods=["POST"])
def change_nickname():
    data = request.get_json(silent=True) or {}
    nickname = str(data.get("nickname", "")).strip()

    if not nickname:
        return jsonify({"error": "Prezývka nemôže byť prázdna."}), 400

    if len(nickname) > 20:
        return jsonify({"error": "Prezývka môže mať maximálne 20 znakov."}), 400

    with get_db() as conn:
        conn.execute("""
            UPDATE users
            SET nickname = ?
            WHERE visitor_id = ?
        """, (nickname, g.visitor_id))

    return jsonify({"success": True, "nickname": nickname})


# ==========================================
# AUTOMATICKA AKTUALIZACIA
# ==========================================

@app.route("/posts")
def posts_update():
    posts = get_posts()
    return render_template_string(POSTS_HTML, posts=posts)


# ==========================================
# LAJKOVANIE
# ==========================================

@app.route("/like/<int:post_id>", methods=["POST"])
def toggle_like(post_id):
    visitor_id = g.visitor_id

    with get_db() as conn:
        post = conn.execute(
            "SELECT id FROM posts WHERE id = ?",
            (post_id,)
        ).fetchone()

        if not post:
            return jsonify({"error": "Príspevok neexistuje."}), 404

        existing_like = conn.execute("""
            SELECT id
            FROM likes
            WHERE post_id = ? AND visitor_id = ?
        """, (post_id, visitor_id)).fetchone()

        if existing_like:
            conn.execute("""
                DELETE FROM likes
                WHERE post_id = ? AND visitor_id = ?
            """, (post_id, visitor_id))
            liked = False
        else:
            conn.execute("""
                INSERT OR IGNORE INTO likes
                (post_id, visitor_id)
                VALUES (?, ?)
            """, (post_id, visitor_id))
            liked = True

        count = conn.execute("""
            SELECT COUNT(*)
            FROM likes
            WHERE post_id = ?
        """, (post_id,)).fetchone()[0]

    return jsonify({"liked": liked, "count": count})


# ==========================================
# CHYBA PRI VELKOM SUBORE
# ==========================================

@app.errorhandler(413)
def too_large(error):
    return "Súbor je príliš veľký. Maximálna veľkosť je 15 MB.", 413


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
* {
    box-sizing: border-box;
}

body {
    background: #101827;
    color: white;
    font-family: Arial, sans-serif;
    margin: 0;
    padding: 20px;
}

.container {
    max-width: 750px;
    margin: auto;
}

.panel, .post {
    background: #1d2b40;
    padding: 20px;
    border-radius: 12px;
    margin-bottom: 15px;
}

h1 {
    color: #42a5ff;
}

input {
    width: 100%;
    padding: 13px;
    margin: 8px 0;
    background: #101827;
    color: white;
    border: 1px solid #344761;
    border-radius: 8px;
}

button {
    cursor: pointer;
    border: none;
    border-radius: 8px;
    padding: 12px;
    color: white;
}

.login-button, .create-button {
    width: 100%;
    background: #2689e8;
}

.delete-button {
    background: #d93650;
    margin-top: 8px;
}

.logout-button {
    background: #475b73;
}

.post-image {
    width: 100%;
    max-height: 300px;
    object-fit: contain;
    margin-top: 10px;
}

.error {
    color: #ff7777;
}

.info {
    color: #9aabc0;
}

.back-link {
    display: inline-block;
    margin-bottom: 15px;
    color: #42a5ff;
    text-decoration: none;
}

.admin-poll {
    background: #152133;
    padding: 10px 14px;
    border-radius: 8px;
    margin-top: 10px;
    font-size: 14px;
}

.room-admin-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: #152133;
    padding: 12px 15px;
    border-radius: 8px;
    margin-top: 10px;
    gap: 10px;
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

        {% if error %}
            <p class="error">{{ error }}</p>
        {% endif %}

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

    <!-- SPRAVA CHATOVACICH MIESTNOSTI -->
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
                <form
                    method="POST"
                    action="/admin/rooms/delete/{{ room[0] }}"
                    onsubmit="return confirm('Vymazať miestnosť aj so všetkými správami v nej?');"
                >
                    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                    <button class="delete-button" type="submit">🗑 Vymazať</button>
                </form>
            </div>
        {% endfor %}
    </div>

    <h2>Príspevky komunity</h2>

    {% for post in posts %}
        <div class="post">
            {% if post.message %}
                <p>{{ post.message }}</p>
            {% endif %}

            {% if post.image %}
                <img
                    class="post-image"
                    src="{{ url_for('static', filename='uploads/' + post.image) }}"
                >
            {% endif %}

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
                Lajky: {{ post.like_count }}
            </p>

            <form
                method="POST"
                action="/admin/delete/{{ post.id }}"
                onsubmit="return confirm('Naozaj chceš vymazať tento príspevok?');"
            >
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


# ==========================================
# PRIHLASENIE ADMINISTRATORA
# ==========================================

@app.route("/admin", methods=["GET", "POST"])
def admin():
    error = ""

    if request.method == "POST":
        if not valid_admin_csrf():
            error = "Neplatná požiadavka. Obnov stránku."
        else:
            username = request.form.get("username", "")
            password = request.form.get("password", "")

            if (
                username == ADMIN_USERNAME
                and check_password_hash(ADMIN_PASSWORD_HASH, password)
            ):
                session.clear()
                session["is_admin"] = True
                return redirect("/admin")
            else:
                error = "Nesprávne meno alebo heslo."

    logged_in = session.get("is_admin", False)
    posts = get_posts() if logged_in else []
    rooms = []

    if logged_in:
        with get_db() as conn:
            rooms = conn.execute(
                "SELECT id, name, description FROM chat_rooms ORDER BY id ASC"
            ).fetchall()

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
            conn.execute(
                "INSERT OR IGNORE INTO chat_rooms (name, description) VALUES (?, ?)",
                (name, description)
            )

    return redirect("/admin")


@app.route("/admin/rooms/delete/<int:room_id>", methods=["POST"])
def admin_delete_room(room_id):
    if not session.get("is_admin"):
        return redirect("/admin")

    if not valid_admin_csrf():
        return "Neplatná požiadavka.", 400

    with get_db() as conn:
        conn.execute("DELETE FROM chat_messages WHERE room_id = ?", (room_id,))
        conn.execute("DELETE FROM chat_rooms WHERE id = ?", (room_id,))

    return redirect("/admin")


# ==========================================
# ODHLASENIE ADMINISTRATORA
# ==========================================

@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    if not session.get("is_admin"):
        return redirect("/admin")

    if not valid_admin_csrf():
        return "Neplatná požiadavka.", 400

    session.clear()
    return redirect("/admin")


# ==========================================
# VYMAZANIE PRISPEVKU VRATANE ANKETY
# ==========================================

@app.route("/admin/delete/<int:post_id>", methods=["POST"])
def admin_delete_post(post_id):
    if not session.get("is_admin"):
        return redirect("/admin")

    if not valid_admin_csrf():
        return "Neplatná požiadavka.", 400

    with get_db() as conn:
        post = conn.execute(
            "SELECT image FROM posts WHERE id = ?",
            (post_id,)
        ).fetchone()

        if post:
            image_name = post[0]

            poll = conn.execute(
                "SELECT id FROM polls WHERE post_id = ?",
                (post_id,)
            ).fetchone()

            if poll:
                poll_id = poll[0]
                conn.execute("DELETE FROM poll_votes WHERE poll_id = ?", (poll_id,))
                conn.execute("DELETE FROM poll_options WHERE poll_id = ?", (poll_id,))
                conn.execute("DELETE FROM polls WHERE id = ?", (poll_id,))

            conn.execute("DELETE FROM likes WHERE post_id = ?", (post_id,))
            conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))

            if image_name:
                safe_name = os.path.basename(image_name)
                image_path = os.path.join(UPLOAD_FOLDER, safe_name)
                if os.path.isfile(image_path):
                    os.remove(image_path)

    return redirect("/admin")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)