#!/usr/bin/env python3
"""
CHEB VPN — HITL Agent Orchestrator
====================================
Автономный агент с Human-in-the-Loop через Telegram.

Архитектура:
  1. Читает задачи из SQLite (/root/workspace_tasks.db)
  2. Для active/high задач — анализирует через Claude API
  3. Отправляет предложение в Telegram с кнопками Approve / Reject
  4. После Approve — выполняет через Claude API
  5. Отчёт → Telegram + обновляет статус задачи

Запуск: python3 /root/hitl_agent/orchestrator.py
Cron:   0 9 * * * python3 /root/hitl_agent/orchestrator.py --daily-report
"""

import os
import sys
import json
import sqlite3
import asyncio
import logging
import argparse
import subprocess
from datetime import datetime
from typing import Optional

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [hitl] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.getenv("BOT_TOKEN",       "8701913962:AAHRUYDARLlWkZeHhqmRoNoTJVPeXXpErP0")
ADMIN_ID       = int(os.getenv("ADMIN_IDS",   "859020176"))
WS_TOKEN       = os.getenv("TERMINAL_TOKEN",  "XzP0S-alQur5ex_wXyUGpQhKrwE82-bJCJpFNhVEw70")
TASKS_DB       = "/root/workspace_tasks.db"
PROPOSALS_DB   = "/root/hitl_agent/proposals.db"
WS_API         = "http://127.0.0.1:8000/api/workspace"
TG_API         = f"https://api.telegram.org/bot{BOT_TOKEN}"
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "")

# ── Proposals DB ──────────────────────────────────────────────────────────────
def init_proposals_db():
    os.makedirs(os.path.dirname(PROPOSALS_DB), exist_ok=True)
    conn = sqlite3.connect(PROPOSALS_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS proposals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id     TEXT NOT NULL,
            task_label  TEXT NOT NULL,
            proposal    TEXT NOT NULL,
            tg_message_id INTEGER,
            status      TEXT DEFAULT 'pending',  -- pending | approved | rejected | done | failed
            result      TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn

def get_proposals_conn():
    return sqlite3.connect(PROPOSALS_DB)

# ── Workspace API ─────────────────────────────────────────────────────────────
async def ws_get_tasks() -> list:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{WS_API}/tasks", headers={"X-Workspace-Token": WS_TOKEN}, timeout=10)
        return r.json() if r.status_code == 200 else []

async def ws_update_task(task_id: str, **fields):
    async with httpx.AsyncClient() as c:
        await c.patch(
            f"{WS_API}/tasks/{task_id}",
            json=fields,
            headers={"X-Workspace-Token": WS_TOKEN},
            timeout=10,
        )

async def ws_add_history(task_id: str, entry: str):
    async with httpx.AsyncClient() as c:
        await c.post(
            f"{WS_API}/tasks/{task_id}/history",
            json={"entry": entry},
            headers={"X-Workspace-Token": WS_TOKEN},
            timeout=10,
        )

async def ws_health() -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{WS_API}/health", headers={"X-Workspace-Token": WS_TOKEN}, timeout=10)
        return r.json() if r.status_code == 200 else {}

# ── Telegram ──────────────────────────────────────────────────────────────────
async def tg_send(text: str, reply_markup=None, parse_mode="HTML") -> Optional[int]:
    payload = {"chat_id": ADMIN_ID, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{TG_API}/sendMessage", json=payload, timeout=15)
        data = r.json()
        if data.get("ok"):
            return data["result"]["message_id"]
        log.error(f"TG send failed: {data}")
        return None

async def tg_send_report(text: str):
    """Отправить длинный отчёт с разбивкой по 4096 символов."""
    for i in range(0, len(text), 4000):
        await tg_send(text[i:i+4000])
        await asyncio.sleep(0.3)

async def tg_answer_callback(callback_id: str, text: str = ""):
    async with httpx.AsyncClient() as c:
        await c.post(f"{TG_API}/answerCallbackQuery",
                     json={"callback_query_id": callback_id, "text": text}, timeout=5)

async def tg_edit_message(msg_id: int, text: str):
    async with httpx.AsyncClient() as c:
        await c.post(f"{TG_API}/editMessageText",
                     json={"chat_id": ADMIN_ID, "message_id": msg_id,
                           "text": text, "parse_mode": "HTML"}, timeout=5)

# ── Claude API (quick analysis) ───────────────────────────────────────────────
async def claude_analyze_task(task: dict) -> str:
    """Анализирует задачу и возвращает краткое предложение по реализации."""
    if not ANTHROPIC_KEY:
        return f"Предлагаю начать с: {task.get('description','нет описания')[:200]}"

    prompt = f"""Ты ассистент по разработке VPN-сервиса CHEB VPN.
Задача: {task.get('label','')}
Описание: {task.get('description','нет описания')}
Ветка: {task.get('branch','')}

Напиши краткий (3-5 предложений) план реализации:
1. Что конкретно нужно сделать (файлы/команды)
2. Какой результат ожидается
3. Возможные риски
Отвечай на русском, коротко и конкретно."""

    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "messages": [{"role": "user", "content": prompt}]
    }
    async with httpx.AsyncClient() as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
                         json=body, headers=headers, timeout=30)
        if r.status_code == 200:
            return r.json()["content"][0]["text"]
        return f"Анализ недоступен. Задача: {task.get('label','')}"

