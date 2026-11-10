import json
import uuid
from pathlib import Path
from typing import Any
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from core.config import settings
from core.database import db_session, row_to_dict
from core.security import (
    can_user_access_task,
    check_permission,
    create_access_token,
    generate_api_token,
    get_effective_permissions,
    hash_password,
    verify_password,
    verify_token,
)
from core.sse import sse_manager
from services.storage_client import storage_client
from services.webpush_client import webpush_client
from workflows.backup_workflow import (
    get_backup_config,
    list_backups_workflow,
    restore_backup_workflow,
    run_backup_workflow,
    update_backup_config,
)
from workflows.filter_workflow import filter_and_paginate_tasks
from workflows.notification_workflow import (
    build_user_digest_summary,
    process_due_reminders_workflow,
    run_scheduled_digests_workflow,
    send_user_notification,
)
from workflows.task_workflow import (
    create_task_workflow,
    delete_task_workflow,
    get_task_detail_workflow,
    hydrate_tasks,
    record_task_history,
    update_task_workflow,
)
from workflows.webhook_workflow import (
    SUPPORTED_WEBHOOK_EVENTS,
    dispatch_webhook_event,
)

api_router = APIRouter(prefix="/api/v1")


# =====================================================================
# PYDANTIC SCHEMAS (OpenAPI / Swagger Documentation)
# =====================================================================
class LoginRequest(BaseModel):
    username: str = Field(..., description="Username ou E-mail do usuário")
    password: str = Field(..., description="Senha")


class ProfileUpdateRequest(BaseModel):
    full_name: str | None = None
    email: str | None = None
    password: str | None = None
    digest_enabled: bool | None = None
    digest_times: list[str] | None = Field(
        default=None, description="Horários ex: ['08:00', '13:00', '18:00']"
    )
    timezone: str | None = None


class UserCreateRequest(BaseModel):
    username: str
    email: str
    full_name: str
    password: str
    role: str = Field(default="member", description="admin, supervisor, member, viewer")
    permissions: dict[str, bool] = Field(default_factory=dict)
    digest_enabled: bool = True
    digest_times: list[str] = Field(default_factory=lambda: ["08:00", "13:00", "18:00"])


class UserUpdateRequest(BaseModel):
    email: str | None = None
    full_name: str | None = None
    password: str | None = None
    role: str | None = None
    permissions: dict[str, bool] | None = None
    digest_enabled: bool | None = None
    digest_times: list[str] | None = None
    is_active: bool | None = None


class ApiTokenCreateRequest(BaseModel):
    name: str = Field(..., description="Nome do agente/integração (ex: n8n, ChatGPT)")
    scopes: list[str] = Field(default_factory=lambda: ["*"])
    expires_at: str | None = None


class ProjectCreateRequest(BaseModel):
    name: str
    description: str = ""
    color: str = "#f97316"
    icon: str = "folder"


class ProjectUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    color: str | None = None
    icon: str | None = None
    is_archived: bool | None = None


class ProjectMemberRequest(BaseModel):
    user_id: str
    role: str = "member"
    permissions: dict[str, bool] = Field(default_factory=dict)


class StatusCreateRequest(BaseModel):
    slug: str | None = None
    name: str
    color: str = "#a855f7"
    category: str = "open"
    position: int = 50


class StatusUpdateRequest(BaseModel):
    name: str | None = None
    color: str | None = None
    category: str | None = None
    position: int | None = None


class CustomFieldCreateRequest(BaseModel):
    key: str | None = None
    name: str
    field_type: str = Field(
        ...,
        description="short_text, long_text, number, currency, date, datetime, checkbox, select, multi_select, url",
    )
    required: bool = False
    default_value: Any = None
    options: list[str] = Field(default_factory=list)
    position: int = 0


class CustomFieldUpdateRequest(BaseModel):
    name: str | None = None
    field_type: str | None = None
    required: bool | None = None
    default_value: Any = None
    options: list[str] | None = None
    position: int | None = None
    is_active: bool | None = None


class TaskCreateRequest(BaseModel):
    title: str
    description: str = ""
    notes: str = ""
    status_slug: str = "pending"
    priority: str = "P3"
    project_id: str | None = None
    assignee_id: str | None = None
    participants: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    color: str = ""
    due_date: str | None = Field(default=None, description="YYYY-MM-DD ou null para sem data")
    due_time: str | None = Field(default=None, description="HH:MM opcional")
    is_today: bool = Field(default=False, description="Flag manual persistente 'Hoje'")
    recurrence: dict[str, Any] | None = None
    custom_values: dict[str, Any] = Field(default_factory=dict)
    subtasks: list[Any] = Field(default_factory=list)
    position: float | None = None


class TaskUpdateRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    notes: str | None = None
    status_slug: str | None = None
    priority: str | None = None
    project_id: str | None = None
    assignee_id: str | None = None
    participants: list[str] | None = None
    tags: list[str] | None = None
    color: str | None = None
    due_date: str | None = None
    due_time: str | None = None
    is_today: bool | None = None
    recurrence: dict[str, Any] | None = None
    custom_values: dict[str, Any] | None = None
    position: float | None = None


class TaskQueryRequest(BaseModel):
    filter_tree: dict[str, Any] | None = Field(
        default=None,
        description="Árvore lógica {'logic': 'AND'|'OR', 'conditions': [...], 'groups': [...]}",
    )
    search: str = ""
    project_ids: list[str] | None = None
    sorts: list[dict[str, str]] | None = None
    group_by: str | None = None
    page: int = 1
    page_size: int = 100


class SubtaskCreateRequest(BaseModel):
    title: str
    is_completed: bool = False
    position: int = 0


class SubtaskUpdateRequest(BaseModel):
    title: str | None = None
    is_completed: bool | None = None
    position: int | None = None


class CommentCreateRequest(BaseModel):
    content: str


class SavedViewRequest(BaseModel):
    name: str
    view_type: str = "table"
    visible_columns: list[str] = Field(
        default_factory=lambda: [
            "code",
            "is_today",
            "title",
            "status_slug",
            "priority",
            "project_id",
            "assignee_id",
            "due_date",
            "tags",
        ]
    )
    column_order: list[str] = Field(
        default_factory=lambda: [
            "code",
            "is_today",
            "title",
            "status_slug",
            "priority",
            "project_id",
            "assignee_id",
            "due_date",
            "tags",
        ]
    )
    filters: dict[str, Any] = Field(
        default_factory=lambda: {"logic": "AND", "conditions": [], "groups": []}
    )
    sorts: list[dict[str, str]] = Field(
        default_factory=lambda: [{"field": "priority", "dir": "asc"}]
    )
    group_by: str = ""
    display_prefs: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False
    is_shared: bool = False
    shared_user_ids: list[str] = Field(default_factory=list)


class PushSubscriptionRequest(BaseModel):
    endpoint: str
    keys: dict[str, str]
    user_agent: str = ""


class ReminderCreateRequest(BaseModel):
    task_id: str
    remind_at: str = Field(..., description="ISO datetime ou YYYY-MM-DDTHH:MM")
    message: str = ""
    user_id: str | None = None
    channel: str = "push"


