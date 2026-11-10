import calendar
import json
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from fastapi import HTTPException
from core.config import settings
from core.database import db_session
from core.sse import sse_manager
from workflows.notification_workflow import classify_task_temporal_status


def compute_next_due_date(current_due_date: str | None, recurrence: dict[str, Any]) -> str:
    """Calculates the next due date based on recurrence rule (daily, weekly, monthly, custom_days)."""
    base_date = date.today()
    if current_due_date:
        try:
            base_date = date.fromisoformat(current_due_date[:10])
        except Exception:
            base_date = settings.now().date()
    else:
        base_date = settings.now().date()

    freq = str(recurrence.get("frequency") or "daily").lower()
    interval = max(1, int(recurrence.get("interval") or 1))

    if freq == "daily":
        next_d = base_date + timedelta(days=interval)
    elif freq == "weekly":
        next_d = base_date + timedelta(weeks=interval)
    elif freq == "monthly":
        month = base_date.month - 1 + interval
        year = base_date.year + month // 12
        month = month % 12 + 1
        day = min(base_date.day, calendar.monthrange(year, month)[1])
        next_d = date(year, month, day)
    elif freq == "custom_days":
        days = max(1, int(recurrence.get("days") or interval))
        next_d = base_date + timedelta(days=days)
    else:
        next_d = base_date + timedelta(days=interval)

    return next_d.isoformat()


