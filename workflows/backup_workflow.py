import json
import uuid
from pathlib import Path
from typing import Any
from core.config import settings
from core.database import db_session
from core.sse import sse_manager
from services.storage_client import storage_client


def get_backup_config() -> dict[str, Any]:
    with db_session() as conn:
        row = conn.execute(
            "SELECT value_json FROM system_settings WHERE key = 'backup_config'"
        ).fetchone()
        if row:
            try:
                return json.loads(row["value_json"])
            except Exception:
                pass
    return {
        "auto_backup_enabled": True,
        "cron": settings.AUTO_BACKUP_CRON,
        "retention_count": settings.BACKUP_RETENTION_COUNT,
        "include_attachments": True,
    }


def update_backup_config(new_config: dict[str, Any]) -> dict[str, Any]:
    current = get_backup_config()
    current.update(new_config)
    now = settings.now_iso()
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO system_settings (key, value_json, updated_at)
            VALUES ('backup_config', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
            """,
            (json.dumps(current, ensure_ascii=False), now),
        )
    return current


def run_backup_workflow(
    user_id: str | None = None,
    notes: str = "Backup manual",
    include_attachments: bool | None = None,
) -> dict[str, Any]:
    cfg = get_backup_config()
    inc_att = (
        include_attachments
        if include_attachments is not None
        else bool(cfg.get("include_attachments", True))
    )
    info = storage_client.create_online_backup(include_attachments=inc_att)
    bkp_id = str(uuid.uuid4())
    now = settings.now_iso()

    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO backups (id, filename, size_bytes, includes_attachments, created_by, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bkp_id,
                info["filename"],
                info["size_bytes"],
                1 if inc_att else 0,
                user_id,
                notes,
                now,
            ),
        )

    # Apply retention policy
    retention = max(1, int(cfg.get("retention_count", settings.BACKUP_RETENTION_COUNT)))
    enforce_backup_retention(retention)

    result = {
        "id": bkp_id,
        "filename": info["filename"],
        "size_bytes": info["size_bytes"],
        "includes_attachments": inc_att,
        "notes": notes,
        "created_at": now,
    }
    sse_manager.broadcast_delta("backup.created", result)
    return result


def enforce_backup_retention(retention_count: int) -> dict[str, Any]:
    removed = []
    with db_session() as conn:
        rows = conn.execute(
            "SELECT id, filename FROM backups ORDER BY created_at DESC"
        ).fetchall()
        if len(rows) > retention_count:
            to_delete = rows[retention_count:]
            for r in to_delete:
                fpath = settings.BACKUPS_DIR / Path(r["filename"]).name
                if fpath.exists():
                    try:
                        fpath.unlink()
                    except Exception:
                        pass
                conn.execute("DELETE FROM backups WHERE id = ?", (r["id"],))
                removed.append(r["filename"])
    return {"removed_count": len(removed), "removed_files": removed}


def list_backups_workflow() -> list[dict[str, Any]]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM backups ORDER BY created_at DESC"
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            fpath = settings.BACKUPS_DIR / Path(d["filename"]).name
            d["exists_on_disk"] = fpath.exists()
            items.append(d)
        return items


def restore_backup_workflow(backup_id_or_filename: str) -> dict[str, Any]:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM backups WHERE id = ? OR filename = ?",
            (backup_id_or_filename, backup_id_or_filename),
        ).fetchone()
        filename = row["filename"] if row else backup_id_or_filename

    res = storage_client.restore_online_backup(filename)
    sse_manager.broadcast_delta("backup.restored", res)
    return res
