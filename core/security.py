import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta
from typing import Any
import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from core.config import settings
from core.database import db_session, row_to_dict

bearer_scheme = HTTPBearer(auto_error=False)

ROLE_DEFAULT_PERMISSIONS: dict[str, dict[str, bool]] = {
    "admin": {
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
    },
    "supervisor": {
        "view": True,
        "create": True,
        "edit": True,
        "complete": True,
        "delete": True,
        "manage_projects": True,
        "manage_users": False,
        "manage_fields": True,
        "manage_webhooks": False,
        "manage_backups": False,
        "only_assigned": False,
    },
    "member": {
        "view": True,
        "create": True,
        "edit": True,
        "complete": True,
        "delete": False,
        "manage_projects": False,
        "manage_users": False,
        "manage_fields": False,
        "manage_webhooks": False,
        "manage_backups": False,
        "only_assigned": True,
    },
    "viewer": {
        "view": True,
        "create": False,
        "edit": False,
        "complete": False,
        "delete": False,
        "manage_projects": False,
        "manage_users": False,
        "manage_fields": False,
        "manage_webhooks": False,
        "manage_backups": False,
        "only_assigned": True,
    },
}


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000
    )
    return f"pbkdf2_sha256$120000${salt}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        parts = stored_hash.split("$")
        if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
            return False
        iterations = int(parts[1])
        salt = parts[2]
        expected_hex = parts[3]
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
        )
        return hmac.compare_digest(dk.hex(), expected_hex)
    except Exception:
        return False


def create_access_token(user_id: str, username: str, role: str, hours: int = 72) -> str:
    now = datetime.now(settings.TZ)
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=hours)).timestamp()),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def generate_api_token() -> tuple[str, str, str]:
    raw = "tf_live_" + secrets.token_urlsafe(28)
    token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    prefix = raw[:14]
    return raw, token_hash, prefix


def get_effective_permissions(user: dict[str, Any], project_id: str | None = None) -> dict[str, bool]:
    role = user.get("role", "member")
    base = dict(ROLE_DEFAULT_PERMISSIONS.get(role, ROLE_DEFAULT_PERMISSIONS["member"]))
    try:
        custom = json.loads(user.get("permissions_json") or "{}")
        if isinstance(custom, dict):
            for k, v in custom.items():
                if isinstance(v, bool):
                    base[k] = v
    except Exception:
        pass

    if role == "admin":
        for k in list(base.keys()):
            base[k] = False if k == "only_assigned" else True
        return base

    if project_id:
        with db_session() as conn:
            pm = conn.execute(
                "SELECT role, permissions_json FROM project_members WHERE project_id = ? AND user_id = ?",
                (project_id, user["id"]),
            ).fetchone()
            if pm:
                pm_role = pm["role"]
                if pm_role in ROLE_DEFAULT_PERMISSIONS:
                    for k, v in ROLE_DEFAULT_PERMISSIONS[pm_role].items():
                        base[k] = v
                try:
                    pm_custom = json.loads(pm["permissions_json"] or "{}")
                    if isinstance(pm_custom, dict):
                        for k, v in pm_custom.items():
                            if isinstance(v, bool):
                                base[k] = v
                except Exception:
                    pass
    return base


def _resolve_user_from_token(token_str: str) -> dict[str, Any]:
    # 1. Check if it's an API token (tf_live_...)
    if token_str.startswith("tf_"):
        token_hash = hashlib.sha256(token_str.encode("utf-8")).hexdigest()
        with db_session() as conn:
            t_row = conn.execute(
                "SELECT * FROM api_tokens WHERE token_hash = ?", (token_hash,)
            ).fetchone()
            if not t_row:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token de API inválido ou revogado.",
                )
            token_data = row_to_dict(t_row) or {}
            if token_data.get("expires_at"):
                try:
                    exp_dt = datetime.fromisoformat(token_data["expires_at"])
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=settings.TZ)
                    if settings.now() > exp_dt:
                        raise HTTPException(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Token de API expirado.",
                        )
                except HTTPException:
                    raise
                except Exception:
                    pass

            u_row = conn.execute(
                "SELECT * FROM users WHERE id = ? AND is_active = 1",
                (token_data["user_id"],),
            ).fetchone()
            if not u_row:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Usuário vinculado ao token está inativo ou não existe.",
                )
            conn.execute(
                "UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
                (settings.now_iso(), token_data["id"]),
            )
            user = row_to_dict(u_row) or {}
            try:
                scopes = json.loads(token_data.get("scopes_json") or '["*"]')
            except Exception:
                scopes = ["*"]
            user["token_scopes"] = scopes
            user["permissions"] = get_effective_permissions(user)
            return user

    # 2. Otherwise decode JWT
    try:
        payload = jwt.decode(token_str, settings.SECRET_KEY, algorithms=["HS256"])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Token malformado.")
        with db_session() as conn:
            u_row = conn.execute(
                "SELECT * FROM users WHERE id = ? AND is_active = 1", (user_id,)
            ).fetchone()
            if not u_row:
                raise HTTPException(status_code=401, detail="Usuário não encontrado ou inativo.")
            user = row_to_dict(u_row) or {}
            user["token_scopes"] = ["*"]
            user["permissions"] = get_effective_permissions(user)
            return user
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Sessão expirada. Faça login novamente.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token de autenticação inválido.")


async def verify_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    x_webhook_token: str | None = Header(default=None, alias="X-Webhook-Token"),
) -> dict[str, Any]:
    """Unified authentication validator for JWT, API Token, Query Token (SSE), and Webhook Receiver."""
    token_str = None
    if credentials and credentials.credentials:
        token_str = credentials.credentials
    elif x_api_key:
        token_str = x_api_key
    elif x_webhook_token:
        if hmac.compare_digest(x_webhook_token, settings.WEBHOOK_RECEIVER_TOKEN):
            with db_session() as conn:
                admin_row = conn.execute(
                    "SELECT * FROM users WHERE role = 'admin' AND is_active = 1 ORDER BY created_at ASC LIMIT 1"
                ).fetchone()
                if admin_row:
                    user = row_to_dict(admin_row) or {}
                    user["token_scopes"] = ["*"]
                    user["permissions"] = get_effective_permissions(user)
                    return user
        token_str = x_webhook_token
    elif "token" in request.query_params:
        token_str = request.query_params["token"]

    if not token_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticação necessária. Envie Authorization: Bearer <token> ou X-API-Key.",
        )

    return _resolve_user_from_token(token_str)


def check_permission(
    user: dict[str, Any],
    perm_name: str,
    project_id: str | None = None,
    required_scope: str | None = None,
) -> None:
    scopes = user.get("token_scopes", ["*"])
    if "*" not in scopes and required_scope and required_scope not in scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Token de API não possui o escopo necessário: '{required_scope}'.",
        )
    perms = get_effective_permissions(user, project_id)
    if not perms.get(perm_name, False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permissão insuficiente para executar '{perm_name}'.",
        )


def can_user_access_task(user: dict[str, Any], task: dict[str, Any]) -> bool:
    if user.get("role") in ("admin", "supervisor"):
        return True
    project_id = task.get("project_id")
    perms = get_effective_permissions(user, project_id)
    if not perms.get("view", False):
        return False
    if not perms.get("only_assigned", False):
        return True
    uid = user["id"]
    if task.get("assignee_id") == uid or task.get("created_by") == uid:
        return True
    try:
        participants = (
            task.get("participants")
            if isinstance(task.get("participants"), list)
            else json.loads(task.get("participants_json") or "[]")
        )
        if uid in participants:
            return True
    except Exception:
        pass
    return False