def validate_and_normalize_custom_values(
    custom_values: dict[str, Any] | None,
    is_partial_update: bool = False,
    existing_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validates custom fields against definitions in custom_fields table."""
    merged = dict(existing_values or {})
    if custom_values:
        merged.update(custom_values)

    with db_session() as conn:
        fields = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM custom_fields WHERE is_active = 1 ORDER BY position ASC"
            ).fetchall()
        ]

    for cf in fields:
        key = cf["key"]
        ftype = cf["field_type"]
        required = bool(cf["required"])
        val = merged.get(key)

        if val is None and cf.get("default_value_json") is not None and not is_partial_update:
            try:
                val = json.loads(cf["default_value_json"])
                merged[key] = val
            except Exception:
                pass

        if required and (val is None or val == "" or val == []):
            if not is_partial_update or (custom_values is not None and key in custom_values):
                raise HTTPException(
                    status_code=422,
                    detail=f"O campo personalizado '{cf['name']}' ({key}) é obrigatório.",
                )

        if val is not None and val != "":
            if ftype in ("number", "currency"):
                try:
                    if isinstance(val, str):
                        val = float(val.replace("R$", "").replace(" ", "").replace(",", "."))
                    else:
                        val = float(val)
                    merged[key] = round(val, 2) if ftype == "currency" else val
                except Exception:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Valor numérico inválido para o campo '{cf['name']}'.",
                    )
            elif ftype == "checkbox":
                merged[key] = bool(val) if not isinstance(val, str) else val.lower() in ("true", "1", "sim", "yes")
            elif ftype == "multi_select":
                if isinstance(val, str):
                    merged[key] = [x.strip() for x in val.split(",") if x.strip()]
                elif isinstance(val, list):
                    merged[key] = [str(x) for x in val]

    return merged


def hydrate_tasks(conn: Any, task_rows: list[Any]) -> list[dict[str, Any]]:
    if not task_rows:
        return []

    raw_tasks = [dict(r) for r in task_rows]
    task_ids = [t["id"] for t in raw_tasks]

    # Lookup maps for statuses, projects, users
    statuses_map = {
        r["slug"]: dict(r)
        for r in conn.execute("SELECT * FROM statuses").fetchall()
    }
    projects_map = {
        r["id"]: dict(r)
        for r in conn.execute("SELECT * FROM projects").fetchall()
    }
    users_map = {
        r["id"]: dict(r)
        for r in conn.execute("SELECT id, username, full_name, email, role FROM users").fetchall()
    }

    # Subtasks, comments count, attachments count
    placeholders = ",".join("?" for _ in task_ids)
    subtasks_by_task: dict[str, list[dict[str, Any]]] = {}
    for s in conn.execute(
        f"SELECT * FROM subtasks WHERE task_id IN ({placeholders}) ORDER BY position ASC, created_at ASC",
        task_ids,
    ).fetchall():
        sd = dict(s)
        sd["is_completed"] = bool(sd["is_completed"])
        subtasks_by_task.setdefault(sd["task_id"], []).append(sd)

    comments_count_map: dict[str, int] = {}
    for c in conn.execute(
        f"SELECT task_id, COUNT(*) AS cnt FROM task_comments WHERE task_id IN ({placeholders}) GROUP BY task_id",
        task_ids,
    ).fetchall():
        comments_count_map[c["task_id"]] = c["cnt"]

    attachments_by_task: dict[str, list[dict[str, Any]]] = {}
    for a in conn.execute(
        f"SELECT * FROM task_attachments WHERE task_id IN ({placeholders}) ORDER BY created_at DESC",
        task_ids,
    ).fetchall():
        ad = dict(a)
        attachments_by_task.setdefault(ad["task_id"], []).append(ad)

    today_str = settings.now().strftime("%Y-%m-%d")
    hydrated: list[dict[str, Any]] = []
    for t in raw_tasks:
        tid = t["id"]
        st_obj = statuses_map.get(t["status_slug"], {})
        proj_obj = projects_map.get(t.get("project_id") or "", {})
        ass_obj = users_map.get(t.get("assignee_id") or "", {})

        try:
            participants = json.loads(t.get("participants_json") or "[]")
        except Exception:
            participants = []
        try:
            tags = json.loads(t.get("tags_json") or "[]")
        except Exception:
            tags = []
        try:
            recurrence = json.loads(t["recurrence_json"]) if t.get("recurrence_json") else None
        except Exception:
            recurrence = None
        try:
            custom_values = json.loads(t.get("custom_values_json") or "{}")
        except Exception:
            custom_values = {}

        subtasks = subtasks_by_task.get(tid, [])
        completed_subtasks = sum(1 for s in subtasks if s["is_completed"])

        item = {
            "id": tid,
            "code": t["code"],
            "title": t["title"],
            "description": t.get("description") or "",
            "notes": t.get("notes") or "",
            "status_slug": t["status_slug"],
            "status_name": st_obj.get("name", t["status_slug"]),
            "status_color": st_obj.get("color", "#71717a"),
            "priority": t.get("priority") or "P3",
            "project_id": t.get("project_id"),
            "project_name": proj_obj.get("name", ""),
            "project_color": proj_obj.get("color", "#71717a"),
            "assignee_id": t.get("assignee_id"),
            "assignee_name": ass_obj.get("full_name") or ass_obj.get("username") or "",
            "participants": participants,
            "tags": tags,
            "color": t.get("color") or "",
            "due_date": t.get("due_date"),
            "due_time": t.get("due_time"),
            "is_today": bool(t.get("is_today", 0)),
            "temporal_status": classify_task_temporal_status(t, today_str=today_str),
            "recurrence": recurrence,
            "custom_values": custom_values,
            "position": t.get("position", 0.0),
            "subtasks": subtasks,
            "subtasks_total": len(subtasks),
            "subtasks_completed": completed_subtasks,
            "comments_count": comments_count_map.get(tid, 0),
            "attachments": attachments_by_task.get(tid, []),
            "attachments_count": len(attachments_by_task.get(tid, [])),
            "created_by": t.get("created_by"),
            "completed_at": t.get("completed_at"),
            "created_at": t.get("created_at"),
            "updated_at": t.get("updated_at"),
        }
        hydrated.append(item)

    return hydrated


def get_task_detail_workflow(task_id: str) -> dict[str, Any]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Tarefa não encontrada.")
        task = hydrate_tasks(conn, [row])[0]

        comments = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
        ]
        history = []
        for h in conn.execute(
            "SELECT * FROM task_history WHERE task_id = ? ORDER BY created_at DESC LIMIT 100",
            (task_id,),
        ).fetchall():
            hd = dict(h)
            try:
                hd["changes"] = json.loads(hd.get("changes_json") or "{}")
            except Exception:
                hd["changes"] = {}
            history.append(hd)

        reminders = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM reminders WHERE task_id = ? ORDER BY remind_at ASC",
                (task_id,),
            ).fetchall()
        ]

        task["comments"] = comments
        task["history"] = history
        task["reminders"] = reminders
        return task


def record_task_history(
    conn: Any,
    task_id: str,
    user: dict[str, Any] | None,
    action: str,
    changes: dict[str, Any],
) -> None:
    actor = (
        (user.get("full_name") or user.get("username") or "Sistema")
        if user
        else "Sistema"
    )
    conn.execute(
        """
        INSERT INTO task_history (id, task_id, user_id, actor_name, action, changes_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            task_id,
            user["id"] if user else None,
            actor,
            action,
            json.dumps(changes, ensure_ascii=False),
            settings.now_iso(),
        ),
    )


