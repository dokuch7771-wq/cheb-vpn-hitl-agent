#!/usr/bin/env python3
"""
task_reviewer.py — проверяет активные задачи на «зависание»
Cron: каждые 6 часов

Логика:
  - active задача без обновления > STALE_HOURS → пингует в Telegram
  - urgent задача без обновления > URGENT_HOURS → алерт
  - inprogress > STALE_HOURS → напоминание
"""
import os, sqlite3, asyncio, json
from datetime import datetime, timedelta
import httpx

BOT_TOKEN   = os.getenv("BOT_TOKEN", "8701913962:AAHRUYDARLlWkZeHhqmRoNoTJVPeXXpErP0")
ADMIN_ID    = int(os.getenv("ADMIN_IDS", "859020176"))
TASKS_DB    = "/root/workspace_tasks.db"
PROPOSALS_DB= "/root/hitl_agent/proposals.db"
TG_API      = f"https://api.telegram.org/bot{BOT_TOKEN}"

STALE_HOURS  = 48   # active/inprogress без обновления → напоминание
URGENT_HOURS = 12   # urgent без обновления → алерт

def get_stale_tasks():
    conn = sqlite3.connect(TASKS_DB)
    conn.row_factory = sqlite3.Row
    now = datetime.utcnow()
    results = {"urgent": [], "stale": []}

    rows = conn.execute(
        "SELECT * FROM tasks WHERE status IN ('active','inprogress','urgent') ORDER BY priority, status"
    ).fetchall()

    for r in rows:
        updated = datetime.fromisoformat(r["updated_at"]) if r["updated_at"] else None
        if not updated:
            continue
        age_hours = (now - updated).total_seconds() / 3600

        if r["status"] == "urgent" and age_hours > URGENT_HOURS:
            results["urgent"].append({"task": dict(r), "age_hours": age_hours})
        elif r["status"] in ("active", "inprogress") and age_hours > STALE_HOURS:
            results["stale"].append({"task": dict(r), "age_hours": age_hours})

    conn.close()
    return results

def record_review(task_id: str, msg_id: int):
    """Сохраняем что отправили ревью, чтобы не спамить."""
    conn = sqlite3.connect(PROPOSALS_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            task_id TEXT PRIMARY KEY,
            tg_message_id INTEGER,
            sent_at TEXT NOT NULL
        )
    """)
    # Проверяем не отправляли ли недавно (последние 20ч)
    existing = conn.execute(
        "SELECT sent_at FROM reviews WHERE task_id=?", (task_id,)
    ).fetchone()
    if existing:
        sent = datetime.fromisoformat(existing[0])
        if (datetime.utcnow() - sent).total_seconds() < 72 * 3600:
            conn.close()
            return False  # уже отправляли, пропускаем
    conn.execute(
        "INSERT OR REPLACE INTO reviews (task_id, tg_message_id, sent_at) VALUES (?,?,?)",
        (task_id, msg_id, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()
    return True

async def tg_send(text: str, markup=None) -> int | None:
    payload = {"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML"}
    if markup:
        payload["reply_markup"] = json.dumps(markup)
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{TG_API}/sendMessage", json=payload, timeout=15)
        data = r.json()
        return data["result"]["message_id"] if data.get("ok") else None

async def main():
    stale = get_stale_tasks()

    # ── Срочные алерты ────────────────────────────────────────────
    for item in stale["urgent"]:
        t = item["task"]
        age_h = int(item["age_hours"])
        text = (
            f"🚨 <b>СРОЧНАЯ задача зависла!</b>\n\n"
            f"<b>{t['label']}</b>\n"
            f"<i>Ветка: {t['branch']}</i>\n"
            f"Без обновлений: <b>{age_h}ч</b>\n\n"
            f"{t.get('description','')[:200]}\n\n"
            f"Что делаем?"
        )
        markup = {"inline_keyboard": [[
            {"text": "✅ Готово",     "callback_data": f"rev_done_{t['id']}"},
            {"text": "🔄 Ещё в работе","callback_data": f"rev_keep_{t['id']}"},
            {"text": "⏸ Заморозить", "callback_data": f"rev_pause_{t['id']}"},
        ]]}
        msg_id = await tg_send(text, markup)
        if msg_id:
            record_review(t["id"], msg_id)

    # ── Зависшие active ───────────────────────────────────────────
    if stale["stale"]:
        lines = []
        for item in stale["stale"]:
            t = item["task"]
            age_h = int(item["age_hours"])
            prio = {"high":"🔴","medium":"🟡","low":"🟢"}.get(t.get("priority",""),"⚪")
            lines.append(f"{prio} <b>{t['label']}</b> — {age_h}ч без изменений")

        text = (
            f"⏰ <b>Задачи без активности {STALE_HOURS}+ часов:</b>\n\n"
            + "\n".join(lines) +
            f"\n\nОткрой workspace и обнови статусы:\n"
            f"<code>task list active</code>"
        )
        # Для зависших задач — общее сообщение без кнопок на каждую
        # (чтобы не спамить при большом количестве)
        first_stale_id = stale["stale"][0]["task"]["id"]

        conn = sqlite3.connect(PROPOSALS_DB)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                task_id TEXT PRIMARY KEY,
                tg_message_id INTEGER,
                sent_at TEXT NOT NULL
            )
        """)
        existing = conn.execute(
            "SELECT sent_at FROM reviews WHERE task_id=?", ("__stale_batch__",)
        ).fetchone()
        skip = False
        if existing:
            sent = datetime.fromisoformat(existing[0])
            if (datetime.utcnow() - sent).total_seconds() < 20 * 3600:
                skip = True
        conn.close()

        if not skip:
            msg_id = await tg_send(text)
            conn = sqlite3.connect(PROPOSALS_DB)
            conn.execute(
                "INSERT OR REPLACE INTO reviews (task_id, tg_message_id, sent_at) VALUES (?,?,?)",
                ("__stale_batch__", msg_id, datetime.utcnow().isoformat())
            )
            conn.commit()
            conn.close()

    if not stale["urgent"] and not stale["stale"]:
        print("✅ Все активные задачи свежие — напоминания не нужны")
    else:
        print(f"Отправлено алертов: urgent={len(stale['urgent'])}, stale={len(stale['stale'])}")

if __name__ == "__main__":
    asyncio.run(main())