class WebhookCreateRequest(BaseModel):
    name: str
    description: str = ""
    target_url: str
    events: list[str] = Field(default_factory=lambda: ["task.created", "task.completed"])
    project_ids: list[str] = Field(default_factory=list)
    secret: str = ""
    is_active: bool = True
    max_retries: int = 3


class WebhookUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    target_url: str | None = None
    events: list[str] | None = None
    project_ids: list[str] | None = None
    secret: str | None = None
    is_active: bool | None = None
    max_retries: int | None = None


class BackupCreateRequest(BaseModel):
    notes: str = "Backup manual via API"
    include_attachments: bool = True


# =====================================================================
# HELPER FUNCTIONS
# =====================================================================
def _serialize_user(u: dict[str, Any]) -> dict[str, Any]:
    try:
        perms = json.loads(u.get("permissions_json") or "{}")
    except Exception:
        perms = {}
    try:
        digest_times = json.loads(u.get("digest_times_json") or '["08:00","13:00","18:00"]')
    except Exception:
        digest_times = ["08:00", "13:00", "18:00"]
    return {
        "id": u["id"],
        "username": u["username"],
        "email": u["email"],
        "full_name": u["full_name"],
        "role": u["role"],
        "permissions": get_effective_permissions(u),
        "raw_permissions": perms,
        "digest_enabled": bool(u.get("digest_enabled", 1)),
        "digest_times": digest_times,
        "timezone": u.get("timezone", settings.TZ_NAME),
        "is_active": bool(u.get("is_active", 1)),
        "created_at": u.get("created_at"),
    }


def _slugify(text: str) -> str:
    import re
    import unicodedata

    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", normalized.lower()).strip("_")
    return slug or f"item_{uuid.uuid4().hex[:6]}"


async def _bg_dispatch_events(events: list[str], task_data: dict[str, Any]) -> None:
    for ev in events:
        await dispatch_webhook_event(ev, task_data, project_id=task_data.get("project_id"))


# =====================================================================
# 1. AUTH & PROFILE & SSE STREAM
# =====================================================================
@api_router.post("/auth/login", tags=["Auth & Users"])
async def login(payload: LoginRequest) -> dict[str, Any]:
    ident = payload.username.strip()
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE (username = ? OR email = ?) AND is_active = 1",
            (ident, ident),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="Credenciais inválidas.")
        user = dict(row)
        if not verify_password(payload.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Credenciais inválidas.")

    token = create_access_token(user["id"], user["username"], user["role"])
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": _serialize_user(user),
    }


@api_router.get("/auth/me", tags=["Auth & Users"])
async def get_me(user: dict[str, Any] = Depends(verify_token)) -> dict[str, Any]:
    return {"user": _serialize_user(user)}