def create_task_workflow(
    data: dict[str, Any],
    user: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    now = settings.now_iso()
    title = str(data.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="O título da tarefa é obrigatório.")

    status_slug = str(data.get("status_slug") or "pending").strip()
    priority = str(data.get("priority") or "P3").upper()
    if priority not in ("P1", "P2", "P3", "P4"):
        priority = "P3"

    custom_vals = validate_and_normalize_custom_values(
        data.get("custom_values"), is_partial_update=False
    )

    task_id = str(uuid.uuid4())
    with db_session() as conn:
        # Verify status exists
        st_exists = conn.execute(
            "SELECT 1 FROM statuses WHERE slug = ?", (status_slug,)
        ).fetchone()
        if not st_exists:
            raise HTTPException(status_code=422, detail=f"Status '{status_slug}' inválido.")

        max_code_row = conn.execute("SELECT COALESCE(MAX(code), 100) AS m FROM tasks").fetchone()
        next_code = int(max_code_row["m"]) + 1

        max_pos_row = conn.execute(
            "SELECT COALESCE(MAX(position), 0) AS p FROM tasks WHERE status_slug = ?",
            (status_slug,),
        ).fetchone()
        position = float(data.get("position") if data.get("position") is not None else (float(max_pos_row["p"]) + 10.0))

        completed_at = now if status_slug == "completed" else None
        due_date = (data.get("due_date") or "").strip() or None
        due_time = (data.get("due_time") or "").strip() or None
        is_today_int = 1 if data.get("is_today") else 0

        conn.execute(
            """
            INSERT INTO tasks (
                id, code, title, description, notes, status_slug, priority,
                project_id, assignee_id, participants_json, tags_json, color,
                due_date, due_time, is_today, recurrence_json, custom_values_json,
                position, created_by, completed_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                next_code,
                title,
                str(data.get("description") or ""),
                str(data.get("notes") or ""),
                status_slug,
                priority,
                data.get("project_id") or None,
                data.get("assignee_id") or None,
                json.dumps(data.get("participants") or [], ensure_ascii=False),
                json.dumps(data.get("tags") or [], ensure_ascii=False),
                str(data.get("color") or ""),
                due_date,
                due_time,
                is_today_int,
                json.dumps(data["recurrence"], ensure_ascii=False) if data.get("recurrence") else None,
                json.dumps(custom_vals, ensure_ascii=False),
                position,
                user["id"],
                completed_at,
                now,
                now,
            ),
        )

        # Create initial subtasks if passed
        subtasks_in = data.get("subtasks") or []
        for idx, st in enumerate(subtasks_in):
            st_title = st.get("title") if isinstance(st, dict) else str(st)
            st_done = bool(st.get("is_completed", False)) if isinstance(st, dict) else False
            if st_title and str(st_title).strip():
                conn.execute(
                    """
                    INSERT INTO subtasks (id, task_id, title, is_completed, position, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), task_id, str(st_title).strip(), 1 if st_done else 0, idx * 10, now),
                )

        record_task_history(
            conn,
            task_id,
            user,
            "created",
            {"title": title, "status_slug": status_slug, "priority": priority, "due_date": due_date, "is_today": bool(is_today_int)},
        )

    created_task = get_task_detail_workflow(task_id)
    events = ["task.created"]
    if created_task.get("assignee_id"):
        events.append("task.assigned")
    if status_slug == "completed":
        events.append("task.completed")

    sse_manager.broadcast_delta("task.created", created_task)
    return created_task, events


