#!/usr/bin/env python3
"""
Миграция задач из map.html → SQLite (workspace_tasks.db)
Парсит JS-данные напрямую через regex, не требует браузера.
"""
import re, json, sqlite3, hashlib
from datetime import datetime

MAP_HTML = "/var/www/chebvpn/map.html"
TASKS_DB = "/root/workspace_tasks.db"

# ── Читаем map.html ───────────────────────────────────────────────
with open(MAP_HTML, encoding="utf-8") as f:
    src = f.read()

# ── Вытаскиваем все ветки и задачи ───────────────────────────────
# Ищем блоки вида: id:'xxx', label:'...', ..., tasks:[{...}]
branch_pattern = re.compile(
    r"id:'([^']+)'.*?label:'([^']+)'.*?tasks:\[([^\]]*)\]",
    re.DOTALL
)
task_pattern = re.compile(
    r"\{([^{}]*)\}",
    re.DOTALL
)
field_pattern = re.compile(r"(\w+):'([^']*)'")

tasks_to_insert = []
now = datetime.utcnow().isoformat()

for branch_m in branch_pattern.finditer(src):
    branch_id    = branch_m.group(1)
    branch_label = branch_m.group(2).replace("\\n", " ").strip()
    tasks_block  = branch_m.group(3)

    for task_m in task_pattern.finditer(tasks_block):
        raw = task_m.group(1)
        fields = dict(field_pattern.findall(raw))
        label = fields.get("l", "").strip()
        if not label:
            continue

        status   = fields.get("s", "todo")
        priority = fields.get("p", None)
        desc     = fields.get("d", "").strip()

        # Стабильный ID на основе ветки + названия
        task_id = "js_" + hashlib.md5(f"{branch_id}:{label}".encode()).hexdigest()[:10]

        tasks_to_insert.append({
            "id":          task_id,
            "branch":      branch_label,
            "status":      status,
            "priority":    priority,
            "label":       label,
            "description": desc or None,
        })

print(f"Найдено задач: {len(tasks_to_insert)}")

# ── Вставляем в SQLite (upsert) ───────────────────────────────────
conn = sqlite3.connect(TASKS_DB)
conn.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        branch TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'todo',
        priority TEXT DEFAULT NULL,
        label TEXT NOT NULL,
        description TEXT DEFAULT NULL,
        notes TEXT DEFAULT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
""")

inserted = 0
skipped  = 0
for t in tasks_to_insert:
    existing = conn.execute("SELECT id FROM tasks WHERE id=?", (t["id"],)).fetchone()
    if existing:
        skipped += 1
        continue
    conn.execute("""
        INSERT INTO tasks (id, branch, status, priority, label, description, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
    """, (t["id"], t["branch"], t["status"], t["priority"], t["label"], t["description"], now, now))
    inserted += 1

conn.commit()
conn.close()

print(f"Вставлено: {inserted} | Пропущено (уже есть): {skipped}")

# ── Проверка ──────────────────────────────────────────────────────
conn = sqlite3.connect(TASKS_DB)
counts = conn.execute("""
    SELECT status, COUNT(*) FROM tasks GROUP BY status ORDER BY status
""").fetchall()
conn.close()
print("\nИтог в БД:")
for status, cnt in counts:
    emoji = {"urgent":"🚨","active":"🔥","todo":"📋","done":"✅"}.get(status,"•")
    print(f"  {emoji} {status}: {cnt}")
