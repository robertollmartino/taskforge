import json
import sqlite3
import uuid
import hashlib
import secrets
from contextlib import contextmanager
from typing import Any, Generator
from loguru import logger
from core.config import settings


def hash_password_bootstrap(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000)
    return f"pbkdf2_sha256$120000${salt}${dk.hex()}"


def get_connection() -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = sqlite3.connect(str(settings.DB_PATH), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def db_session() -> Generator[sqlite3.Connection, None, None]:
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    permissions_json TEXT NOT NULL DEFAULT '{}',
    digest_enabled INTEGER NOT NULL DEFAULT 1,
    digest_times_json TEXT NOT NULL DEFAULT '["08:00","13:00","18:00"]',
    timezone TEXT NOT NULL DEFAULT 'America/Sao_Paulo',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_hash TEXT UNIQUE NOT NULL,
    token_prefix TEXT NOT NULL,
    scopes_json TEXT NOT NULL DEFAULT '["*"]',
    last_used_at TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    color TEXT NOT NULL DEFAULT '#f97316',
    icon TEXT DEFAULT 'folder',
    is_archived INTEGER NOT NULL DEFAULT 0,
    created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_members (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'member',
    permissions_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(project_id, user_id)
);

CREATE TABLE IF NOT EXISTS statuses (
    id TEXT PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT '#71717a',
    category TEXT NOT NULL DEFAULT 'open',
    position INTEGER NOT NULL DEFAULT 0,
    is_system INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS custom_fields (
    id TEXT PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    field_type TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 0,
    default_value_json TEXT,
    options_json TEXT NOT NULL DEFAULT '[]',
    position INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    code INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    status_slug TEXT NOT NULL REFERENCES statuses(slug) ON UPDATE CASCADE,
    priority TEXT NOT NULL DEFAULT 'P3',
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    assignee_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    participants_json TEXT NOT NULL DEFAULT '[]',
    tags_json TEXT NOT NULL DEFAULT '[]',
    color TEXT DEFAULT '',
    due_date TEXT,
    due_time TEXT,
    is_today INTEGER NOT NULL DEFAULT 0,
    recurrence_json TEXT,
    custom_values_json TEXT NOT NULL DEFAULT '{}',
    position REAL NOT NULL DEFAULT 0,
    created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status_slug);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee_id);
CREATE INDEX IF NOT EXISTS idx_tasks_due_date ON tasks(due_date);
CREATE INDEX IF NOT EXISTS idx_tasks_is_today ON tasks(is_today);

CREATE TABLE IF NOT EXISTS subtasks (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    is_completed INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_comments (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    author_name TEXT NOT NULL DEFAULT 'Sistema',
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_attachments (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_history (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    actor_name TEXT NOT NULL DEFAULT 'Sistema',
    action TEXT NOT NULL,
    changes_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_views (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_id TEXT REFERENCES users(id) ON DELETE CASCADE,
    view_type TEXT NOT NULL DEFAULT 'table',
    visible_columns_json TEXT NOT NULL DEFAULT '["code","is_today","title","status_slug","priority","project_id","assignee_id","due_date","tags"]',
    column_order_json TEXT NOT NULL DEFAULT '["code","is_today","title","status_slug","priority","project_id","assignee_id","due_date","tags"]',
    filters_json TEXT NOT NULL DEFAULT '{"logic":"AND","conditions":[],"groups":[]}',
    sorts_json TEXT NOT NULL DEFAULT '[{"field":"position","dir":"asc"},{"field":"created_at","dir":"desc"}]',
    group_by TEXT DEFAULT '',
    display_prefs_json TEXT NOT NULL DEFAULT '{"density":"comfortable","show_subtasks":true,"show_empty_groups":true}',
    is_default INTEGER NOT NULL DEFAULT 0,
    is_shared INTEGER NOT NULL DEFAULT 1,
    shared_user_ids_json TEXT NOT NULL DEFAULT '[]',
    is_system_preset INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint TEXT UNIQUE NOT NULL,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    user_agent TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    remind_at TEXT NOT NULL,
    message TEXT DEFAULT '',
    channel TEXT NOT NULL DEFAULT 'push',
    is_sent INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_logs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    dedup_key TEXT UNIQUE NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS webhooks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    target_url TEXT NOT NULL,
    events_json TEXT NOT NULL DEFAULT '[]',
    project_ids_json TEXT NOT NULL DEFAULT '[]',
    secret TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    max_retries INTEGER NOT NULL DEFAULT 3,
    created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id TEXT PRIMARY KEY,
    webhook_id TEXT NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
    event_name TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    response_status INTEGER,
    response_body TEXT DEFAULT '',
    error_message TEXT DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 1,
    next_retry_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    duration_ms REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backups (
    id TEXT PRIMARY KEY,
    filename TEXT UNIQUE NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    includes_attachments INTEGER NOT NULL DEFAULT 1,
    created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def init_db() -> None:
    now = settings.now_iso()
    with db_session() as conn:
        conn.executescript(SCHEMA_SQL)

        # Safe migration for existing databases: add is_today column if missing
        task_cols = [
            r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()
        ]
        if "is_today" not in task_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN is_today INTEGER NOT NULL DEFAULT 0;")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_is_today ON tasks(is_today);")

        # Seed default statuses
        default_statuses = [
            ("pending", "Pendente", "#f97316", "todo", 10, 1),
            ("in_progress", "Em andamento", "#ec4899", "in_progress", 20, 1),
            ("completed", "Concluída", "#10b981", "done", 30, 1),
            ("cancelled", "Cancelada", "#71717a", "canceled", 40, 1),
        ]
        for slug, name, color, category, pos, is_sys in default_statuses:
            exists = conn.execute(
                "SELECT 1 FROM statuses WHERE slug = ?", (slug,)
            ).fetchone()
            if not exists:
                conn.execute(
                    """
                    INSERT INTO statuses (id, slug, name, color, category, position, is_system, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), slug, name, color, category, pos, is_sys, now),
                )

        # Seed default Admin user
        admin = conn.execute(
            "SELECT id FROM users WHERE username = ?", (settings.DEFAULT_ADMIN_USER,)
        ).fetchone()
        if not admin:
            admin_id = str(uuid.uuid4())
            admin_perms = {
                "view": True,
                "create": True,
                "edit": True,
                "complete": True,
                "delete": True,
                "manage_projects": True,
                "manage_users": True,
                "manage_fields": True,
                "manage_webhooks": True,
                "manage_backups": True,
                "only_assigned": False,
            }
            conn.execute(
                """
                INSERT INTO users (
                    id, username, email, full_name, password_hash, role,
                    permissions_json, digest_enabled, digest_times_json, timezone, is_active, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    admin_id,
                    settings.DEFAULT_ADMIN_USER,
                    settings.DEFAULT_ADMIN_EMAIL,
                    "Administrador Principal",
                    hash_password_bootstrap(settings.DEFAULT_ADMIN_PASSWORD),
                    "admin",
                    json.dumps(admin_perms),
                    1,
                    json.dumps(["08:00", "13:00", "18:00"]),
                    settings.TZ_NAME,
                    1,
                    now,
                ),
            )
            logger.info(f"Default admin user '{settings.DEFAULT_ADMIN_USER}' created.")
        else:
            admin_id = admin["id"]

        # Seed default Projects if empty
        proj_count = conn.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"]
        if proj_count == 0:
            default_projects = [
                ("Operações & Estratégia", "Gestão executiva e processos internos", "#f97316", "briefcase"),
                ("Produto & Engenharia", "Desenvolvimento, arquitetura e automações", "#ec4899", "code"),
                ("Clientes & Contratos", "Demandas de clientes e entregas críticas", "#a855f7", "users"),
            ]
            for name, desc, color, icon in default_projects:
                conn.execute(
                    """
                    INSERT INTO projects (id, name, description, color, icon, is_archived, created_by, created_at)
                    VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (str(uuid.uuid4()), name, desc, color, icon, admin_id, now),
                )

        # Seed default System Views if empty
        view_count = conn.execute("SELECT COUNT(*) AS c FROM saved_views").fetchone()["c"]
        if view_count == 0:
            default_cols = json.dumps([
                "code", "is_today", "title", "status_slug", "priority", "project_id",
                "assignee_id", "due_date", "tags"
            ])
            presets = [
                (
                    "Todas as Tarefas",
                    "table",
                    {"logic": "AND", "conditions": [], "groups": []},
                    [{"field": "priority", "dir": "asc"}, {"field": "due_date", "dir": "asc"}],
                    "",
                    1,
                ),
                (
                    "Quadro Kanban Geral",
                    "kanban",
                    {"logic": "AND", "conditions": [], "groups": []},
                    [{"field": "position", "dir": "asc"}],
                    "status_slug",
                    0,
                ),
                (
                    "Hoje",
                    "list",
                    {
                        "logic": "AND",
                        "conditions": [
                            {"field": "is_today", "op": "is_true", "value": True},
                            {"field": "status_slug", "op": "not_in", "value": ["completed", "cancelled"]},
                        ],
                        "groups": [],
                    },
                    [{"field": "priority", "dir": "asc"}],
                    "priority",
                    0,
                ),
                (
                    "Atrasadas",
                    "table",
                    {
                        "logic": "AND",
                        "conditions": [
                            {"field": "due_date", "op": "is_overdue", "value": None},
                            {"field": "status_slug", "op": "not_in", "value": ["completed", "cancelled"]},
                        ],
                        "groups": [],
                    },
                    [{"field": "due_date", "dir": "asc"}],
                    "",
                    0,
                ),
                (
                    "Próximos 7 dias",
                    "table",
                    {
                        "logic": "AND",
                        "conditions": [
                            {"field": "due_date", "op": "next_7_days", "value": None},
                            {"field": "status_slug", "op": "not_in", "value": ["completed", "cancelled"]},
                        ],
                        "groups": [],
                    },
                    [{"field": "due_date", "dir": "asc"}],
                    "due_date",
                    0,
                ),
                (
                    "Sem data",
                    "list",
                    {
                        "logic": "AND",
                        "conditions": [
                            {"field": "due_date", "op": "is_empty", "value": None},
                            {"field": "status_slug", "op": "not_in", "value": ["completed", "cancelled"]},
                        ],
                        "groups": [],
                    },
                    [{"field": "priority", "dir": "asc"}],
                    "",
                    0,
                ),
                (
                    "Prioridade P1",
                    "table",
                    {
                        "logic": "AND",
                        "conditions": [
                            {"field": "priority", "op": "eq", "value": "P1"},
                            {"field": "status_slug", "op": "not_in", "value": ["completed", "cancelled"]},
                        ],
                        "groups": [],
                    },
                    [{"field": "due_date", "dir": "asc"}],
                    "",
                    0,
                ),
                (
                    "Por responsável",
                    "table",
                    {
                        "logic": "AND",
                        "conditions": [
                            {"field": "status_slug", "op": "not_in", "value": ["completed", "cancelled"]}
                        ],
                        "groups": [],
                    },
                    [{"field": "assignee_id", "dir": "asc"}, {"field": "priority", "dir": "asc"}],
                    "assignee_id",
                    0,
                ),
                (
                    "Concluídas",
                    "table",
                    {
                        "logic": "AND",
                        "conditions": [
                            {"field": "status_slug", "op": "eq", "value": "completed"}
                        ],
                        "groups": [],
                    },
                    [{"field": "completed_at", "dir": "desc"}],
                    "",
                    0,
                ),
            ]
            for name, vtype, flt, sorts, grp, is_def in presets:
                conn.execute(
                    """
                    INSERT INTO saved_views (
                        id, name, owner_id, view_type, visible_columns_json, column_order_json,
                        filters_json, sorts_json, group_by, is_default, is_shared, is_system_preset, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        name,
                        admin_id,
                        vtype,
                        default_cols,
                        default_cols,
                        json.dumps(flt, ensure_ascii=False),
                        json.dumps(sorts, ensure_ascii=False),
                        grp,
                        is_def,
                        now,
                    ),
                )

        # Ensure existing 'Hoje' system preset uses the persistent is_today flag instead of due_date
        hoje_filter = json.dumps(
            {
                "logic": "AND",
                "conditions": [
                    {"field": "is_today", "op": "is_true", "value": True},
                    {"field": "status_slug", "op": "not_in", "value": ["completed", "cancelled"]},
                ],
                "groups": [],
            },
            ensure_ascii=False,
        )
        conn.execute(
            "UPDATE saved_views SET filters_json = ? WHERE name = 'Hoje' AND is_system_preset = 1",
            (hoje_filter,),
        )

        # Seed default backup configuration in system_settings
        bkp_cfg = conn.execute(
            "SELECT 1 FROM system_settings WHERE key = 'backup_config'"
        ).fetchone()
        if not bkp_cfg:
            conn.execute(
                "INSERT INTO system_settings (key, value_json, updated_at) VALUES (?, ?, ?)",
                (
                    "backup_config",
                    json.dumps(
                        {
                            "auto_backup_enabled": True,
                            "cron": settings.AUTO_BACKUP_CRON,
                            "retention_count": settings.BACKUP_RETENTION_COUNT,
                            "include_attachments": True,
                        }
                    ),
                    now,
                ),
            )
