#!/usr/bin/env python3
"""
Проверяет пользователей с utm_source, у которых появился первый трафик в X-UI.
Cron: каждые 15 минут
"""
import os, sqlite3, asyncio, httpx, json
from datetime import datetime

OPS_BOT  = os.getenv("OPS_BOT_TOKEN", "8701913962:AAHRUYDARLlWkZeHhqmRoNoTJVPeXXpErP0")
ADMIN_ID = int(os.getenv("ADMIN_IDS",  "859020176"))
SITE_DB  = "/var/www/chebvpn-backend/chebvpn.db"
SEEN_DB  = "/root/hitl_agent/first_traffic_seen.db"

XUI_HOST = os.getenv("XUI_HOST", "127.0.0.1")
XUI_PORT = os.getenv("XUI_PORT", "2053")
XUI_PATH = os.getenv("XUI_PATH", "5e91e236460c42262347ec4c186a790b")
XUI_USER = os.getenv("XUI_USERNAME", "admin")
XUI_PASS = os.getenv("XUI_PASSWORD", "")

def init_seen():
    c = sqlite3.connect(SEEN_DB)
    c.execute("CREATE TABLE IF NOT EXISTS seen (user_id INTEGER PRIMARY KEY, notified_at TEXT)")
    c.commit()
    return c

def already_seen(conn, user_id):
    return conn.execute("SELECT 1 FROM seen WHERE user_id=?", (user_id,)).fetchone() is not None

def mark_seen(conn, user_id):
    conn.execute("INSERT OR IGNORE INTO seen (user_id, notified_at) VALUES (?,?)",
                 (user_id, datetime.utcnow().isoformat()))
    conn.commit()

async def get_xui_clients():
    """Получить список клиентов X-UI с их трафиком."""
    base = f"http://{XUI_HOST}:{XUI_PORT}/{XUI_PATH}"
    async with httpx.AsyncClient(verify=False, timeout=10) as c:
        # Login
        r = await c.post(f"{base}/login",
                         data={"username": XUI_USER, "password": XUI_PASS})
        if r.status_code != 200:
            return {}
        # Get inbounds
        r2 = await c.get(f"{base}/xui/inbound/list")
        data = r2.json()
        clients = {}
        for inbound in data.get("obj", []):
            for cl in json.loads(inbound.get("clientStats") or "[]"):
                clients[cl.get("email", "")] = {
                    "up": cl.get("up", 0),
                    "down": cl.get("down", 0),
                    "total": cl.get("up", 0) + cl.get("down", 0),
                }
        return clients

async def notify(text):
    async with httpx.AsyncClient() as c:
        await c.post(f"https://api.telegram.org/bot{OPS_BOT}/sendMessage",
                     json={"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML"}, timeout=10)

async def main():
    # Пользователи с utm_source у которых есть активная подписка
    site_conn = sqlite3.connect(SITE_DB)
    site_conn.row_factory = sqlite3.Row
    users = site_conn.execute("""
        SELECT u.id, u.email, u.utm_source, s.xui_client_uuid
        FROM users u
        JOIN subscriptions s ON s.user_id = u.id
        WHERE u.utm_source IS NOT NULL
          AND s.is_active = 1
          AND s.xui_client_uuid IS NOT NULL
    """).fetchall()
    site_conn.close()

    if not users:
        return

    try:
        xui_clients = await get_xui_clients()
    except Exception as e:
        print(f"X-UI error: {e}")
        return

    seen_conn = init_seen()

    for u in users:
        if already_seen(seen_conn, u["id"]):
            continue
        # Ищем по email (X-UI хранит email клиента)
        client = xui_clients.get(u["email"], {})
        if client.get("total", 0) > 0:
            mb = round(client["total"] / 1024 / 1024, 1)
            source_emoji = "🎯" if u["utm_source"] == "flyer_qr" else "📍"
            await notify(
                f"📡 <b>Первый трафик!</b>\n"
                f"📧 {u['email']}\n"
                f"{source_emoji} Источник: <b>{u['utm_source']}</b>\n"
                f"📊 Передано: <b>{mb} МБ</b>\n"
                f"✅ VPN подключён и работает!"
            )
            mark_seen(seen_conn, u["id"])
            print(f"Notified first traffic: {u['email']} ({mb} MB)")

    seen_conn.close()

if __name__ == "__main__":
    asyncio.run(main())