# ── HITL: создать предложение ─────────────────────────────────────────────────
async def propose_task(task: dict):
    """Отправляет предложение по задаче в Telegram и ждёт Approve/Reject."""
    conn = get_proposals_conn()
    # Проверяем не было ли уже предложения по этой задаче
    existing = conn.execute(
        "SELECT id FROM proposals WHERE task_id=? AND status='pending'", (task['id'],)
    ).fetchone()
    if existing:
        log.info(f"Proposal already pending for task {task['id']}")
        conn.close()
        return

    analysis = await claude_analyze_task(task)

    priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(task.get("priority",""), "⚪")
    text = (
        f"🤖 <b>Предложение агента</b>\n\n"
        f"{priority_emoji} <b>{task.get('label','')}</b>\n"
        f"<i>Ветка: {task.get('branch','')}</i>\n\n"
        f"📋 <b>Анализ:</b>\n{analysis}\n\n"
        f"Одобрить выполнение?"
    )

    now = datetime.utcnow().isoformat()
    cur = conn.execute(
        "INSERT INTO proposals (task_id,task_label,proposal,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        (task['id'], task.get('label',''), analysis, 'pending', now, now)
    )
    proposal_id = cur.lastrowid
    conn.commit()

    markup = {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"hitl_approve_{proposal_id}"},
        {"text": "❌ Reject",  "callback_data": f"hitl_reject_{proposal_id}"},
    ]]}

    msg_id = await tg_send(text, reply_markup=markup)
    if msg_id:
        conn.execute("UPDATE proposals SET tg_message_id=?,updated_at=? WHERE id=?",
                     (msg_id, datetime.utcnow().isoformat(), proposal_id))
        conn.commit()
    conn.close()
    log.info(f"Proposal #{proposal_id} sent for task {task.get('label','')}")

# ── HITL: обработка callback ──────────────────────────────────────────────────
async def handle_callback(callback_id: str, data: str, msg_id: int):
    """Обрабатывает нажатие Approve/Reject."""
    conn = get_proposals_conn()
    if data.startswith("hitl_approve_"):
        proposal_id = int(data.split("_")[-1])
        row = conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if not row:
            await tg_answer_callback(callback_id, "Предложение не найдено")
            conn.close()
            return

        conn.execute("UPDATE proposals SET status='approved',updated_at=? WHERE id=?",
                     (datetime.utcnow().isoformat(), proposal_id))
        conn.commit()
        await tg_answer_callback(callback_id, "✅ Одобрено! Запускаю...")
        await tg_edit_message(msg_id, f"✅ <b>Одобрено</b>\n\n{row[3][:500]}...\n\n⏳ Выполняется...")

        # Запускаем Claude Code с контекстом задачи
        task_id = row[1]
        task_label = row[2]
        await ws_update_task(task_id, status="active")
        await ws_add_history(task_id, f"[HITL] Одобрено {datetime.utcnow().strftime('%d.%m %H:%M')} — агент начал выполнение")

        await tg_send(
            f"🚀 <b>Задача запущена</b>\n"
            f"<code>{task_label}</code>\n\n"
            f"Открой терминал и запусти:\n"
            f"<code>claude</code>\n\n"
            f"Контекст задачи будет в /root/hitl_agent/current_task.json"
        )
        # Сохраняем текущую задачу для claude
        task_data = {"id": task_id, "label": task_label, "proposal": row[3]}
        with open("/root/hitl_agent/current_task.json", "w") as f:
            json.dump(task_data, f, ensure_ascii=False, indent=2)

    elif data.startswith("hitl_reject_"):
        proposal_id = int(data.split("_")[-1])
        conn.execute("UPDATE proposals SET status='rejected',updated_at=? WHERE id=?",
                     (datetime.utcnow().isoformat(), proposal_id))
        conn.commit()
        await tg_answer_callback(callback_id, "❌ Отклонено")
        await tg_edit_message(msg_id, f"❌ <b>Отклонено</b>")

    conn.close()

