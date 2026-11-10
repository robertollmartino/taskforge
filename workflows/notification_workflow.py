import json
import uuid
from datetime import datetime
from typing import Any
from loguru import logger
from core.config import settings
from core.database import db_session
from core.sse import sse_manager
from services.webpush_client import webpush_client


def classify_task_temporal_status(task: dict[str, Any], today_str: str | None = None) -> str:
    """Distinguishes 'overdue', 'today', 'upcoming', 'no_date', or 'completed'."""
    status_slug = task.get("status_slug", "")
    if status_slug in ("completed", "cancelled"):
        return "completed"
    due_date = (task.get("due_date") or "").strip()
    if not due_date:
        return "no_date"
    ref_today = today_str or settings.now().strftime("%Y-%m-%d")
    if due_date < ref_today:
        return "overdue"
    elif due_date == ref_today:
        due_time = (task.get("due_time") or "").strip()
        if due_time:
            now_time = settings.now().strftime("%H:%M")
            if due_time < now_time:
                return "overdue"
        return "today"
    else:
        return "upcoming"


def send_user_notification(
    user_id: str,
    title: str,
    body: str,
    category: str = "general",
    dedup_key: str | None = None,
    task_id: str | None = None,
    url: str = "/",
) -> dict[str, Any]:
    """Logs notification with strict deduplication and dispatches Web Push VAPID to user devices."""
    now_iso = settings.now_iso()
    final_dedup = dedup_key or f"{user_id}:{category}:{uuid.uuid4().hex}"

    with db_session() as conn:
        existing = conn.execute(
            "SELECT id FROM notification_logs WHERE dedup_key = ?", (final_dedup,)
        ).fetchone()
        if existing:
            return {"sent": False, "deduplicated": True, "dedup_key": final_dedup}

        notif_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO notification_logs (
                id, user_id, task_id, dedup_key, category, title, body, is_read, sent_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (notif_id, user_id, task_id, final_dedup, category, title, body, now_iso),
        )
        subs = conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id = ?", (user_id,)
        ).fetchall()
        sub_list = [dict(s) for s in subs]

    push_sent = 0
    dead_sub_ids = []
    for sub in sub_list:
        sub_info = {
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
        }
        res = webpush_client.send_push(
            subscription_info=sub_info,
            title=title,
            body=body,
            url=url,
            tag=final_dedup[:40],
        )
        if res.get("sent"):
            push_sent += 1
        elif res.get("status_code") in (404, 410):
            dead_sub_ids.append(sub["id"])

    if dead_sub_ids:
        with db_session() as conn:
            for sid in dead_sub_ids:
                conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (sid,))

    payload = {
        "id": notif_id,
        "user_id": user_id,
        "task_id": task_id,
        "category": category,
        "title": title,
        "body": body,
        "sent_at": now_iso,
        "push_devices_reached": push_sent,
    }
    sse_manager.broadcast_delta("notification.created", payload)
    return {"sent": True, "deduplicated": False, "notification": payload}