def update_task_workflow(
    task_id: str,
    patch: dict[str, Any],
    user: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    now = settings.now_iso()
    with db_session() as conn:
        old_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not old_row:
            raise HTTPException(status_code=404, detail="Tarefa não encontrada.")
        old = dict(old_row)

        try:
            old_custom = json.loads(old.get("custom_values_json") or "{}")
        except Exception:
            old_custom = {}

        if "custom_values" in patch and patch["custom_values"] is not None:
            new_custom = validate_and_normalize_custom_values(
                patch["custom_values"], is_partial_update=True, existing_values=old_custom
            )
        else:
            new_custom = old_custom

        changes: dict[str, Any] = {}
        events: list[str] = ["task.updated"]

        new_title = str(patch["title"]).strip() if "title" in patch and patch["title"] is not None else old["title"]
        if not new_title:
            raise HTTPException(status_code=422, detail="O título da tarefa não pode ficar vazio.")

        new_status = str(patch["status_slug"]).strip() if "status_slug" in patch and patch["status_slug"] is not None else old["status_slug"]
        if new_status != old["status_slug"]:
            st_exists = conn.execute("SELECT 1 FROM statuses WHERE slug = ?", (new_status,)).fetchone()
            if not st_exists:
                raise HTTPException(status_code=422, detail=f"Status '{new_status}' não existe.")

        new_priority = str(patch["priority"]).upper() if "priority" in patch and patch["priority"] is not None else old["priority"]
        if new_priority not in ("P1", "P2", "P3", "P4"):
            new_priority = old["priority"]

        new_desc = str(patch["description"]) if "description" in patch and patch["description"] is not None else old["description"]
        new_notes = str(patch["notes"]) if "notes" in patch and patch["notes"] is not None else old["notes"]
        new_project = patch["project_id"] if "project_id" in patch else old["project_id"]
        new_assignee = patch["assignee_id"] if "assignee_id" in patch else old["assignee_id"]
        new_color = str(patch["color"]) if "color" in patch and patch["color"] is not None else old["color"]
        new_due_date = (patch["due_date"] or "").strip() or None if "due_date" in patch else old["due_date"]
        new_due_time = (patch["due_time"] or "").strip() or None if "due_time" in patch else old["due_time"]
        new_is_today = (1 if patch["is_today"] else 0) if "is_today" in patch and patch["is_today"] is not None else int(old.get("is_today") or 0)
        new_pos = float(patch["position"]) if "position" in patch and patch["position"] is not None else float(old["position"])

        if "participants" in patch and patch["participants"] is not None:
            new_participants_json = json.dumps(patch["participants"], ensure_ascii=False)
        else:
            new_participants_json = old["participants_json"]

        if "tags" in patch and patch["tags"] is not None:
            new_tags_json = json.dumps(patch["tags"], ensure_ascii=False)
        else:
            new_tags_json = old["tags_json"]

        if "recurrence" in patch:
            new_recurrence_json = (
                json.dumps(patch["recurrence"], ensure_ascii=False)
                if patch["recurrence"]
                else None
            )
        else:
            new_recurrence_json = old["recurrence_json"]

        # Track semantic events and history diffs
        tracked_pairs = [
            ("title", old["title"], new_title),
            ("status_slug", old["status_slug"], new_status),
            ("priority", old["priority"], new_priority),
            ("project_id", old["project_id"], new_project),
            ("assignee_id", old["assignee_id"], new_assignee),
            ("due_date", old["due_date"], new_due_date),
            ("due_time", old["due_time"], new_due_time),
            ("is_today", bool(old.get("is_today")), bool(new_is_today)),
            ("color", old["color"], new_color),
        ]
        for field_key, ov, nv in tracked_pairs:
            if ov != nv:
                changes[field_key] = {"from": ov, "to": nv}

        if old_custom != new_custom:
            changes["custom_values"] = {"from": old_custom, "to": new_custom}

        completed_at = old["completed_at"]
        spawn_recurring_task = False
        if old["status_slug"] != "completed" and new_status == "completed":
            completed_at = now
            events.append("task.completed")
            if new_recurrence_json:
                spawn_recurring_task = True
        elif old["status_slug"] == "completed" and new_status != "completed":
            completed_at = None
            events.append("task.reopened")

        if old["assignee_id"] != new_assignee and new_assignee:
            events.append("task.assigned")

        if old["due_date"] != new_due_date or old["due_time"] != new_due_time:
            events.append("task.due_date_changed")

        conn.execute(
            """
            UPDATE tasks SET
                title = ?, description = ?, notes = ?, status_slug = ?, priority = ?,
                project_id = ?, assignee_id = ?, participants_json = ?, tags_json = ?,
                color = ?, due_date = ?, due_time = ?, is_today = ?, recurrence_json = ?,
                custom_values_json = ?, position = ?, completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                new_title,
                new_desc,
                new_notes,
                new_status,
                new_priority,
                new_project,
                new_assignee,
                new_participants_json,
                new_tags_json,
                new_color,
                new_due_date,
                new_due_time,
                new_is_today,
                new_recurrence_json,
                json.dumps(new_custom, ensure_ascii=False),
                new_pos,
                completed_at,
                now,
                task_id,
            ),
        )

        if changes:
            action_label = "completed" if "task.completed" in events else "updated"
            record_task_history(conn, task_id, user, action_label, changes)

        # Handle recurring task auto-creation when completed
        if spawn_recurring_task and new_recurrence_json:
            try:
                rec_rule = json.loads(new_recurrence_json)
                if isinstance(rec_rule, dict) and rec_rule.get("enabled", True):
                    next_due = compute_next_due_date(new_due_date, rec_rule)
                    max_code = conn.execute("SELECT COALESCE(MAX(code), 100) AS m FROM tasks").fetchone()["m"]
                    new_rec_id = str(uuid.uuid4())
                    conn.execute(
                        """
                        INSERT INTO tasks (
                            id, code, title, description, notes, status_slug, priority,
                            project_id, assignee_id, participants_json, tags_json, color,
                            due_date, due_time, is_today, recurrence_json, custom_values_json,
                            position, created_by, completed_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                        """,
                        (
                            new_rec_id,
                            int(max_code) + 1,
                            new_title,
                            new_desc,
                            new_notes,
                            new_priority,
                            new_project,
                            new_assignee,
                            new_participants_json,
                            new_tags_json,
                            new_color,
                            next_due,
                            new_due_time,
                            new_is_today,
                            new_recurrence_json,
                            json.dumps(new_custom, ensure_ascii=False),
                            new_pos + 1.0,
                            user["id"],
                            now,
                            now,
                        ),
                    )
                    record_task_history(
                        conn,
                        new_rec_id,
                        user,
                        "recurrence_spawned",
                        {"source_task_id": task_id, "due_date": next_due},
                    )
            except Exception:
                pass

    updated_task = get_task_detail_workflow(task_id)
    # Broadcast Delta-Only payload via SSE for Zero-Flicker update
    sse_manager.broadcast_delta(
        "task.updated",
        {
            "id": task_id,
            "changes": changes,
            "task": updated_task,
        },
    )
    return updated_task, events


def delete_task_workflow(task_id: str, user: dict[str, Any]) -> dict[str, Any]:
    task = get_task_detail_workflow(task_id)
    with db_session() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    sse_manager.broadcast_delta("task.deleted", {"id": task_id, "code": task["code"]})
    return task