@api_router.patch("/auth/me", tags=["Auth & Users"])
async def update_my_profile(
    payload: ProfileUpdateRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    with db_session() as conn:
        curr = dict(conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone())
        full_name = payload.full_name if payload.full_name is not None else curr["full_name"]
        email = payload.email if payload.email is not None else curr["email"]
        pwd_hash = (
            hash_password(payload.password)
            if payload.password
            else curr["password_hash"]
        )
        dig_en = (
            (1 if payload.digest_enabled else 0)
            if payload.digest_enabled is not None
            else curr["digest_enabled"]
        )
        dig_times = (
            json.dumps(payload.digest_times)
            if payload.digest_times is not None
            else curr["digest_times_json"]
        )
        tz = payload.timezone if payload.timezone is not None else curr["timezone"]

        conn.execute(
            """
            UPDATE users
            SET full_name = ?, email = ?, password_hash = ?,
                digest_enabled = ?, digest_times_json = ?, timezone = ?
            WHERE id = ?
            """,
            (full_name, email, pwd_hash, dig_en, dig_times, tz, user["id"]),
        )
        updated = dict(conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone())
    return {"user": _serialize_user(updated)}


@api_router.get("/stream", tags=["Real-Time SSE"])
async def sse_stream(user: dict[str, Any] = Depends(verify_token)) -> StreamingResponse:
    """Singleton Server-Sent Events stream for Delta-Only Zero-Flicker updates."""
    return StreamingResponse(
        sse_manager.subscribe(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# =====================================================================
# 2. USERS & API TOKENS
# =====================================================================
@api_router.get("/users", tags=["Auth & Users"])
async def list_users(user: dict[str, Any] = Depends(verify_token)) -> dict[str, Any]:
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
        items = [_serialize_user(dict(r)) for r in rows]
    return {"count": len(items), "items": items}


@api_router.post("/users", status_code=201, tags=["Auth & Users"])
async def create_user(
    payload: UserCreateRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_users")
    now = settings.now_iso()
    uid = str(uuid.uuid4())
    with db_session() as conn:
        exists = conn.execute(
            "SELECT 1 FROM users WHERE username = ? OR email = ?",
            (payload.username.strip(), payload.email.strip()),
        ).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="Username ou e-mail já cadastrado.")
        conn.execute(
            """
            INSERT INTO users (
                id, username, email, full_name, password_hash, role,
                permissions_json, digest_enabled, digest_times_json, timezone, is_active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                uid,
                payload.username.strip(),
                payload.email.strip(),
                payload.full_name.strip(),
                hash_password(payload.password),
                payload.role,
                json.dumps(payload.permissions),
                1 if payload.digest_enabled else 0,
                json.dumps(payload.digest_times),
                settings.TZ_NAME,
                now,
            ),
        )
        created = dict(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())
    return {"user": _serialize_user(created)}


@api_router.patch("/users/{target_id}", tags=["Auth & Users"])
async def update_user(
    target_id: str,
    payload: UserUpdateRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_users")
    with db_session() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (target_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Usuário não encontrado.")
        curr = dict(row)

        email = payload.email.strip() if payload.email is not None else curr["email"]
        full_name = payload.full_name.strip() if payload.full_name is not None else curr["full_name"]
        pwd_hash = hash_password(payload.password) if payload.password else curr["password_hash"]
        role = payload.role if payload.role is not None else curr["role"]
        perms_json = (
            json.dumps(payload.permissions)
            if payload.permissions is not None
            else curr["permissions_json"]
        )
        dig_en = (
            (1 if payload.digest_enabled else 0)
            if payload.digest_enabled is not None
            else curr["digest_enabled"]
        )
        dig_times = (
            json.dumps(payload.digest_times)
            if payload.digest_times is not None
            else curr["digest_times_json"]
        )
        is_act = (
            (1 if payload.is_active else 0)
            if payload.is_active is not None
            else curr["is_active"]
        )

        conn.execute(
            """
            UPDATE users SET
                email = ?, full_name = ?, password_hash = ?, role = ?,
                permissions_json = ?, digest_enabled = ?, digest_times_json = ?, is_active = ?
            WHERE id = ?
            """,
            (email, full_name, pwd_hash, role, perms_json, dig_en, dig_times, is_act, target_id),
        )
        updated = dict(conn.execute("SELECT * FROM users WHERE id = ?", (target_id,)).fetchone())
    return {"user": _serialize_user(updated)}


@api_router.delete("/users/{target_id}", tags=["Auth & Users"])
async def delete_user(
    target_id: str,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_users")
    if target_id == user["id"]:
        raise HTTPException(status_code=400, detail="Não é permitido excluir o próprio usuário logado.")
    with db_session() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (target_id,))
    return {"deleted": True, "id": target_id}


@api_router.get("/tokens", tags=["API Tokens"])
async def list_api_tokens(user: dict[str, Any] = Depends(verify_token)) -> dict[str, Any]:
    with db_session() as conn:
        if user["role"] == "admin":
            rows = conn.execute(
                """
                SELECT t.id, t.user_id, u.username, t.name, t.token_prefix, t.scopes_json,
                       t.last_used_at, t.expires_at, t.created_at
                FROM api_tokens t
                JOIN users u ON u.id = t.user_id
                ORDER BY t.created_at DESC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT t.id, t.user_id, u.username, t.name, t.token_prefix, t.scopes_json,
                       t.last_used_at, t.expires_at, t.created_at
                FROM api_tokens t
                JOIN users u ON u.id = t.user_id
                WHERE t.user_id = ?
                ORDER BY t.created_at DESC
                """,
                (user["id"],),
            ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            try:
                d["scopes"] = json.loads(d.get("scopes_json") or '["*"]')
            except Exception:
                d["scopes"] = ["*"]
            items.append(d)
    return {"count": len(items), "items": items}


@api_router.post("/tokens", status_code=201, tags=["API Tokens"])
async def create_api_token_endpoint(
    payload: ApiTokenCreateRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    raw_token, token_hash, prefix = generate_api_token()
    tid = str(uuid.uuid4())
    now = settings.now_iso()
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO api_tokens (id, user_id, name, token_hash, token_prefix, scopes_json, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tid,
                user["id"],
                payload.name.strip(),
                token_hash,
                prefix,
                json.dumps(payload.scopes),
                payload.expires_at,
                now,
            ),
        )
    return {
        "id": tid,
        "name": payload.name.strip(),
        "token": raw_token,
        "token_prefix": prefix,
        "scopes": payload.scopes,
        "created_at": now,
    }


@api_router.delete("/tokens/{token_id}", tags=["API Tokens"])
async def revoke_api_token(
    token_id: str,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    with db_session() as conn:
        if user["role"] == "admin":
            conn.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
        else:
            conn.execute(
                "DELETE FROM api_tokens WHERE id = ? AND user_id = ?",
                (token_id, user["id"]),
            )
    return {"revoked": True, "id": token_id}


# =====================================================================
# 3. PROJECTS & PROJECT PERMISSIONS
# =====================================================================
@api_router.get("/projects", tags=["Projects"])
async def list_projects(user: dict[str, Any] = Depends(verify_token)) -> dict[str, Any]:
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY created_at ASC").fetchall()
        projects = []
        for r in rows:
            p = dict(r)
            p["is_archived"] = bool(p["is_archived"])
            members = [
                dict(m)
                for m in conn.execute(
                    """
                    SELECT pm.*, u.username, u.full_name
                    FROM project_members pm
                    JOIN users u ON u.id = pm.user_id
                    WHERE pm.project_id = ?
                    """,
                    (p["id"],),
                ).fetchall()
            ]
            for m in members:
                try:
                    m["permissions"] = json.loads(m.get("permissions_json") or "{}")
                except Exception:
                    m["permissions"] = {}
            p["members"] = members
            projects.append(p)
    return {"count": len(projects), "items": projects}


@api_router.post("/projects", status_code=201, tags=["Projects"])
async def create_project(
    payload: ProjectCreateRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_projects")
    pid = str(uuid.uuid4())
    now = settings.now_iso()
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO projects (id, name, description, color, icon, is_archived, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                pid,
                payload.name.strip(),
                payload.description.strip(),
                payload.color,
                payload.icon,
                user["id"],
                now,
            ),
        )
        proj = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone())
    sse_manager.broadcast_delta("project.created", proj)
    return {"project": proj}


@api_router.patch("/projects/{project_id}", tags=["Projects"])
async def update_project(
    project_id: str,
    payload: ProjectUpdateRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_projects", project_id)
    with db_session() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Projeto não encontrado.")
        curr = dict(row)
        conn.execute(
            """
            UPDATE projects SET
                name = ?, description = ?, color = ?, icon = ?, is_archived = ?
            WHERE id = ?
            """,
            (
                payload.name.strip() if payload.name is not None else curr["name"],
                payload.description if payload.description is not None else curr["description"],
                payload.color if payload.color is not None else curr["color"],
                payload.icon if payload.icon is not None else curr["icon"],
                (1 if payload.is_archived else 0) if payload.is_archived is not None else curr["is_archived"],
                project_id,
            ),
        )
        updated = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())
    sse_manager.broadcast_delta("project.updated", updated)
    return {"project": updated}


@api_router.delete("/projects/{project_id}", tags=["Projects"])
async def delete_project(
    project_id: str,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_projects", project_id)
    with db_session() as conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    sse_manager.broadcast_delta("project.deleted", {"id": project_id})
    return {"deleted": True, "id": project_id}


@api_router.post("/projects/{project_id}/members", tags=["Projects"])
async def set_project_member_permissions(
    project_id: str,
    payload: ProjectMemberRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_projects", project_id)
    now = settings.now_iso()
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO project_members (id, project_id, user_id, role, permissions_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, user_id) DO UPDATE SET
                role = excluded.role,
                permissions_json = excluded.permissions_json
            """,
            (
                str(uuid.uuid4()),
                project_id,
                payload.user_id,
                payload.role,
                json.dumps(payload.permissions),
                now,
            ),
        )
    return {
        "project_id": project_id,
        "user_id": payload.user_id,
        "role": payload.role,
        "permissions": payload.permissions,
    }


# =====================================================================
# 4. STATUSES & PRIORITIES
# =====================================================================
@api_router.get("/priorities", tags=["Statuses & Priorities"])
async def list_priorities(user: dict[str, Any] = Depends(verify_token)) -> dict[str, Any]:
    return {
        "items": [
            {"code": "P1", "name": "Urgente", "color": "#ef4444", "rank": 1},
            {"code": "P2", "name": "Alta", "color": "#f97316", "rank": 2},
            {"code": "P3", "name": "Normal", "color": "#ec4899", "rank": 3},
            {"code": "P4", "name": "Baixa", "color": "#a855f7", "rank": 4},
        ]
    }


@api_router.get("/statuses", tags=["Statuses & Priorities"])
async def list_statuses(user: dict[str, Any] = Depends(verify_token)) -> dict[str, Any]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM statuses ORDER BY position ASC, created_at ASC"
        ).fetchall()
        items = [dict(r) for r in rows]
        for s in items:
            s["is_system"] = bool(s["is_system"])
    return {"count": len(items), "items": items}


@api_router.post("/statuses", status_code=201, tags=["Statuses & Priorities"])
async def create_status(
    payload: StatusCreateRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_projects")
    slug = (payload.slug or _slugify(payload.name)).strip()
    now = settings.now_iso()
    sid = str(uuid.uuid4())
    with db_session() as conn:
        exists = conn.execute("SELECT 1 FROM statuses WHERE slug = ?", (slug,)).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail=f"Status '{slug}' já existe.")
        conn.execute(
            """
            INSERT INTO statuses (id, slug, name, color, category, position, is_system, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (sid, slug, payload.name.strip(), payload.color, payload.category, payload.position, now),
        )
        st = dict(conn.execute("SELECT * FROM statuses WHERE id = ?", (sid,)).fetchone())
    sse_manager.broadcast_delta("status.created", st)
    return {"status": st}


@api_router.patch("/statuses/{status_id}", tags=["Statuses & Priorities"])
async def update_status(
    status_id: str,
    payload: StatusUpdateRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_projects")
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM statuses WHERE id = ? OR slug = ?", (status_id, status_id)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Status não encontrado.")
        curr = dict(row)
        conn.execute(
            """
            UPDATE statuses SET name = ?, color = ?, category = ?, position = ?
            WHERE id = ?
            """,
            (
                payload.name.strip() if payload.name is not None else curr["name"],
                payload.color if payload.color is not None else curr["color"],
                payload.category if payload.category is not None else curr["category"],
                payload.position if payload.position is not None else curr["position"],
                curr["id"],
            ),
        )
        updated = dict(conn.execute("SELECT * FROM statuses WHERE id = ?", (curr["id"],)).fetchone())
    sse_manager.broadcast_delta("status.updated", updated)
    return {"status": updated}


@api_router.delete("/statuses/{status_id}", tags=["Statuses & Priorities"])
async def delete_status(
    status_id: str,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_projects")
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM statuses WHERE id = ? OR slug = ?", (status_id, status_id)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Status não encontrado.")
        curr = dict(row)
        if curr["is_system"]:
            raise HTTPException(
                status_code=400,
                detail="Status padrão do sistema não pode ser excluído.",
            )
        # Reassign any tasks using this status back to 'pending'
        conn.execute(
            "UPDATE tasks SET status_slug = 'pending' WHERE status_slug = ?",
            (curr["slug"],),
        )
        conn.execute("DELETE FROM statuses WHERE id = ?", (curr["id"],))
    sse_manager.broadcast_delta("status.deleted", {"id": curr["id"], "slug": curr["slug"]})
    return {"deleted": True, "slug": curr["slug"]}


# =====================================================================
# 5. CUSTOM FIELDS
# =====================================================================
VALID_FIELD_TYPES = {
    "short_text",
    "long_text",
    "number",
    "currency",
    "date",
    "datetime",
    "checkbox",
    "select",
    "multi_select",
    "url",
}


def _serialize_custom_field(r: dict[str, Any]) -> dict[str, Any]:
    try:
        def_val = (
            json.loads(r["default_value_json"])
            if r.get("default_value_json") is not None
            else None
        )
    except Exception:
        def_val = r.get("default_value_json")
    try:
        opts = json.loads(r.get("options_json") or "[]")
    except Exception:
        opts = []
    return {
        "id": r["id"],
        "key": r["key"],
        "name": r["name"],
        "field_type": r["field_type"],
        "required": bool(r["required"]),
        "default_value": def_val,
        "options": opts,
        "position": r["position"],
        "is_active": bool(r["is_active"]),
        "created_at": r["created_at"],
    }


@api_router.get("/custom-fields", tags=["Custom Fields"])
async def list_custom_fields(user: dict[str, Any] = Depends(verify_token)) -> dict[str, Any]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM custom_fields WHERE is_active = 1 ORDER BY position ASC, created_at ASC"
        ).fetchall()
        items = [_serialize_custom_field(dict(r)) for r in rows]
    return {"count": len(items), "items": items}


@api_router.post("/custom-fields", status_code=201, tags=["Custom Fields"])
async def create_custom_field(
    payload: CustomFieldCreateRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_fields")
    if payload.field_type not in VALID_FIELD_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Tipo de campo inválido. Use um de: {sorted(VALID_FIELD_TYPES)}",
        )
    key = (payload.key or _slugify(payload.name)).strip()
    fid = str(uuid.uuid4())
    now = settings.now_iso()
    with db_session() as conn:
        exists = conn.execute("SELECT 1 FROM custom_fields WHERE key = ?", (key,)).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail=f"Campo personalizado '{key}' já existe.")
        conn.execute(
            """
            INSERT INTO custom_fields (
                id, key, name, field_type, required, default_value_json, options_json, position, is_active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                fid,
                key,
                payload.name.strip(),
                payload.field_type,
                1 if payload.required else 0,
                json.dumps(payload.default_value, ensure_ascii=False) if payload.default_value is not None else None,
                json.dumps(payload.options, ensure_ascii=False),
                payload.position,
                now,
            ),
        )
        row = dict(conn.execute("SELECT * FROM custom_fields WHERE id = ?", (fid,)).fetchone())
    cf = _serialize_custom_field(row)
    sse_manager.broadcast_delta("custom_field.created", cf)
    return {"custom_field": cf}


@api_router.patch("/custom-fields/{field_id}", tags=["Custom Fields"])
async def update_custom_field(
    field_id: str,
    payload: CustomFieldUpdateRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_fields")
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM custom_fields WHERE id = ? OR key = ?", (field_id, field_id)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Campo personalizado não encontrado.")
        curr = dict(row)
        conn.execute(
            """
            UPDATE custom_fields SET
                name = ?, field_type = ?, required = ?, default_value_json = ?,
                options_json = ?, position = ?, is_active = ?
            WHERE id = ?
            """,
            (
                payload.name.strip() if payload.name is not None else curr["name"],
                payload.field_type if payload.field_type is not None else curr["field_type"],
                (1 if payload.required else 0) if payload.required is not None else curr["required"],
                json.dumps(payload.default_value, ensure_ascii=False)
                if payload.default_value is not None
                else curr["default_value_json"],
                json.dumps(payload.options, ensure_ascii=False)
                if payload.options is not None
                else curr["options_json"],
                payload.position if payload.position is not None else curr["position"],
                (1 if payload.is_active else 0) if payload.is_active is not None else curr["is_active"],
                curr["id"],
            ),
        )
        updated = dict(conn.execute("SELECT * FROM custom_fields WHERE id = ?", (curr["id"],)).fetchone())
    cf = _serialize_custom_field(updated)
    sse_manager.broadcast_delta("custom_field.updated", cf)
    return {"custom_field": cf}


@api_router.delete("/custom-fields/{field_id}", tags=["Custom Fields"])
async def delete_custom_field(
    field_id: str,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_fields")
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM custom_fields WHERE id = ? OR key = ?", (field_id, field_id)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Campo personalizado não encontrado.")
        conn.execute("DELETE FROM custom_fields WHERE id = ?", (row["id"],))
    sse_manager.broadcast_delta("custom_field.deleted", {"id": row["id"], "key": row["key"]})
    return {"deleted": True, "id": row["id"]}


# =====================================================================
# 6. TASKS, SUBTASKS, COMMENTS, ATTACHMENTS & ADVANCED FILTERING
# =====================================================================
@api_router.get("/tasks", tags=["Tasks"])
async def list_tasks_endpoint(
    search: str = Query(default="", description="Pesquisa global rápida"),
    status_slug: str | None = Query(default=None),
    priority: str | None = Query(default=None),
    project_ids: str | None = Query(
        default=None, description="IDs de projetos separados por vírgula (multi-projeto)"
    ),
    assignee_id: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    due_preset: str | None = Query(
        default=None, description="today, overdue, next_7_days, no_date"
    ),
    filters_json: str | None = Query(
        default=None, description="Árvore JSON completa de filtros E/OU"
    ),
    sorts_json: str | None = Query(default=None, description="Lista JSON de ordenações"),
    group_by: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=1000),
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "view")
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM tasks").fetchall()
        all_tasks = hydrate_tasks(conn, rows)

    conditions: list[dict[str, Any]] = []
    if status_slug:
        conditions.append({"field": "status_slug", "op": "in", "value": status_slug.split(",")})
    if priority:
        conditions.append({"field": "priority", "op": "in", "value": priority.split(",")})
    if assignee_id:
        conditions.append({"field": "assignee_id", "op": "eq", "value": assignee_id})
    if tag:
        conditions.append({"field": "tags", "op": "contains", "value": tag})
    if due_preset == "today":
        conditions.append({"field": "is_today", "op": "is_true", "value": True})
    elif due_preset == "overdue":
        conditions.append({"field": "due_date", "op": "is_overdue", "value": None})
    elif due_preset == "next_7_days":
        conditions.append({"field": "due_date", "op": "next_7_days", "value": None})
    elif due_preset == "no_date":
        conditions.append({"field": "due_date", "op": "is_empty", "value": None})

    filter_tree: dict[str, Any] = {"logic": "AND", "conditions": conditions, "groups": []}
    if filters_json:
        try:
            parsed_tree = json.loads(filters_json)
            if isinstance(parsed_tree, dict):
                if conditions:
                    filter_tree = {
                        "logic": "AND",
                        "conditions": conditions,
                        "groups": [parsed_tree],
                    }
                else:
                    filter_tree = parsed_tree
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"filters_json inválido: {exc}")

    sorts = None
    if sorts_json:
        try:
            sorts = json.loads(sorts_json)
        except Exception:
            sorts = None

    proj_list = [p.strip() for p in project_ids.split(",") if p.strip()] if project_ids else None

    return filter_and_paginate_tasks(
        all_tasks=all_tasks,
        user=user,
        filter_tree=filter_tree,
        search_query=search,
        project_ids=proj_list,
        sorts=sorts,
        group_by=group_by,
        page=page,
        page_size=page_size,
    )


