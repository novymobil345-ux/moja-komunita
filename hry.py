from flask import Blueprint, request, render_template_string, jsonify, session
from datetime import datetime
import os
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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sudoku_scores (
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


def _get_leaderboard(game="minesweeper", difficulty="easy"):
    if difficulty not in ("easy", "medium", "hard"):
        difficulty = "easy"

    table = "sudoku_scores" if game == "sudoku" else "minesweeper_scores"

    with _get_games_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT nickname, seconds, created_at
                FROM {table}
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
<title>Minihry - Moja komunita</title>
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

.game-switcher { display: flex; gap: 10px; margin-bottom: 18px; }
.game-switch-btn {
    flex: 1; padding: 12px 14px; border-radius: 12px; border: 1px solid #344761;
    background: #152133; color: #b6c5d8; font-size: 15px; font-weight: bold; cursor: pointer;
}
.game-switch-btn.active { background: #2689e8; border-color: #2689e8; color: white; }

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

/* MÍNY */
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

/* SUDOKU */
.sudoku-board {
    display: grid; grid-template-columns: repeat(9, 1fr);
    max-width: 396px; width: 100%; margin: 0 auto 14px auto;
    background: #101827; border: 2px solid #6c8bb5; border-radius: 10px;
    overflow: hidden; user-select: none;
}
.s-cell {
    aspect-ratio: 1 / 1; background: #152133; color: #e6eef8;
    border: 1px solid #2b3e58; display: flex; align-items: center;
    justify-content: center; font-size: 19px; font-weight: bold;
    cursor: pointer; padding: 0; position: relative;
}
.s-cell.border-right-3 { border-right: 2px solid #6c8bb5; }
.s-cell.border-bottom-3 { border-bottom: 2px solid #6c8bb5; }
.s-cell.given { color: #9ecaff; background: #19283e; }
.s-cell.related { background: #1c2f49; }
.s-cell.same-num { background: #223b5c; }
.s-cell.selected { background: #2689e8; color: white; }
.s-cell.conflict { background: rgba(217, 54, 80, 0.35); color: #ff99aa; }
.s-notes {
    display: grid; grid-template-columns: repeat(3, 1fr);
    width: 100%; height: 100%; padding: 2px; font-size: 9px;
    line-height: 1.05; color: #9aabc0; font-weight: normal;
}
.s-notes span { display: flex; align-items: center; justify-content: center; }

.numpad {
    display: grid; grid-template-columns: repeat(5, 1fr);
    gap: 8px; max-width: 396px; margin: 0 auto 12px auto;
}
.num-btn {
    padding: 11px 6px; border-radius: 9px; border: 1px solid #3d5678;
    background: #293c55; color: white; font-size: 16px; font-weight: bold; cursor: pointer;
}
.num-btn:hover { background: #354e70; }
.num-btn.action-btn { background: #1e334d; color: #9ecaff; font-size: 13px; }
.check-btn {
    display: block; width: 100%; max-width: 396px; margin: 0 auto;
    padding: 12px; border-radius: 10px; border: none;
    background: #2689e8; color: white; font-size: 15px; font-weight: bold; cursor: pointer;
}
.check-btn:hover { background: #1472cb; }

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

<div class="game-switcher">
    <button type="button" class="game-switch-btn active" data-game="minesweeper">💣 Míny</button>
    <button type="button" class="game-switch-btn" data-game="sudoku">🔢 Sudoku</button>
</div>

<!-- ========================================== -->
<!-- SEKCIa MINY                                -->
<!-- ========================================== -->
<div id="section-minesweeper">
    <div class="panel">
        <h2 style="margin-top: 0;">💣 Míny</h2>

        <div class="controls-row">
            <div class="diff-group">
                <button type="button" class="small-btn active" data-ms-diff="easy">Ľahká (8×8)</button>
                <button type="button" class="small-btn" data-ms-diff="medium">Stredná (10×10)</button>
                <button type="button" class="small-btn" data-ms-diff="hard">Ťažká (12×12)</button>
            </div>
            <button type="button" class="reset-btn" id="ms-reset-btn">↻ Nová hra</button>
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
            <div>⏱️ Čas: <span id="ms-timer">0</span> s</div>
        </div>

        <div class="status-text" id="ms-status">Klikni na políčko a začni.</div>

        <div class="board-wrap">
            <div class="board" id="ms-board"></div>
        </div>
    </div>

    <div class="panel">
        <h3 style="margin-top: 0;">🏆 Najlepšie časy – Míny (<span id="ms-lb-label">Ľahká</span>)</h3>
        <div id="ms-leaderboard"></div>
    </div>
</div>

<!-- ========================================== -->
<!-- SEKCIa SUDOKU                               -->
<!-- ========================================== -->
<div id="section-sudoku" style="display: none;">
    <div class="panel">
        <h2 style="margin-top: 0;">🔢 Sudoku</h2>

        <div class="controls-row">
            <div class="diff-group">
                <button type="button" class="small-btn active" data-sd-diff="easy">Ľahká</button>
                <button type="button" class="small-btn" data-sd-diff="medium">Stredná</button>
                <button type="button" class="small-btn" data-sd-diff="hard">Ťažká</button>
            </div>
            <button type="button" class="reset-btn" id="sd-reset-btn">↻ Nové Sudoku</button>
        </div>

        <div class="controls-row">
            <div class="mode-group">
                <button type="button" class="small-btn active" id="sd-mode-value">✏️ Číslo</button>
                <button type="button" class="small-btn" id="sd-mode-notes">📝 Poznámky</button>
            </div>
            <span class="subtitle" style="font-size: 12px;">Klávesnica: 1–9, Backspace/Delete</span>
        </div>

        <div class="hud-bar">
            <div>🔢 Vyplnené: <span id="sd-filled">0</span> / 81</div>
            <div>⏱️ Čas: <span id="sd-timer">0</span> s</div>
        </div>

        <div class="status-text" id="sd-status">Vyber políčko a doplň čísla 1–9.</div>

        <div class="sudoku-board" id="sd-board"></div>

        <div class="numpad" id="sd-numpad">
            <button type="button" class="num-btn" data-num="1">1</button>
            <button type="button" class="num-btn" data-num="2">2</button>
            <button type="button" class="num-btn" data-num="3">3</button>
            <button type="button" class="num-btn" data-num="4">4</button>
            <button type="button" class="num-btn" data-num="5">5</button>
            <button type="button" class="num-btn" data-num="6">6</button>
            <button type="button" class="num-btn" data-num="7">7</button>
            <button type="button" class="num-btn" data-num="8">8</button>
            <button type="button" class="num-btn" data-num="9">9</button>
            <button type="button" class="num-btn action-btn" id="sd-erase-btn">⌫ Zmazať</button>
        </div>

        <button type="button" class="check-btn" id="sd-check-btn">✓ Skontrolovať riešenie</button>
    </div>

    <div class="panel">
        <h3 style="margin-top: 0;">🏆 Najlepšie časy – Sudoku (<span id="sd-lb-label">Ľahká</span>)</h3>
        <div id="sd-leaderboard"></div>
    </div>
</div>
</div>

<script>
const DIFF_LABELS = { easy: "Ľahká", medium: "Stredná", hard: "Ťažká" };
let activeGame = "minesweeper";

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

async function renderLeaderboard(targetEl, game, diff) {
    try {
        const res = await fetch(`/games/${game}/leaderboard?difficulty=${diff}`, { cache: "no-store" });
        if (!res.ok) return;
        const rows = await res.json();
        if (!rows.length) {
            targetEl.innerHTML = '<div class="lb-empty">Zatiaľ žiadne zapísané časy. Buď prvý!</div>';
            return;
        }
        targetEl.innerHTML = `
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

async function postScore(game, diff, sec) {
    try {
        return await fetch(`/games/${game}/score`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ difficulty: diff, seconds: sec })
        });
    } catch (e) {
        return null;
    }
}

/* ========================================== */
/* LOGIKA HRY MINY                            */
/* ========================================== */
const MS_CONFIGS = {
    easy:   { rows: 8,  cols: 8,  mines: 10 },
    medium: { rows: 10, cols: 10, mines: 16 },
    hard:   { rows: 12, cols: 12, mines: 25 }
};

let msDiff = "easy";
let msFlagMode = false;
let msGrid = [];
let msOver = false;
let msWon = false;
let msFirstClick = true;
let msSeconds = 0;
let msTimerId = null;

const msBoardEl = document.getElementById("ms-board");
const minesLeftEl = document.getElementById("mines-left");
const msTimerEl = document.getElementById("ms-timer");
const msStatusEl = document.getElementById("ms-status");
const msLbEl = document.getElementById("ms-leaderboard");
const msLbLabel = document.getElementById("ms-lb-label");

function stopMsTimer() {
    if (msTimerId) { clearInterval(msTimerId); msTimerId = null; }
}

function startMsTimer() {
    if (msTimerId) return;
    msTimerId = setInterval(() => {
        msSeconds += 1;
        msTimerEl.textContent = msSeconds;
    }, 1000);
}

function getMsNeighbors(rows, cols, r, c) {
    const out = [];
    for (let dr = -1; dr <= 1; dr++) {
        for (let dc = -1; dc <= 1; dc++) {
            if (dr === 0 && dc === 0) continue;
            const nr = r + dr, nc = c + dc;
            if (nr >= 0 && nr < rows && nc >= 0 && nc < cols) out.push([nr, nc]);
        }
    }
    return out;
}

function generateMsLayout(rows, cols, minesCount, safeR, safeC) {
    const mineMap = Array.from({ length: rows }, () => Array(cols).fill(false));
    let placed = 0;
    while (placed < minesCount) {
        const r = Math.floor(Math.random() * rows);
        const c = Math.floor(Math.random() * cols);
        if (mineMap[r][c]) continue;
        if (Math.abs(r - safeR) <= 1 && Math.abs(c - safeC) <= 1) continue;
        mineMap[r][c] = true;
        placed += 1;
    }

    const adjMap = Array.from({ length: rows }, () => Array(cols).fill(0));
    for (let r = 0; r < rows; r++) {
        for (let c = 0; c < cols; c++) {
            if (mineMap[r][c]) continue;
            let count = 0;
            for (const [nr, nc] of getMsNeighbors(rows, cols, r, c)) {
                if (mineMap[nr][nc]) count += 1;
            }
            adjMap[r][c] = count;
        }
    }
    return { mineMap, adjMap };
}

function isMsLogicallySolvable(rows, cols, mineMap, adjMap, startR, startC) {
    const revealed = Array.from({ length: rows }, () => Array(cols).fill(false));
    const markedMines = Array.from({ length: rows }, () => Array(cols).fill(false));

    function floodReveal(sr, sc) {
        if (revealed[sr][sc] || markedMines[sr][sc] || mineMap[sr][sc]) return;
        const stack = [[sr, sc]];
        while (stack.length > 0) {
            const [cr, cc] = stack.pop();
            if (revealed[cr][cc] || markedMines[cr][cc] || mineMap[cr][cc]) continue;
            revealed[cr][cc] = true;
            if (adjMap[cr][cc] === 0) {
                for (const [nr, nc] of getMsNeighbors(rows, cols, cr, cc)) {
                    if (!revealed[nr][nc] && !markedMines[nr][nc] && !mineMap[nr][nc]) {
                        stack.push([nr, nc]);
                    }
                }
            }
        }
    }

    floodReveal(startR, startC);

    while (true) {
        let progress = false;
        for (let r = 0; r < rows; r++) {
            for (let c = 0; c < cols; c++) {
                if (!revealed[r][c] || adjMap[r][c] === 0) continue;
                const hidden = [];
                let flaggedCount = 0;
                for (const [nr, nc] of getMsNeighbors(rows, cols, r, c)) {
                    if (markedMines[nr][nc]) flaggedCount += 1;
                    else if (!revealed[nr][nc]) hidden.push([nr, nc]);
                }
                if (hidden.length === 0) continue;
                if (flaggedCount === adjMap[r][c]) {
                    for (const [hr, hc] of hidden) {
                        if (!mineMap[hr][hc] && !revealed[hr][hc]) {
                            floodReveal(hr, hc);
                            progress = true;
                        }
                    }
                } else if (flaggedCount + hidden.length === adjMap[r][c]) {
                    for (const [hr, hc] of hidden) {
                        if (!markedMines[hr][hc]) {
                            markedMines[hr][hc] = true;
                            progress = true;
                        }
                    }
                }
            }
        }
        if (!progress) break;
    }

    for (let r = 0; r < rows; r++) {
        for (let c = 0; c < cols; c++) {
            if (!mineMap[r][c] && !revealed[r][c]) return false;
        }
    }
    return true;
}

function placeMsMines(safeR, safeC) {
    const cfg = MS_CONFIGS[msDiff];
    let chosen = null;
    for (let i = 0; i < 300; i++) {
        const candidate = generateMsLayout(cfg.rows, cfg.cols, cfg.mines, safeR, safeC);
        chosen = candidate;
        if (isMsLogicallySolvable(cfg.rows, cfg.cols, candidate.mineMap, candidate.adjMap, safeR, safeC)) {
            break;
        }
    }
    for (let r = 0; r < cfg.rows; r++) {
        for (let c = 0; c < cfg.cols; c++) {
            msGrid[r][c].mine = chosen.mineMap[r][c];
            msGrid[r][c].adj = chosen.adjMap[r][c];
        }
    }
}

function updateMsCounter() {
    let flags = 0;
    for (const row of msGrid) {
        for (const cell of row) {
            if (cell.flagged) flags += 1;
        }
    }
    minesLeftEl.textContent = MS_CONFIGS[msDiff].mines - flags;
}

function toggleMsFlag(r, c) {
    if (msOver || msWon) return;
    const cell = msGrid[r][c];
    if (cell.revealed) return;
    cell.flagged = !cell.flagged;
    updateMsCounter();
    renderMsBoard();
}

function revealMsCell(r, c) {
    if (msOver || msWon) return;
    const cell = msGrid[r][c];
    if (cell.revealed || cell.flagged) return;

    if (msFirstClick) {
        msFirstClick = false;
        placeMsMines(r, c);
        startMsTimer();
    }

    if (cell.mine) {
        cell.revealed = true;
        cell.hit = true;
        msOver = true;
        stopMsTimer();
        for (const row of msGrid) {
            for (const item of row) {
                if (item.mine) item.revealed = true;
            }
        }
        msStatusEl.textContent = "💥 Narazil si na mínu! Skús to znova.";
        renderMsBoard();
        return;
    }

    const stack = [[r, c]];
    while (stack.length > 0) {
        const [cr, cc] = stack.pop();
        const cur = msGrid[cr][cc];
        if (cur.revealed || cur.flagged) continue;
        cur.revealed = true;
        if (cur.adj === 0) {
            for (const [nr, nc] of getMsNeighbors(msGrid.length, msGrid[0].length, cr, cc)) {
                if (!msGrid[nr][nc].revealed && !msGrid[nr][nc].mine) stack.push([nr, nc]);
            }
        }
    }

    for (const row of msGrid) {
        for (const item of row) {
            if (!item.mine && !item.revealed) {
                renderMsBoard();
                return;
            }
        }
    }

    msWon = true;
    stopMsTimer();
    msStatusEl.textContent = `🎉 Vyhral si za ${msSeconds} s! Ukladám skóre...`;
    renderMsBoard();
    postScore("minesweeper", msDiff, msSeconds).then(res => {
        if (res && res.ok) {
            msStatusEl.textContent = `🎉 Vyhral si za ${msSeconds} s! Rekord zapísaný.`;
            renderLeaderboard(msLbEl, "minesweeper", msDiff);
        }
    });
}

function renderMsBoard() {
    msBoardEl.innerHTML = "";
    for (let r = 0; r < msGrid.length; r++) {
        for (let c = 0; c < msGrid[0].length; c++) {
            const cell = msGrid[r][c];
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
            btn.addEventListener("click", () => msFlagMode ? toggleMsFlag(r, c) : revealMsCell(r, c));
            btn.addEventListener("contextmenu", (e) => { e.preventDefault(); toggleMsFlag(r, c); });
            msBoardEl.appendChild(btn);
        }
    }
}

function initMinesweeper() {
    stopMsTimer();
    const cfg = MS_CONFIGS[msDiff];
    msOver = false;
    msWon = false;
    msFirstClick = true;
    msSeconds = 0;
    msTimerEl.textContent = "0";
    minesLeftEl.textContent = cfg.mines;
    msStatusEl.textContent = "Klikni na políčko a začni.";
    msLbLabel.textContent = DIFF_LABELS[msDiff];
    msGrid = Array.from({ length: cfg.rows }, (_, r) =>
        Array.from({ length: cfg.cols }, (_, c) => ({
            r, c, mine: false, revealed: false, flagged: false, adj: 0
        }))
    );
    msBoardEl.style.gridTemplateColumns = `repeat(${cfg.cols}, 34px)`;
    renderMsBoard();
    renderLeaderboard(msLbEl, "minesweeper", msDiff);
}

/* ========================================== */
/* LOGIKA HRY SUDOKU                          */
/* ========================================== */
const SD_CLUES = { easy: 42, medium: 34, hard: 27 };
let sdDiff = "easy";
let sdNotesMode = false;
let sdSolution = [];
let sdGiven = [];
let sdValues = [];
let sdNotes = [];
let sdSelected = [0, 0];
let sdSeconds = 0;
let sdTimerId = null;
let sdCompleted = false;

const sdBoardEl = document.getElementById("sd-board");
const sdFilledEl = document.getElementById("sd-filled");
const sdTimerEl = document.getElementById("sd-timer");
const sdStatusEl = document.getElementById("sd-status");
const sdLbEl = document.getElementById("sd-leaderboard");
const sdLbLabel = document.getElementById("sd-lb-label");

function stopSdTimer() {
    if (sdTimerId) { clearInterval(sdTimerId); sdTimerId = null; }
}

function startSdTimer() {
    if (sdTimerId) return;
    sdTimerId = setInterval(() => {
        sdSeconds += 1;
        sdTimerEl.textContent = sdSeconds;
    }, 1000);
}

function shuffleArray(arr) {
    const copy = arr.slice();
    for (let i = copy.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [copy[i], copy[j]] = [copy[j], copy[i]];
    }
    return copy;
}

function isValidSudokuPlacement(grid, r, c, num) {
    for (let i = 0; i < 9; i++) {
        if (grid[r][i] === num || grid[i][c] === num) return false;
    }
    const br = Math.floor(r / 3) * 3;
    const bc = Math.floor(c / 3) * 3;
    for (let dr = 0; dr < 3; dr++) {
        for (let dc = 0; dc < 3; dc++) {
            if (grid[br + dr][bc + dc] === num) return false;
        }
    }
    return true;
}

function generateFullSudoku() {
    const grid = Array.from({ length: 9 }, () => Array(9).fill(0));

    function solve(pos) {
        if (pos === 81) return true;
        const r = Math.floor(pos / 9);
        const c = pos % 9;
        const nums = shuffleArray([1, 2, 3, 4, 5, 6, 7, 8, 9]);
        for (const num of nums) {
            if (isValidSudokuPlacement(grid, r, c, num)) {
                grid[r][c] = num;
                if (solve(pos + 1)) return true;
                grid[r][c] = 0;
            }
        }
        return false;
    }

    solve(0);
    return grid;
}

function initSudoku() {
    stopSdTimer();
    sdCompleted = false;
    sdSeconds = 0;
    sdTimerEl.textContent = "0";
    sdStatusEl.textContent = "Vyber políčko a doplň čísla 1–9.";
    sdLbLabel.textContent = DIFF_LABELS[sdDiff];

    sdSolution = generateFullSudoku();
    sdValues = sdSolution.map(row => row.slice());
    sdGiven = Array.from({ length: 9 }, () => Array(9).fill(true));
    sdNotes = Array.from({ length: 9 }, () => Array.from({ length: 9 }, () => []));

    const targetClues = SD_CLUES[sdDiff];
    let cellsToRemove = 81 - targetClues;
    const positions = shuffleArray(Array.from({ length: 81 }, (_, i) => i));

    for (const pos of positions) {
        if (cellsToRemove <= 0) break;
        const r = Math.floor(pos / 9);
        const c = pos % 9;
        sdValues[r][c] = 0;
        sdGiven[r][c] = false;
        cellsToRemove -= 1;
    }

    sdSelected = [0, 0];
    renderSudoku();
    renderLeaderboard(sdLbEl, "sudoku", sdDiff);
    startSdTimer();
}

function hasSdConflict(r, c, val) {
    if (!val) return false;
    for (let i = 0; i < 9; i++) {
        if (i !== c && sdValues[r][i] === val) return true;
        if (i !== r && sdValues[i][c] === val) return true;
    }
    const br = Math.floor(r / 3) * 3;
    const bc = Math.floor(c / 3) * 3;
    for (let dr = 0; dr < 3; dr++) {
        for (let dc = 0; dc < 3; dc++) {
            const nr = br + dr, nc = bc + dc;
            if ((nr !== r || nc !== c) && sdValues[nr][nc] === val) return true;
        }
    }
    return false;
}

function applySdNumber(num) {
    if (sdCompleted) return;
    const [r, c] = sdSelected;
    if (sdGiven[r][c]) return;

    if (sdNotesMode) {
        sdValues[r][c] = 0;
        const list = sdNotes[r][c];
        const idx = list.indexOf(num);
        if (idx >= 0) list.splice(idx, 1);
        else { list.push(num); list.sort((a, b) => a - b); }
    } else {
        sdNotes[r][c] = [];
        sdValues[r][c] = (sdValues[r][c] === num) ? 0 : num;
    }

    renderSudoku();
}

function eraseSdCell() {
    if (sdCompleted) return;
    const [r, c] = sdSelected;
    if (sdGiven[r][c]) return;
    sdValues[r][c] = 0;
    sdNotes[r][c] = [];
    renderSudoku();
}

function checkSudokuSolution() {
    if (sdCompleted) return;
    let emptyCount = 0;
    let wrongCount = 0;

    for (let r = 0; r < 9; r++) {
        for (let c = 0; c < 9; c++) {
            if (sdValues[r][c] === 0) emptyCount += 1;
            else if (sdValues[r][c] !== sdSolution[r][c]) wrongCount += 1;
        }
    }

    if (emptyCount > 0) {
        sdStatusEl.textContent = `Zostáva ešte ${emptyCount} nevyplnených políčok (chybné: ${wrongCount}).`;
        return;
    }

    if (wrongCount > 0) {
        sdStatusEl.textContent = `Našlo sa ${wrongCount} nesprávnych čísel. Skús ich opraviť.`;
        renderSudoku();
        return;
    }

    sdCompleted = true;
    stopSdTimer();
    sdStatusEl.textContent = `🎉 Sudoku vyriešené za ${sdSeconds} s! Ukladám skóre...`;
    renderSudoku();
    postScore("sudoku", sdDiff, sdSeconds).then(res => {
        if (res && res.ok) {
            sdStatusEl.textContent = `🎉 Sudoku vyriešené za ${sdSeconds} s! Rekord zapísaný.`;
            renderLeaderboard(sdLbEl, "sudoku", sdDiff);
        }
    });
}

function renderSudoku() {
    sdBoardEl.innerHTML = "";
    const [selR, selC] = sdSelected;
    const selVal = sdValues[selR][selC];
    let filled = 0;

    for (let r = 0; r < 9; r++) {
        for (let c = 0; c < 9; c++) {
            const val = sdValues[r][c];
            if (val !== 0) filled += 1;

            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "s-cell";
            if (c === 2 || c === 5) btn.classList.add("border-right-3");
            if (r === 2 || r === 5) btn.classList.add("border-bottom-3");
            if (sdGiven[r][c]) btn.classList.add("given");

            const sameBox = Math.floor(r / 3) === Math.floor(selR / 3) && Math.floor(c / 3) === Math.floor(selC / 3);
            if (r === selR || c === selC || sameBox) btn.classList.add("related");
            if (selVal !== 0 && val === selVal) btn.classList.add("same-num");
            if (r === selR && c === selC) btn.classList.add("selected");
            if (hasSdConflict(r, c, val)) btn.classList.add("conflict");

            if (val !== 0) {
                btn.textContent = val;
            } else if (sdNotes[r][c].length > 0) {
                const notesDiv = document.createElement("div");
                notesDiv.className = "s-notes";
                for (let n = 1; n <= 9; n++) {
                    const sp = document.createElement("span");
                    sp.textContent = sdNotes[r][c].includes(n) ? n : "";
                    notesDiv.appendChild(sp);
                }
                btn.appendChild(notesDiv);
            }

            btn.addEventListener("click", () => {
                sdSelected = [r, c];
                renderSudoku();
            });

            sdBoardEl.appendChild(btn);
        }
    }

    sdFilledEl.textContent = filled;
}

/* ========================================== */
/* PREPÍNANIE HIER A OVLÁDACIE PRVKY          */
/* ========================================== */
document.querySelectorAll(".game-switch-btn").forEach(btn => {
    btn.addEventListener("click", () => {
        activeGame = btn.dataset.game;
        document.querySelectorAll(".game-switch-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        document.getElementById("section-minesweeper").style.display = activeGame === "minesweeper" ? "block" : "none";
        document.getElementById("section-sudoku").style.display = activeGame === "sudoku" ? "block" : "none";
    });
});

document.querySelectorAll("[data-ms-diff]").forEach(btn => {
    btn.addEventListener("click", () => {
        document.querySelectorAll("[data-ms-diff]").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        msDiff = btn.dataset.msDiff;
        initMinesweeper();
    });
});

document.getElementById("mode-reveal").addEventListener("click", () => {
    msFlagMode = false;
    document.getElementById("mode-reveal").classList.add("active");
    document.getElementById("mode-flag").classList.remove("active");
});

document.getElementById("mode-flag").addEventListener("click", () => {
    msFlagMode = true;
    document.getElementById("mode-flag").classList.add("active");
    document.getElementById("mode-reveal").classList.remove("active");
});

document.getElementById("ms-reset-btn").addEventListener("click", initMinesweeper);

document.querySelectorAll("[data-sd-diff]").forEach(btn => {
    btn.addEventListener("click", () => {
        document.querySelectorAll("[data-sd-diff]").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        sdDiff = btn.dataset.sdDiff;
        initSudoku();
    });
});

document.getElementById("sd-mode-value").addEventListener("click", () => {
    sdNotesMode = false;
    document.getElementById("sd-mode-value").classList.add("active");
    document.getElementById("sd-mode-notes").classList.remove("active");
});

document.getElementById("sd-mode-notes").addEventListener("click", () => {
    sdNotesMode = true;
    document.getElementById("sd-mode-notes").classList.add("active");
    document.getElementById("sd-mode-value").classList.remove("active");
});

document.querySelectorAll("#sd-numpad [data-num]").forEach(btn => {
    btn.addEventListener("click", () => applySdNumber(Number(btn.dataset.num)));
});

document.getElementById("sd-erase-btn").addEventListener("click", eraseSdCell);
document.getElementById("sd-reset-btn").addEventListener("click", initSudoku);
document.getElementById("sd-check-btn").addEventListener("click", checkSudokuSolution);

document.addEventListener("keydown", (e) => {
    if (activeGame !== "sudoku") return;
    if (["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) return;

    if (e.key >= "1" && e.key <= "9") {
        applySdNumber(Number(e.key));
    } else if (e.key === "Backspace" || e.key === "Delete" || e.key === "0") {
        eraseSdCell();
    } else if (e.key === "ArrowUp") {
        e.preventDefault();
        sdSelected[0] = (sdSelected[0] + 8) % 9;
        renderSudoku();
    } else if (e.key === "ArrowDown") {
        e.preventDefault();
        sdSelected[0] = (sdSelected[0] + 1) % 9;
        renderSudoku();
    } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        sdSelected[1] = (sdSelected[1] + 8) % 9;
        renderSudoku();
    } else if (e.key === "ArrowRight") {
        e.preventDefault();
        sdSelected[1] = (sdSelected[1] + 1) % 9;
        renderSudoku();
    }
});

initMinesweeper();
initSudoku();
</script>
</body>
</html>
"""


@games_bp.route("/games")
def games_home():
    player = _get_current_player()
    return render_template_string(GAMES_HTML, player=player)


@games_bp.route("/games/<game>/leaderboard")
def game_leaderboard(game):
    if game not in ("minesweeper", "sudoku"):
        return jsonify({"error": "Neplatná hra."}), 404
    diff = request.args.get("difficulty", "easy")
    return jsonify(_get_leaderboard(game, diff))


@games_bp.route("/games/<game>/score", methods=["POST"])
def game_score(game):
    if game not in ("minesweeper", "sudoku"):
        return jsonify({"error": "Neplatná hra."}), 404

    data = request.get_json(silent=True) or {}
    difficulty = str(data.get("difficulty", "easy"))
    if difficulty not in ("easy", "medium", "hard"):
        return jsonify({"error": "Neplatná obtiažnosť."}), 400

    try:
        seconds = int(data.get("seconds", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Neplatný čas."}), 400

    max_seconds = 7200 if game == "sudoku" else 3600
    if seconds < 1 or seconds > max_seconds:
        return jsonify({"error": "Čas mimo povoleného rozsahu."}), 400

    table = "sudoku_scores" if game == "sudoku" else "minesweeper_scores"
    player = _get_current_player()
    created_at = datetime.now().strftime("%d.%m. %H:%M")

    with _get_games_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {table} (difficulty, seconds, visitor_id, nickname, created_at)
                VALUES (%s, %s, %s, %s, %s)
            """, (difficulty, seconds, player["visitor_id"], player["nickname"], created_at))
        conn.commit()

    return jsonify({"success": True})