def process_due_reminders_workflow() -> dict[str, Any]:
    """Processes task reminders whose remind_at <= current Sao Paulo time."""
    now = settings.now()
    now_iso = now.isoformat()
    now_short = now.strftime("%Y-%m-%dT%H:%M")

    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT r.*, t.title AS task_title, t.code AS task_code, t.status_slug, t.due_date, t.due_time
            FROM reminders r
            JOIN tasks t ON t.id = r.task_id
            WHERE r.is_sent = 0 AND (r.remind_at <= ? OR r.remind_at <= ?)
            """,
            (now_iso, now_short),
        ).fetchall()
        due_list = [dict(r) for r in rows]

    triggered = 0
    for rem in due_list:
        if rem["status_slug"] in ("completed", "cancelled"):
            with db_session() as conn:
                conn.execute(
                    "UPDATE reminders SET is_sent = 1, sent_at = ? WHERE id = ?",
                    (now_iso, rem["id"]),
                )
            continue

        temp_class = classify_task_temporal_status(rem)
        prefix_map = {
            "overdue": "⚠️ [ATRASADA]",
            "today": "📅 [HOJE]",
            "upcoming": "🔔 [LEMBRETE]",
            "no_date": "🔔 [LEMBRETE]",
        }
        prefix = prefix_map.get(temp_class, "🔔 [LEMBRETE]")
        title = f"{prefix} #{rem['task_code']} {rem['task_title']}"
        body = rem.get("message") or f"Lembrete programado para a tarefa #{rem['task_code']}."
        dedup_key = f"reminder:{rem['id']}"

        send_user_notification(
            user_id=rem["user_id"],
            title=title,
            body=body,
            category=temp_class if temp_class in ("overdue", "today", "upcoming") else "reminder",
            dedup_key=dedup_key,
            task_id=rem["task_id"],
        )

        with db_session() as conn:
            conn.execute(
                "UPDATE reminders SET is_sent = 1, sent_at = ? WHERE id = ?",
                (now_iso, rem["id"]),
            )
        triggered += 1

    return {"processed_reminders": triggered}


def build_user_digest_summary(user_id: str) -> dict[str, Any]:
    """Calculates pending tasks grouped by Overdue, Today, and Upcoming for a user."""
    today_str = settings.now().strftime("%Y-%m-%d")
    with db_session() as conn:
        user_row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user_row:
            return {"overdue": [], "today": [], "upcoming": [], "no_date": []}
        user = dict(user_row)

        if user["role"] in ("admin", "supervisor"):
            tasks_rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status_slug NOT IN ('completed', 'cancelled')
                  AND (assignee_id = ? OR assignee_id IS NULL OR created_by = ?)
                """,
                (user_id, user_id),
            ).fetchall()
        else:
            tasks_rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status_slug NOT IN ('completed', 'cancelled')
                  AND (assignee_id = ? OR created_by = ? OR participants_json LIKE ?)
                """,
                (user_id, user_id, f'%"{user_id}"%'),
            ).fetchall()

    grouped: dict[str, list[dict[str, Any]]] = {
        "overdue": [],
        "today": [],
        "upcoming": [],
        "no_date": [],
    }
    for r in tasks_rows:
        t = dict(r)
        cat = classify_task_temporal_status(t, today_str=today_str)
        if cat in grouped:
            grouped[cat].append(
                {
                    "id": t["id"],
                    "code": t["code"],
                    "title": t["title"],
                    "priority": t["priority"],
                    "due_date": t["due_date"],
                    "due_time": t["due_time"],
                }
            )
    return grouped


def run_scheduled_digests_workflow(force_slot: str | None = None) -> dict[str, Any]:
    """Checks users' digest_times_json (default 08:00, 13:00, 18:00 in America/Sao_Paulo) and sends summary."""
    now = settings.now()
    today_str = now.strftime("%Y-%m-%d")
    current_hhmm = force_slot or now.strftime("%H:%M")

    with db_session() as conn:
        users = [
            dict(u)
            for u in conn.execute(
                "SELECT * FROM users WHERE is_active = 1 AND digest_enabled = 1"
            ).fetchall()
        ]

    sent_count = 0
    for u in users:
        try:
            times = json.loads(u.get("digest_times_json") or '["08:00","13:00","18:00"]')
        except Exception:
            times = ["08:00", "13:00", "18:00"]

        if not force_slot and current_hhmm not in times:
            continue

        summary = build_user_digest_summary(u["id"])
        n_overdue = len(summary["overdue"])
        n_today = len(summary["today"])
        n_upcoming = len(summary["upcoming"])
        total_pending = n_overdue + n_today + n_upcoming + len(summary["no_date"])

        if total_pending == 0 and not force_slot:
            continue

        dedup_key = f"digest:{u['id']}:{today_str}:{current_hhmm}"
        title = f"📊 Resumo de Tarefas ({current_hhmm})"
        body = (
            f"Atrasadas: {n_overdue} | Para Hoje: {n_today} | "
            f"Futuras: {n_upcoming} | Total pendentes: {total_pending}"
        )
        res = send_user_notification(
            user_id=u["id"],
            title=title,
            body=body,
            category="digest",
            dedup_key=dedup_key,
        )
        if res.get("sent"):
            sent_count += 1

    return {"slot": current_hhmm, "digests_sent": sent_count}