@api_router.post("/tasks/query", tags=["Tasks"])
async def query_tasks_endpoint(
    payload: TaskQueryRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    """Advanced POST query endpoint for complex multi-project nested AND/OR filters."""
    check_permission(user, "view")
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM tasks").fetchall()
        all_tasks = hydrate_tasks(conn, rows)

    return filter_and_paginate_tasks(
        all_tasks=all_tasks,
        user=user,
        filter_tree=payload.filter_tree,
        search_query=payload.search,
        project_ids=payload.project_ids,
        sorts=payload.sorts,
        group_by=payload.group_by,
        page=payload.page,
        page_size=payload.page_size,
    )


@api_router.post("/tasks", status_code=201, tags=["Tasks"])
async def create_task_endpoint(
    payload: TaskCreateRequest,
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "create", payload.project_id)
    task, events = create_task_workflow(payload.model_dump(), user)
    background_tasks.add_task(_bg_dispatch_events, events, task)
    return {"task": task}


@api_router.get("/tasks/{task_id}", tags=["Tasks"])
async def get_task_endpoint(
    task_id: str,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    task = get_task_detail_workflow(task_id)
    if not can_user_access_task(user, task):
        raise HTTPException(status_code=403, detail="Você não tem permissão para visualizar esta tarefa.")
    return {"task": task}


@api_router.patch("/tasks/{task_id}", tags=["Tasks"])
async def update_task_endpoint(
    task_id: str,
    payload: TaskUpdateRequest,
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    existing = get_task_detail_workflow(task_id)
    if not can_user_access_task(user, existing):
        raise HTTPException(status_code=403, detail="Sem acesso a esta tarefa.")

    patch_data = payload.model_dump(exclude_unset=True)
    # If only completing/reopening task, check 'complete' permission; otherwise 'edit'
    if set(patch_data.keys()) == {"status_slug"} and patch_data.get("status_slug") == "completed":
        check_permission(user, "complete", existing.get("project_id"))
    else:
        check_permission(user, "edit", existing.get("project_id"))

    updated, events = update_task_workflow(task_id, patch_data, user)
    background_tasks.add_task(_bg_dispatch_events, events, updated)
    return {"task": updated}


@api_router.delete("/tasks/{task_id}", tags=["Tasks"])
async def delete_task_endpoint(
    task_id: str,
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    existing = get_task_detail_workflow(task_id)
    if not can_user_access_task(user, existing):
        raise HTTPException(status_code=403, detail="Sem acesso a esta tarefa.")
    check_permission(user, "delete", existing.get("project_id"))

    deleted_task = delete_task_workflow(task_id, user)
    background_tasks.add_task(_bg_dispatch_events, ["task.deleted"], deleted_task)
    return {"deleted": True, "id": task_id}


# --- SUBTASKS ---
@api_router.post("/tasks/{task_id}/subtasks", status_code=201, tags=["Subtasks & Checklists"])
async def create_subtask(
    task_id: str,
    payload: SubtaskCreateRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    existing = get_task_detail_workflow(task_id)
    check_permission(user, "edit", existing.get("project_id"))
    sid = str(uuid.uuid4())
    now = settings.now_iso()
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO subtasks (id, task_id, title, is_completed, position, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (sid, task_id, payload.title.strip(), 1 if payload.is_completed else 0, payload.position, now),
        )
        record_task_history(conn, task_id, user, "subtask_added", {"subtask": payload.title.strip()})
    updated_task = get_task_detail_workflow(task_id)
    sse_manager.broadcast_delta("task.updated", {"id": task_id, "task": updated_task})
    return {"task": updated_task}


@api_router.patch("/tasks/{task_id}/subtasks/{subtask_id}", tags=["Subtasks & Checklists"])
async def update_subtask(
    task_id: str,
    subtask_id: str,
    payload: SubtaskUpdateRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    existing = get_task_detail_workflow(task_id)
    check_permission(user, "complete", existing.get("project_id"))
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM subtasks WHERE id = ? AND task_id = ?", (subtask_id, task_id)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Subtarefa não encontrada.")
        curr = dict(row)
        conn.execute(
            """
            UPDATE subtasks SET title = ?, is_completed = ?, position = ?
            WHERE id = ?
            """,
            (
                payload.title.strip() if payload.title is not None else curr["title"],
                (1 if payload.is_completed else 0) if payload.is_completed is not None else curr["is_completed"],
                payload.position if payload.position is not None else curr["position"],
                subtask_id,
            ),
        )
    updated_task = get_task_detail_workflow(task_id)
    sse_manager.broadcast_delta("task.updated", {"id": task_id, "task": updated_task})
    return {"task": updated_task}


@api_router.delete("/tasks/{task_id}/subtasks/{subtask_id}", tags=["Subtasks & Checklists"])
async def delete_subtask(
    task_id: str,
    subtask_id: str,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    existing = get_task_detail_workflow(task_id)
    check_permission(user, "edit", existing.get("project_id"))
    with db_session() as conn:
        conn.execute("DELETE FROM subtasks WHERE id = ? AND task_id = ?", (subtask_id, task_id))
    updated_task = get_task_detail_workflow(task_id)
    sse_manager.broadcast_delta("task.updated", {"id": task_id, "task": updated_task})
    return {"task": updated_task}


# --- COMMENTS ---
@api_router.post("/tasks/{task_id}/comments", status_code=201, tags=["Comments & Attachments"])
async def add_task_comment(
    task_id: str,
    payload: CommentCreateRequest,
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    existing = get_task_detail_workflow(task_id)
    if not can_user_access_task(user, existing):
        raise HTTPException(status_code=403, detail="Sem acesso a esta tarefa.")
    cid = str(uuid.uuid4())
    now = settings.now_iso()
    author = user.get("full_name") or user.get("username") or "Usuário"
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO task_comments (id, task_id, user_id, author_name, content, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (cid, task_id, user["id"], author, payload.content.strip(), now),
        )
        record_task_history(
            conn, task_id, user, "comment_added", {"comment": payload.content.strip()[:120]}
        )
    updated_task = get_task_detail_workflow(task_id)
    sse_manager.broadcast_delta("task.updated", {"id": task_id, "task": updated_task})
    background_tasks.add_task(
        _bg_dispatch_events,
        ["task.comment_created"],
        {"task": updated_task, "comment": {"id": cid, "author": author, "content": payload.content.strip()}},
    )
    return {"task": updated_task}


# --- ATTACHMENTS ---
@api_router.post("/tasks/{task_id}/attachments", status_code=201, tags=["Comments & Attachments"])
async def upload_task_attachment(
    task_id: str,
    file: UploadFile = File(...),
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    existing = get_task_detail_workflow(task_id)
    check_permission(user, "edit", existing.get("project_id"))

    content = await file.read()
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Tamanho máximo de anexo é 25MB.")

    saved = storage_client.save_attachment(file.filename or "anexo.bin", content)
    aid = str(uuid.uuid4())
    now = settings.now_iso()
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO task_attachments (id, task_id, user_id, filename, stored_name, mime_type, size_bytes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                aid,
                task_id,
                user["id"],
                file.filename or "anexo.bin",
                saved["stored_name"],
                file.content_type or "application/octet-stream",
                saved["size_bytes"],
                now,
            ),
        )
        record_task_history(
            conn, task_id, user, "attachment_added", {"filename": file.filename}
        )
    updated_task = get_task_detail_workflow(task_id)
    sse_manager.broadcast_delta("task.updated", {"id": task_id, "task": updated_task})
    return {"task": updated_task}


@api_router.get("/attachments/{attachment_id}/download", tags=["Comments & Attachments"])
async def download_task_attachment(
    attachment_id: str,
    user: dict[str, Any] = Depends(verify_token),
) -> FileResponse:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM task_attachments WHERE id = ?", (attachment_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Anexo não encontrado.")
        att = dict(row)

    task = get_task_detail_workflow(att["task_id"])
    if not can_user_access_task(user, task):
        raise HTTPException(status_code=403, detail="Sem permissão para baixar este anexo.")

    fpath = storage_client.get_attachment_path(att["stored_name"])
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="Arquivo físico não encontrado no disco.")
    return FileResponse(
        path=str(fpath),
        filename=att["filename"],
        media_type=att["mime_type"],
    )


@api_router.delete("/tasks/{task_id}/attachments/{attachment_id}", tags=["Comments & Attachments"])
async def delete_task_attachment(
    task_id: str,
    attachment_id: str,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    existing = get_task_detail_workflow(task_id)
    check_permission(user, "edit", existing.get("project_id"))
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM task_attachments WHERE id = ? AND task_id = ?",
            (attachment_id, task_id),
        ).fetchone()
        if row:
            storage_client.delete_attachment(row["stored_name"])
            conn.execute("DELETE FROM task_attachments WHERE id = ?", (attachment_id,))
    updated_task = get_task_detail_workflow(task_id)
    sse_manager.broadcast_delta("task.updated", {"id": task_id, "task": updated_task})
    return {"task": updated_task}


# =====================================================================
# 7. SAVED VIEWS (Table, Kanban, List + Unlimited Custom Views)
# =====================================================================
def _serialize_view(r: dict[str, Any]) -> dict[str, Any]:
    def _j(k: str, default: Any) -> Any:
        try:
            return json.loads(r.get(k) or "")
        except Exception:
            return default

    return {
        "id": r["id"],
        "name": r["name"],
        "owner_id": r.get("owner_id"),
        "view_type": r["view_type"],
        "visible_columns": _j("visible_columns_json", []),
        "column_order": _j("column_order_json", []),
        "filters": _j("filters_json", {"logic": "AND", "conditions": [], "groups": []}),
        "sorts": _j("sorts_json", []),
        "group_by": r.get("group_by") or "",
        "display_prefs": _j("display_prefs_json", {}),
        "is_default": bool(r.get("is_default", 0)),
        "is_shared": bool(r.get("is_shared", 0)),
        "shared_user_ids": _j("shared_user_ids_json", []),
        "is_system_preset": bool(r.get("is_system_preset", 0)),
        "created_at": r.get("created_at"),
    }


@api_router.get("/views", tags=["Saved Views"])
async def list_saved_views(user: dict[str, Any] = Depends(verify_token)) -> dict[str, Any]:
    uid = user["id"]
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM saved_views ORDER BY is_system_preset DESC, created_at ASC").fetchall()
        views = []
        for r in rows:
            v = _serialize_view(dict(r))
            if (
                v["owner_id"] == uid
                or v["is_shared"]
                or uid in v["shared_user_ids"]
                or user["role"] == "admin"
            ):
                views.append(v)
    return {"count": len(views), "items": views}


@api_router.post("/views", status_code=201, tags=["Saved Views"])
async def create_saved_view(
    payload: SavedViewRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    vid = str(uuid.uuid4())
    now = settings.now_iso()
    with db_session() as conn:
        if payload.is_default:
            conn.execute("UPDATE saved_views SET is_default = 0 WHERE owner_id = ?", (user["id"],))
        conn.execute(
            """
            INSERT INTO saved_views (
                id, name, owner_id, view_type, visible_columns_json, column_order_json,
                filters_json, sorts_json, group_by, display_prefs_json,
                is_default, is_shared, shared_user_ids_json, is_system_preset, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                vid,
                payload.name.strip(),
                user["id"],
                payload.view_type,
                json.dumps(payload.visible_columns, ensure_ascii=False),
                json.dumps(payload.column_order, ensure_ascii=False),
                json.dumps(payload.filters, ensure_ascii=False),
                json.dumps(payload.sorts, ensure_ascii=False),
                payload.group_by,
                json.dumps(payload.display_prefs, ensure_ascii=False),
                1 if payload.is_default else 0,
                1 if payload.is_shared else 0,
                json.dumps(payload.shared_user_ids, ensure_ascii=False),
                now,
            ),
        )
        row = dict(conn.execute("SELECT * FROM saved_views WHERE id = ?", (vid,)).fetchone())
    v = _serialize_view(row)
    sse_manager.broadcast_delta("view.created", v)
    return {"view": v}


@api_router.patch("/views/{view_id}", tags=["Saved Views"])
async def update_saved_view(
    view_id: str,
    payload: SavedViewRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM saved_views WHERE id = ?", (view_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Visualização não encontrada.")
        curr = dict(row)
        if curr["owner_id"] != user["id"] and user["role"] != "admin" and not curr["is_shared"]:
            raise HTTPException(status_code=403, detail="Sem permissão para editar esta visualização.")

        if payload.is_default:
            conn.execute("UPDATE saved_views SET is_default = 0")

        conn.execute(
            """
            UPDATE saved_views SET
                name = ?, view_type = ?, visible_columns_json = ?, column_order_json = ?,
                filters_json = ?, sorts_json = ?, group_by = ?, display_prefs_json = ?,
                is_default = ?, is_shared = ?, shared_user_ids_json = ?
            WHERE id = ?
            """,
            (
                payload.name.strip(),
                payload.view_type,
                json.dumps(payload.visible_columns, ensure_ascii=False),
                json.dumps(payload.column_order, ensure_ascii=False),
                json.dumps(payload.filters, ensure_ascii=False),
                json.dumps(payload.sorts, ensure_ascii=False),
                payload.group_by,
                json.dumps(payload.display_prefs, ensure_ascii=False),
                1 if payload.is_default else 0,
                1 if payload.is_shared else 0,
                json.dumps(payload.shared_user_ids, ensure_ascii=False),
                view_id,
            ),
        )
        updated = dict(conn.execute("SELECT * FROM saved_views WHERE id = ?", (view_id,)).fetchone())
    v = _serialize_view(updated)
    sse_manager.broadcast_delta("view.updated", v)
    return {"view": v}


@api_router.post("/views/{view_id}/duplicate", status_code=201, tags=["Saved Views"])
async def duplicate_saved_view(
    view_id: str,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM saved_views WHERE id = ?", (view_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Visualização não encontrada.")
        curr = dict(row)
        new_id = str(uuid.uuid4())
        now = settings.now_iso()
        conn.execute(
            """
            INSERT INTO saved_views (
                id, name, owner_id, view_type, visible_columns_json, column_order_json,
                filters_json, sorts_json, group_by, display_prefs_json,
                is_default, is_shared, shared_user_ids_json, is_system_preset, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?)
            """,
            (
                new_id,
                f"{curr['name']} (Cópia)",
                user["id"],
                curr["view_type"],
                curr["visible_columns_json"],
                curr["column_order_json"],
                curr["filters_json"],
                curr["sorts_json"],
                curr["group_by"],
                curr["display_prefs_json"],
                curr["is_shared"],
                curr["shared_user_ids_json"],
                now,
            ),
        )
        dup = dict(conn.execute("SELECT * FROM saved_views WHERE id = ?", (new_id,)).fetchone())
    v = _serialize_view(dup)
    sse_manager.broadcast_delta("view.created", v)
    return {"view": v}


@api_router.delete("/views/{view_id}", tags=["Saved Views"])
async def delete_saved_view(
    view_id: str,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM saved_views WHERE id = ?", (view_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Visualização não encontrada.")
        if row["owner_id"] != user["id"] and user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Apenas o criador ou administrador pode excluir.")
        conn.execute("DELETE FROM saved_views WHERE id = ?", (view_id,))
    sse_manager.broadcast_delta("view.deleted", {"id": view_id})
    return {"deleted": True, "id": view_id}


# =====================================================================
# 8. PWA, WEB PUSH VAPID, REMINDERS & DIGESTS
# =====================================================================
@api_router.get("/push/vapid-public-key", tags=["PWA & Notifications"])
async def get_vapid_public_key() -> dict[str, str]:
    return {"public_key": webpush_client.public_key}


@api_router.post("/push/subscribe", status_code=201, tags=["PWA & Notifications"])
async def subscribe_push_device(
    payload: PushSubscriptionRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    p256dh = payload.keys.get("p256dh", "")
    auth = payload.keys.get("auth", "")
    if not payload.endpoint or not p256dh or not auth:
        raise HTTPException(status_code=422, detail="Dados de assinatura Web Push incompletos.")
    sid = str(uuid.uuid4())
    now = settings.now_iso()
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO push_subscriptions (id, user_id, endpoint, p256dh, auth, user_agent, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                user_id = excluded.user_id,
                p256dh = excluded.p256dh,
                auth = excluded.auth,
                user_agent = excluded.user_agent
            """,
            (sid, user["id"], payload.endpoint, p256dh, auth, payload.user_agent, now),
        )
    return {"subscribed": True, "endpoint": payload.endpoint}


@api_router.delete("/push/unsubscribe", tags=["PWA & Notifications"])
async def unsubscribe_push_device(
    endpoint: str = Query(...),
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    with db_session() as conn:
        conn.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?",
            (endpoint, user["id"]),
        )
    return {"unsubscribed": True}


@api_router.post("/push/test", tags=["PWA & Notifications"])
async def send_test_notification(user: dict[str, Any] = Depends(verify_token)) -> dict[str, Any]:
    res = send_user_notification(
        user_id=user["id"],
        title="🚀 TaskForge Web Push Ativo!",
        body=f"Notificação de teste enviada às {settings.now().strftime('%H:%M:%S')} ({settings.TZ_NAME}).",
        category="test",
    )
    return res


@api_router.get("/notifications", tags=["PWA & Notifications"])
async def list_notifications(
    limit: int = Query(default=50, ge=1, le=200),
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM notification_logs WHERE user_id = ? ORDER BY sent_at DESC LIMIT ?",
            (user["id"], limit),
        ).fetchall()
        items = [dict(r) for r in rows]
        for n in items:
            n["is_read"] = bool(n["is_read"])
    summary = build_user_digest_summary(user["id"])
    return {
        "count": len(items),
        "unread_count": sum(1 for n in items if not n["is_read"]),
        "summary": {
            "overdue_count": len(summary["overdue"]),
            "today_count": len(summary["today"]),
            "upcoming_count": len(summary["upcoming"]),
            "no_date_count": len(summary["no_date"]),
            "details": summary,
        },
        "items": items,
    }


@api_router.post("/notifications/read-all", tags=["PWA & Notifications"])
async def mark_all_notifications_read(user: dict[str, Any] = Depends(verify_token)) -> dict[str, Any]:
    with db_session() as conn:
        conn.execute("UPDATE notification_logs SET is_read = 1 WHERE user_id = ?", (user["id"],))
    return {"marked_read": True}


@api_router.post("/notifications/trigger-digest", tags=["PWA & Notifications"])
async def trigger_digest_now(user: dict[str, Any] = Depends(verify_token)) -> dict[str, Any]:
    slot = settings.now().strftime("%H:%M")
    return run_scheduled_digests_workflow(force_slot=slot)


@api_router.get("/reminders", tags=["PWA & Notifications"])
async def list_reminders(
    task_id: str | None = Query(default=None),
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    with db_session() as conn:
        if task_id:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE task_id = ? ORDER BY remind_at ASC",
                (task_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE user_id = ? ORDER BY remind_at ASC",
                (user["id"],),
            ).fetchall()
        items = [dict(r) for r in rows]
    return {"count": len(items), "items": items}


@api_router.post("/reminders", status_code=201, tags=["PWA & Notifications"])
async def create_reminder(
    payload: ReminderCreateRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    rid = str(uuid.uuid4())
    now = settings.now_iso()
    target_user = payload.user_id or user["id"]
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO reminders (id, task_id, user_id, remind_at, message, channel, is_sent, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (rid, payload.task_id, target_user, payload.remind_at, payload.message, payload.channel, now),
        )
        rem = dict(conn.execute("SELECT * FROM reminders WHERE id = ?", (rid,)).fetchone())
    # Immediately check if reminder is already due
    process_due_reminders_workflow()
    return {"reminder": rem}


@api_router.delete("/reminders/{reminder_id}", tags=["PWA & Notifications"])
async def delete_reminder(
    reminder_id: str,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    with db_session() as conn:
        conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    return {"deleted": True, "id": reminder_id}


# =====================================================================
# 9. CONFIGURABLE OUTBOUND WEBHOOKS
# =====================================================================
def _serialize_webhook(r: dict[str, Any]) -> dict[str, Any]:
    try:
        events = json.loads(r.get("events_json") or "[]")
    except Exception:
        events = []
    try:
        projects = json.loads(r.get("project_ids_json") or "[]")
    except Exception:
        projects = []
    return {
        "id": r["id"],
        "name": r["name"],
        "description": r.get("description") or "",
        "target_url": r["target_url"],
        "events": events,
        "project_ids": projects,
        "secret": r.get("secret") or "",
        "is_active": bool(r.get("is_active", 1)),
        "max_retries": int(r.get("max_retries", 3)),
        "created_at": r.get("created_at"),
    }


@api_router.get("/webhooks", tags=["Webhooks Admin"])
async def list_webhooks(user: dict[str, Any] = Depends(verify_token)) -> dict[str, Any]:
    check_permission(user, "manage_webhooks")
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM webhooks ORDER BY created_at DESC").fetchall()
        items = [_serialize_webhook(dict(r)) for r in rows]
    return {
        "supported_events": SUPPORTED_WEBHOOK_EVENTS,
        "count": len(items),
        "items": items,
    }


@api_router.post("/webhooks", status_code=201, tags=["Webhooks Admin"])
async def create_webhook(
    payload: WebhookCreateRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_webhooks")
    wid = str(uuid.uuid4())
    now = settings.now_iso()
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO webhooks (
                id, name, description, target_url, events_json, project_ids_json,
                secret, is_active, max_retries, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                wid,
                payload.name.strip(),
                payload.description.strip(),
                payload.target_url.strip(),
                json.dumps(payload.events),
                json.dumps(payload.project_ids),
                payload.secret,
                1 if payload.is_active else 0,
                payload.max_retries,
                user["id"],
                now,
            ),
        )
        row = dict(conn.execute("SELECT * FROM webhooks WHERE id = ?", (wid,)).fetchone())
    return {"webhook": _serialize_webhook(row)}


@api_router.patch("/webhooks/{webhook_id}", tags=["Webhooks Admin"])
async def update_webhook(
    webhook_id: str,
    payload: WebhookUpdateRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_webhooks")
    with db_session() as conn:
        row = conn.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Webhook não encontrado.")
        curr = dict(row)
        conn.execute(
            """
            UPDATE webhooks SET
                name = ?, description = ?, target_url = ?, events_json = ?,
                project_ids_json = ?, secret = ?, is_active = ?, max_retries = ?
            WHERE id = ?
            """,
            (
                payload.name.strip() if payload.name is not None else curr["name"],
                payload.description if payload.description is not None else curr["description"],
                payload.target_url.strip() if payload.target_url is not None else curr["target_url"],
                json.dumps(payload.events) if payload.events is not None else curr["events_json"],
                json.dumps(payload.project_ids) if payload.project_ids is not None else curr["project_ids_json"],
                payload.secret if payload.secret is not None else curr["secret"],
                (1 if payload.is_active else 0) if payload.is_active is not None else curr["is_active"],
                payload.max_retries if payload.max_retries is not None else curr["max_retries"],
                webhook_id,
            ),
        )
        updated = dict(conn.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,)).fetchone())
    return {"webhook": _serialize_webhook(updated)}


@api_router.delete("/webhooks/{webhook_id}", tags=["Webhooks Admin"])
async def delete_webhook(
    webhook_id: str,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_webhooks")
    with db_session() as conn:
        conn.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
    return {"deleted": True, "id": webhook_id}


@api_router.get("/webhooks/deliveries", tags=["Webhooks Admin"])
async def list_webhook_deliveries(
    webhook_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_webhooks")
    with db_session() as conn:
        if webhook_id:
            rows = conn.execute(
                """
                SELECT d.*, w.name AS webhook_name, w.target_url
                FROM webhook_deliveries d
                JOIN webhooks w ON w.id = d.webhook_id
                WHERE d.webhook_id = ?
                ORDER BY d.created_at DESC LIMIT ?
                """,
                (webhook_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT d.*, w.name AS webhook_name, w.target_url
                FROM webhook_deliveries d
                JOIN webhooks w ON w.id = d.webhook_id
                ORDER BY d.created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        items = [dict(r) for r in rows]
    return {"count": len(items), "items": items}


# =====================================================================
# 10. ONLINE SQLITE BACKUPS & RESTORATION
# =====================================================================
@api_router.get("/backups", tags=["Database Backups"])
async def list_backups_endpoint(user: dict[str, Any] = Depends(verify_token)) -> dict[str, Any]:
    check_permission(user, "manage_backups")
    items = list_backups_workflow()
    cfg = get_backup_config()
    return {"config": cfg, "count": len(items), "items": items}


@api_router.patch("/backups/config", tags=["Database Backups"])
async def update_backup_config_endpoint(
    payload: dict[str, Any],
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_backups")
    cfg = update_backup_config(payload)
    return {"config": cfg}


@api_router.post("/backups", status_code=201, tags=["Database Backups"])
async def create_backup_endpoint(
    payload: BackupCreateRequest,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_backups")
    res = run_backup_workflow(
        user_id=user["id"],
        notes=payload.notes,
        include_attachments=payload.include_attachments,
    )
    return {"backup": res}


@api_router.post("/backups/{backup_id}/restore", tags=["Database Backups"])
async def restore_backup_endpoint(
    backup_id: str,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_backups")
    res = restore_backup_workflow(backup_id)
    return res


@api_router.get("/backups/{backup_id}/download", tags=["Database Backups"])
async def download_backup_file(
    backup_id: str,
    user: dict[str, Any] = Depends(verify_token),
) -> FileResponse:
    check_permission(user, "manage_backups")
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM backups WHERE id = ? OR filename = ?",
            (backup_id, backup_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Backup não encontrado.")
        filename = row["filename"]

    fpath = settings.BACKUPS_DIR / Path(filename).name
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="Arquivo de backup não encontrado no disco.")
    return FileResponse(
        path=str(fpath),
        filename=filename,
        media_type="application/zip",
    )


@api_router.delete("/backups/{backup_id}", tags=["Database Backups"])
async def delete_backup_endpoint(
    backup_id: str,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    check_permission(user, "manage_backups")
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM backups WHERE id = ? OR filename = ?",
            (backup_id, backup_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Backup não encontrado.")
        fpath = settings.BACKUPS_DIR / Path(row["filename"]).name
        if fpath.exists():
            fpath.unlink()
        conn.execute("DELETE FROM backups WHERE id = ?", (row["id"],))
    return {"deleted": True, "id": row["id"]}