# ── Daily Report ──────────────────────────────────────────────────────────────
async def daily_report():
    """Ежедневная сводка: здоровье сервера + задачи + статистика."""
    tasks = await ws_get_tasks()
    health = await ws_health()

    urgent = [t for t in tasks if t.get("status") == "urgent"]
    active = [t for t in tasks if t.get("status") == "active"]
    todo_high = [t for t in tasks if t.get("status") == "todo" and t.get("priority") == "high"]
    done_total = len([t for t in tasks if t.get("status") == "done"])

    # Server health
    cpu = health.get("cpu_pct")
    ram = health.get("ram_pct")
    disk = health.get("disk_pct")
    server_ok = cpu and ram and cpu < 85 and ram < 85
    server_emoji = "🟢" if server_ok else "🔴"

    # Bot stats
    try:
        import sqlite3 as sl
        bot_conn = sl.connect("/root/vpn_bot/vpn_bot.db")
        total_users = bot_conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active_subs = bot_conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_active=1 AND subscription_end > datetime('now')"
        ).fetchone()[0]
        bot_conn.close()
        bot_stats = f"👥 Пользователей: <b>{total_users}</b> (активных: <b>{active_subs}</b>)"
    except Exception:
        bot_stats = "👥 Статистика бота недоступна"

    report = (
        f"📊 <b>Ежедневный отчёт CHEB VPN</b>\n"
        f"{datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
        f"{server_emoji} <b>Сервер:</b> CPU {cpu:.0f}% | RAM {ram:.0f}% | Диск {disk:.0f}%\n\n"
        f"{bot_stats}\n\n"
        f"📋 <b>Задачи:</b>\n"
        f"  🚨 Срочно: {len(urgent)}\n"
        f"  🔥 В работе: {len(active)}\n"
        f"  🔴 Высокий приоритет (очередь): {len(todo_high)}\n"
        f"  ✅ Завершено всего: {done_total}\n"
    )

    if urgent:
        report += f"\n⚠️ <b>Срочные задачи:</b>\n"
        for t in urgent[:3]:
            report += f"  • {t.get('label','')}\n"

    if active:
        report += f"\n🔥 <b>Сейчас в работе:</b>\n"
        for t in active[:3]:
            report += f"  • {t.get('label','')}\n"

    if todo_high:
        report += f"\n🔴 <b>Следующие на очереди (high):</b>\n"
        for t in todo_high[:3]:
            report += f"  • {t.get('label','')}\n"

    await tg_send(report)
    log.info("Daily report sent")

# ── Webhook polling (simple long-poll) ────────────────────────────────────────
async def poll_updates(offset: int = 0) -> tuple[list, int]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{TG_API}/getUpdates",
                        params={"offset": offset, "timeout": 30, "allowed_updates": ["callback_query"]},
                        timeout=40)
        data = r.json()
        updates = data.get("result", [])
        new_offset = (updates[-1]["update_id"] + 1) if updates else offset
        return updates, new_offset

async def run_listener():
    """Слушает Telegram callback-кнопки (Approve/Reject)."""
    log.info("HITL listener started — waiting for Approve/Reject...")
    offset = 0
    while True:
        try:
            updates, offset = await poll_updates(offset)
            for upd in updates:
                cb = upd.get("callback_query")
                if cb and cb.get("from", {}).get("id") == ADMIN_ID:
                    data = cb.get("data", "")
                    if data.startswith("hitl_"):
                        await handle_callback(
                            cb["id"], data, cb["message"]["message_id"]
                        )
        except Exception as e:
            log.error(f"Poll error: {e}")
            await asyncio.sleep(5)

# ── Propose pending high-priority tasks ───────────────────────────────────────
async def propose_pending():
    """Предлагает на одобрение todo/high задачи без активного предложения."""
    tasks = await ws_get_tasks()
    candidates = [
        t for t in tasks
        if t.get("status") == "todo" and t.get("priority") == "high"
    ]
    if not candidates:
        log.info("No high-priority tasks to propose")
        return
    # Предлагаем только первую задачу (чтобы не спамить)
    task = candidates[0]
    log.info(f"Proposing task: {task.get('label','')}")
    await propose_task(task)

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--daily-report", action="store_true", help="Отправить ежедневный отчёт")
    parser.add_argument("--propose",      action="store_true", help="Предложить задачу на одобрение")
    parser.add_argument("--listen",       action="store_true", help="Запустить слушатель Approve/Reject")
    args = parser.parse_args()

    init_proposals_db()

    if args.daily_report:
        await daily_report()
    elif args.propose:
        await propose_pending()
    elif args.listen:
        await run_listener()
    else:
        # По умолчанию: отчёт + предложение
        await daily_report()
        await asyncio.sleep(1)
        await propose_pending()

if __name__ == "__main__":
    asyncio.run(main())
