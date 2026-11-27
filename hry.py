from flask import Blueprint, request, render_template_string, jsonify, session
import os
import secrets
import psycopg

games_bp = Blueprint("games_bp", __name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
_GAMES_DB_READY = False


def _open_games_db():
    if not DATABASE_URL:
        raise RuntimeError("Chýba premenná prostredia DATABASE_URL.")
    return psycopg.connect(DATABASE_URL)


def _ensure_games_schema(conn):
    global _GAMES_DB_READY
    if _GAMES_DB_READY:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS minesweeper_scores (
                id SERIAL PRIMARY KEY,
                difficulty TEXT NOT NULL,
                seconds INTEGER NOT NULL,
                visitor_id TEXT NOT NULL,
                nickname TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
    conn.commit()
    _GAMES_DB_READY = True


def _get_games_db():
    conn = _open_games_db()
    _ensure_games_schema(conn)
    return conn


def _get_current_player():
    account_id = session.get("account_id")
    cookie_visitor_id = request.cookies.get("visitor_id", "")

    if account_id:
        visitor_id = f"acc:{account_id}"
    elif cookie_visitor_id and not cookie_visitor_id.startswith("acc:"):
        visitor_id = cookie_visitor_id
    else:
        visitor_id = "anon"

    nickname = "Anonymný používateľ"
    with _get_games_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT nickname FROM users WHERE visitor_id = %s", (visitor_id,))
            row = cur.fetchone()
            if row and row[0]:
                nickname = row[0]
            elif account_id:
                cur.execute("SELECT username FROM accounts WHERE id = %s", (account_id,))
                acc_row = cur.fetchone()
                if acc_row and acc_row[0]:
                    nickname = acc_row[0][:20]

            unread_dm = 0
            if account_id:
                cur.execute(
                    "SELECT COUNT(*) FROM direct_messages WHERE recipient_account_id = %s AND is_read = 0",
                    (account_id,)
                )
                u_row = cur.fetchone()
                unread_dm = u_row[0] if u_row else 0

    return {
        "visitor_id": visitor_id,
        "nickname": nickname,
        "account_id": account_id,
        "unread_dm_count": unread_dm,
    }


def _get_leaderboard(difficulty="easy"):
    if difficulty not in ("easy", "medium", "hard"):
        difficulty = "easy"

    with _get_games_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT nickname, seconds, created_at
                FROM minesweeper_scores
                WHERE difficulty = %s
                ORDER BY seconds ASC, id ASC
                LIMIT 10
            """, (difficulty,))
            rows = cur.fetchall()

    return [
        {"nickname": r[0], "seconds": r[1], "created_at": r[2]}
        for r in rows
    ]


GAMES_HTML = """
<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Minihry - Míny</title>
<style>
* { box-sizing: border-box; }
body { background: #101827; color: white; font-family: Arial, sans-serif; margin: 0; padding: 20px; }
.container { max-width: 650px; margin: auto; }
header { text-align: center; padding: 20px 10px; }
h1 { color: #42a5ff; font-size: 32px; margin: 0 0 8px 0; }
.subtitle { color: #9aabc0; margin: 6px 0; }
.nav-tabs { display: flex; gap: 8px; justify-content: center; margin-bottom: 22px; flex-wrap: wrap; }
.nav-tab {
    flex: 1; min-width: 125px; text-align: center; padding: 11px 10px; background: #1d2b40;
    border: 1px solid #344761; border-radius: 10px; color: #b6c5d8;
    text-decoration: none; font-weight: bold; font-size: 14px;
    display: inline-flex; justify-content: center; align-items: center; gap: 6px;
}
.nav-tab.active { background: #2689e8; border-color: #2689e8; color: white; }
.unread-badge {
    background: #ff405e; color: white; border-radius: 999px;
    padding: 1px 7px; font-size: 11px; font-weight: bold;
}
.panel {
    background: #1d2b40; padding: 20px; border-radius: 15px;
    border: 1px solid #344761; margin-bottom: 20px;
}
.controls-row {
    display: flex; justify-content: space-between; align-items: center;
    flex-wrap: wrap; gap: 10px; margin-bottom: 14px;
}
.diff-group, .mode-group { display: flex; gap: 6px; flex-wrap: wrap; }
.small-btn {
    padding: 8px 12px; border-radius: 8px; border: 1px solid #3d5678;
    background: #293c55; color: #d6e4f5; font-size: 13px; font-weight: bold; cursor: pointer;
}
.small-btn.active { background: #2689e8; border-color: #2689e8; color: white; }
.reset-btn {
    padding: 8px 14px; border-radius: 8px; border: none;
    background: #2e8b57; color: white; font-size: 13px; font-weight: bold; cursor: pointer;
}
.hud-bar {
    display: flex; justify-content: space-between; align-items: center;
    background: #152133; border: 1px solid #344761; border-radius: 10px;
    padding: 10px 14px; margin-bottom: 14px; font-size: 14px; font-weight: bold;
}
.status-text { text-align: center; min-height: 22px; margin-bottom: 12px; font-size: 14px; color: #ffd166; }
.board-wrap { overflow-x: auto; display: flex; justify-content: center; }
.board {
    display: grid; gap: 4px; background: #152133; padding: 10px;
    border-radius: 12px; border: 1px solid #344761; user-select: none;
}
.cell {
    width: 34px; height: 34px; border-radius: 6px; border: 1px solid #3b5272;
    background: #263954; color: white; font-weight: bold; font-size: 15px;
    display: flex; align-items: center; justify-content: center; cursor: pointer; padding: 0;
}
.cell:hover:not(.revealed) { background: #31496b; }
.cell.revealed { background: #101827; border-color: #233247; cursor: default; }
.cell.mine-hit { background: #8f1d2c; border-color: #ff4d6d; }
.n1 { color: #4da6ff; }
.n2 { color: #52d17c; }
.n3 { color: #ff6b6b; }
.n4 { color: #b57cff; }
.n5 { color: #ffa94d; }
.n6 { color: #4dd0e1; }
.n7 { color: #f06292; }
.n8 { color: #cfd8dc; }

.lb-table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 14px; }
.lb-table th, .lb-table td { padding: 9px 10px; border-bottom: 1px solid #2c3f59; text-align: left; }
.lb-table th { color: #9aabc0; font-size: 12px; text-transform: uppercase; }
.lb-empty { color: #8ca0b8; font-size: 14px; text-align: center; padding: 14px 0; }
</style>
</head>
<body>
<div class="container">
<header>
    <h1>MOJA KOMUNITA</h1>
    <p class="subtitle">Herný kútik · Hráš ako <strong>{{ player.nickname }}</strong></p>
</header>

<nav class="nav-tabs">
    <a href="/" class="nav-tab">📋 Nástenka</a>
    <a href="/chat" class="nav-tab">💬 Chat</a>
    <a href="/messages" class="nav-tab">
        ✉️ Súkromné
        {% if player.unread_dm_count > 0 %}<span class="unread-badge">{{ player.unread_dm_count }}</span>{% endif %}
    </a>
    <a href="/games" class="nav-tab active">🎮 Minihry</a>
</nav>

<div class="panel">
    <h2 style="margin-top: 0;">💣 Míny</h2>

    <div class="controls-row">
        <div class="diff-group">
            <button type="button" class="small-btn active" data-diff="easy">Ľahká (8×8)</button>
            <button type="button" class="small-btn" data-diff="medium">Stredná (10×10)</button>
            <button type="button" class="small-btn" data-diff="hard">Ťažká (12×12)</button>
        </div>
        <button type="button" class="reset-btn" id="reset-btn">↻ Nová hra</button>
    </div>

    <div class="controls-row">
        <div class="mode-group">
            <button type="button" class="small-btn active" id="mode-reveal">⛏️ Odkryť</button>
            <button type="button" class="small-btn" id="mode-flag">🚩 Vlajka</button>
        </div>
        <span class="subtitle" style="font-size: 12px;">Na PC: pravý klik = vlajka</span>
    </div>

    <div class="hud-bar">
        <div>💣 Míny: <span id="mines-left">10</span></div>
        <div>⏱️ Čas: <span id="timer">0</span> s</div>
    </div>

    <div class="status-text" id="status-text">Klikni na políčko a začni.</div>

    <div class="board-wrap">
        <div class="board" id="board"></div>
    </div>
</div>

<div class="panel">
    <h3 style="margin-top: 0;">🏆 Najlepšie časy (<span id="lb-diff-label">Ľahká</span>)</h3>
    <div id="leaderboard-box"></div>
</div>
</div>

<script>
const CONFIGS = {
    easy:   { rows: 8,  cols: 8,  mines: 10, label: "Ľahká" },
    medium: { rows: 10, cols: 10, mines: 16, label: "Stredná" },
    hard:   { rows: 12, cols: 12, mines: 25, label: "Ťažká" }
};

let currentDiff = "easy";
let flagMode = false;
let grid = [];
let gameOver = false;
let gameWon = false;
let firstClick = true;
let seconds = 0;
let timerId = null;

const boardEl = document.getElementById("board");
const minesLeftEl = document.getElementById("mines-left");
const timerEl = document.getElementById("timer");
const statusEl = document.getElementById("status-text");
const lbBox = document.getElementById("leaderboard-box");
const lbDiffLabel = document.getElementById("lb-diff-label");

function stopTimer() {
    if (timerId) {
        clearInterval(timerId);
        timerId = null;
    }
}

function startTimer() {
    if (timerId) return;
    timerId = setInterval(() => {
        seconds += 1;
        timerEl.textContent = seconds;
    }, 1000);
}

function initGame() {
    stopTimer();
    const cfg = CONFIGS[currentDiff];
    gameOver = false;
    gameWon = false;
    firstClick = true;
    seconds = 0;
    timerEl.textContent = "0";
    minesLeftEl.textContent = cfg.mines;
    statusEl.textContent = "Klikni na políčko a začni.";

    grid = Array.from({ length: cfg.rows }, (_, r) =>
        Array.from({ length: cfg.cols }, (_, c) => ({
            r, c, mine: false, revealed: false, flagged: false, adj: 0
        }))
    );

    boardEl.style.gridTemplateColumns = `repeat(${cfg.cols}, 34px)`;
    renderBoard();
    loadLeaderboard(currentDiff);
}

function placeMines(safeR, safeC) {
    const cfg = CONFIGS[currentDiff];
    let placed = 0;
    while (placed < cfg.mines) {
        const r = Math.floor(Math.random() * cfg.rows);
        const c = Math.floor(Math.random() * cfg.cols);
        if (grid[r][c].mine) continue;
        if (Math.abs(r - safeR) <= 1 && Math.abs(c - safeC) <= 1) continue;
        grid[r][c].mine = true;
        placed += 1;
    }

    for (let r = 0; r < cfg.rows; r++) {
        for (let c = 0; c < cfg.cols; c++) {
            if (grid[r][c].mine) continue;
            let count = 0;
            for (let dr = -1; dr <= 1; dr++) {
                for (let dc = -1; dc <= 1; dc++) {
                    const nr = r + dr, nc = c + dc;
                    if (nr >= 0 && nr < cfg.rows && nc >= 0 && nc < cfg.cols && grid[nr][nc].mine) {
                        count += 1;
                    }
                }
            }
            grid[r][c].adj = count;
        }
    }
}

function toggleFlag(r, c) {
    if (gameOver || gameWon) return;
    const cell = grid[r][c];
    if (cell.revealed) return;
    cell.flagged = !cell.flagged;
    updateMinesCounter();
    renderBoard();
}

function updateMinesCounter() {
    const cfg = CONFIGS[currentDiff];
    let flags = 0;
    for (const row of grid) {
        for (const cell of row) {
            if (cell.flagged) flags += 1;
        }
    }
    minesLeftEl.textContent = cfg.mines - flags;
}

function revealCell(r, c) {
    if (gameOver || gameWon) return;
    const cell = grid[r][c];
    if (cell.revealed || cell.flagged) return;

    if (firstClick) {
        firstClick = false;
        placeMines(r, c);
        startTimer();
    }

    if (cell.mine) {
        cell.revealed = true;
        cell.hit = true;
        gameOver = true;
        stopTimer();
        for (const row of grid) {
            for (const item of row) {
                if (item.mine) item.revealed = true;
            }
        }
        statusEl.textContent = "💥 Narazil si na mínu! Skús to znova.";
        renderBoard();
        return;
    }

    const stack = [[r, c]];
    while (stack.length > 0) {
        const [cr, cc] = stack.pop();
        const cur = grid[cr][cc];
        if (cur.revealed || cur.flagged) continue;
        cur.revealed = true;

        if (cur.adj === 0) {
            for (let dr = -1; dr <= 1; dr++) {
                for (let dc = -1; dc <= 1; dc++) {
                    const nr = cr + dr, nc = cc + dc;
                    if (nr >= 0 && nr < grid.length && nc >= 0 && nc < grid[0].length) {
                        if (!grid[nr][nc].revealed && !grid[nr][nc].mine) {
                            stack.push([nr, nc]);
                        }
                    }
                }
            }
        }
    }

    checkWin();
    renderBoard();
}

function checkWin() {
    for (const row of grid) {
        for (const cell of row) {
            if (!cell.mine && !cell.revealed) return;
        }
    }
    gameWon = true;
    stopTimer();
    statusEl.textContent = `🎉 Vyhral si za ${seconds} s! Ukladám skóre...`;
    submitScore(currentDiff, seconds);
}

async function submitScore(diff, sec) {
    try {
        const res = await fetch("/games/minesweeper/score", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ difficulty: diff, seconds: sec })
        });
        if (res.ok) {
            statusEl.textContent = `🎉 Vyhral si za ${seconds} s! Rekord zapísaný.`;
            await loadLeaderboard(diff);
        }
    } catch (e) {}
}

async function loadLeaderboard(diff) {
    lbDiffLabel.textContent = CONFIGS[diff].label;
    try {
        const res = await fetch(`/games/minesweeper/leaderboard?difficulty=${diff}`, { cache: "no-store" });
        if (!res.ok) return;
        const rows = await res.json();
        if (!rows.length) {
            lbBox.innerHTML = '<div class="lb-empty">Zatiaľ žiadne zapísané časy. Buď prvý!</div>';
            return;
        }
        lbBox.innerHTML = `
            <table class="lb-table">
                <thead>
                    <tr><th>#</th><th>Hráč</th><th>Čas</th><th>Dátum</th></tr>
                </thead>
                <tbody>
                    ${rows.map((item, idx) => `
                        <tr>
                            <td>${idx + 1}.</td>
                            <td><strong>${escapeHtml(item.nickname)}</strong></td>
                            <td>${item.seconds} s</td>
                            <td>${escapeHtml(item.created_at)}</td>
                        </tr>
                    `).join("")}
                </tbody>
            </table>
        `;
    } catch (e) {}
}

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

function renderBoard() {
    boardEl.innerHTML = "";
    for (let r = 0; r < grid.length; r++) {
        for (let c = 0; c < grid[0].length; c++) {
            const cell = grid[r][c];
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "cell";

            if (cell.revealed) {
                btn.classList.add("revealed");
                if (cell.mine) {
                    btn.textContent = "💣";
                    if (cell.hit) btn.classList.add("mine-hit");
                } else if (cell.adj > 0) {
                    btn.textContent = cell.adj;
                    btn.classList.add(`n${cell.adj}`);
                }
            } else if (cell.flagged) {
                btn.textContent = "🚩";
            }

            btn.addEventListener("click", () => {
                if (flagMode) toggleFlag(r, c);
                else revealCell(r, c);
            });

            btn.addEventListener("contextmenu", (e) => {
                e.preventDefault();
                toggleFlag(r, c);
            });

            boardEl.appendChild(btn);
        }
    }
}

document.querySelectorAll("[data-diff]").forEach(btn => {
    btn.addEventListener("click", () => {
        document.querySelectorAll("[data-diff]").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        currentDiff = btn.dataset.diff;
        initGame();
    });
});

document.getElementById("mode-reveal").addEventListener("click", () => {
    flagMode = false;
    document.getElementById("mode-reveal").classList.add("active");
    document.getElementById("mode-flag").classList.remove("active");
});

document.getElementById("mode-flag").addEventListener("click", () => {
    flagMode = true;
    document.getElementById("mode-flag").classList.add("active");
    document.getElementById("mode-reveal").classList.remove("active");
});

document.getElementById("reset-btn").addEventListener("click", initGame);

initGame();
</script>
</body>
</html>
"""


@games_bp.route("/games")
def games_home():
    player = _get_current_player()
    return render_template_string(GAMES_HTML, player=player)


@games_bp.route("/games/minesweeper/leaderboard")
def minesweeper_leaderboard():
    diff = request.args.get("difficulty", "easy")
    return jsonify(_get_leaderboard(diff))


@games_bp.route("/games/minesweeper/score", methods=["POST"])
def minesweeper_score():
    from datetime import datetime

    data = request.get_json(silent=True) or {}
    difficulty = str(data.get("difficulty", "easy"))
    if difficulty not in ("easy", "medium", "hard"):
        return jsonify({"error": "Neplatná obtiažnosť."}), 400

    try:
        seconds = int(data.get("seconds", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Neplatný čas."}), 400

    if seconds < 1 or seconds > 3600:
        return jsonify({"error": "Čas mimo povoleného rozsahu."}), 400

    player = _get_current_player()
    created_at = datetime.now().strftime("%d.%m. %H:%M")

    with _get_games_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO minesweeper_scores (difficulty, seconds, visitor_id, nickname, created_at)
                VALUES (%s, %s, %s, %s, %s)
            """, (difficulty, seconds, player["visitor_id"], player["nickname"], created_at))
        conn.commit()

    return jsonify({"success": True})
