import json
import uuid
from datetime import timedelta
from typing import Any
from loguru import logger
from core.config import settings
from core.database import db_session, row_to_dict
from core.sse import sse_manager
from services.webhook_client import webhook_client

SUPPORTED_WEBHOOK_EVENTS = [
    "task.created",
    "task.updated",
    "task.deleted",
    "task.completed",
    "task.reopened",
    "task.assigned",
    "task.comment_created",
    "task.due_date_changed",
]


async def dispatch_webhook_event(
    event_name: str,
    payload_data: dict[str, Any],
    project_id: str | None = None,
) -> dict[str, Any]:
    """Finds active webhooks matching event_name and project_id, sends payloads, and records history."""
    now = settings.now_iso()
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM webhooks WHERE is_active = 1").fetchall()
        webhooks = [dict(r) for r in rows]

    matched_webhooks: list[dict[str, Any]] = []
    for wh in webhooks:
        try:
            events = json.loads(wh.get("events_json") or "[]")
            projects = json.loads(wh.get("project_ids_json") or "[]")
        except Exception:
            events, projects = [], []

        if "*" not in events and event_name not in events:
            continue
        if projects and project_id and project_id not in projects:
            continue
        matched_webhooks.append(wh)

    results = []
    for wh in matched_webhooks:
        delivery_id = str(uuid.uuid4())
        envelope = {
            "id": delivery_id,
            "event": event_name,
            "timestamp": now,
            "project_id": project_id,
            "data": payload_data,
        }
        res = await webhook_client.send(
            target_url=wh["target_url"],
            event_name=event_name,
            payload=envelope,
            secret=wh.get("secret", ""),
            delivery_id=delivery_id,
        )
        max_retries = int(wh.get("max_retries", 3))
        if res["success"]:
            status_str = "success"
            next_retry = None
        else:
            if max_retries > 1:
                status_str = "retrying"
                next_retry = (settings.now() + timedelta(minutes=2)).isoformat()
            else:
                status_str = "failed"
                next_retry = None

        with db_session() as conn:
            conn.execute(
                """
                INSERT INTO webhook_deliveries (
                    id, webhook_id, event_name, payload_json, response_status,
                    response_body, error_message, attempt_count, next_retry_at,
                    status, duration_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    wh["id"],
                    event_name,
                    json.dumps(envelope, ensure_ascii=False),
                    res["status_code"],
                    res["response_body"],
                    res["error"],
                    next_retry,
                    status_str,
                    res["duration_ms"],
                    now,
                ),
            )
        results.append(
            {
                "delivery_id": delivery_id,
                "webhook_id": wh["id"],
                "status": status_str,
                "status_code": res["status_code"],
            }
        )

    if results:
        sse_manager.broadcast_delta("webhook.delivered", {"count": len(results), "event": event_name})
    return {"dispatched": len(results), "deliveries": results}


async def process_pending_webhook_retries() -> dict[str, Any]:
    """Retries failed webhook deliveries with exponential backoff."""
    now_iso = settings.now_iso()
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT d.*, w.target_url, w.secret, w.max_retries, w.is_active
            FROM webhook_deliveries d
            JOIN webhooks w ON w.id = d.webhook_id
            WHERE d.status = 'retrying' AND d.next_retry_at IS NOT NULL AND d.next_retry_at <= ?
            LIMIT 30
            """,
            (now_iso,),
        ).fetchall()
        pending = [dict(r) for r in rows]

    retried = 0
    succeeded = 0
    for item in pending:
        if not item.get("is_active"):
            with db_session() as conn:
                conn.execute(
                    "UPDATE webhook_deliveries SET status = 'failed', next_retry_at = NULL WHERE id = ?",
                    (item["id"],),
                )
            continue

        try:
            envelope = json.loads(item["payload_json"])
        except Exception:
            envelope = {"raw": item["payload_json"]}

        attempt = int(item["attempt_count"]) + 1
        res = await webhook_client.send(
            target_url=item["target_url"],
            event_name=item["event_name"],
            payload=envelope,
            secret=item.get("secret", ""),
            delivery_id=item["id"],
        )
        max_retries = int(item.get("max_retries", 3))
        if res["success"]:
            new_status = "success"
            next_retry = None
            succeeded += 1
        elif attempt >= max_retries:
            new_status = "failed"
            next_retry = None
        else:
            new_status = "retrying"
            backoff_minutes = 2 ** attempt
            next_retry = (settings.now() + timedelta(minutes=backoff_minutes)).isoformat()

        with db_session() as conn:
            conn.execute(
                """
                UPDATE webhook_deliveries
                SET response_status = ?, response_body = ?, error_message = ?,
                    attempt_count = ?, next_retry_at = ?, status = ?, duration_ms = ?
                WHERE id = ?
                """,
                (
                    res["status_code"],
                    res["response_body"],
                    res["error"],
                    attempt,
                    next_retry,
                    new_status,
                    res["duration_ms"],
                    item["id"],
                ),
            )
        retried += 1

    return {"retried": retried, "succeeded": succeeded}
