import json
import sqlite3
import time
import uuid
from typing import Any
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from loguru import logger
from core.config import settings
from core.security import verify_token

audit_router = APIRouter(tags=["Observability (Executions)"])


def get_executions_conn() -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = sqlite3.connect(str(settings.EXECUTIONS_DB_PATH), timeout=15.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_executions_db() -> None:
    conn = get_executions_conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS executions (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                method TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                input_payload TEXT,
                output_payload TEXT,
                duration_ms REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_exec_timestamp ON executions(timestamp DESC);"
        )
        conn.commit()
    finally:
        conn.close()


def record_execution(
    method: str,
    endpoint: str,
    status_code: int,
    input_payload: str,
    output_payload: str,
    duration_ms: float,
) -> None:
    try:
        conn = get_executions_conn()
        try:
            conn.execute(
                """
                INSERT INTO executions (
                    id, timestamp, method, endpoint, status_code, input_payload, output_payload, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    settings.now_iso(),
                    method,
                    endpoint,
                    status_code,
                    input_payload[:8000] if input_payload else "",
                    output_payload[:8000] if output_payload else "",
                    round(duration_ms, 2),
                ),
            )
            # Keep last 1000 executions to maintain low disk usage
            conn.execute(
                """
                DELETE FROM executions WHERE id NOT IN (
                    SELECT id FROM executions ORDER BY timestamp DESC LIMIT 1000
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"Audit log write error: {exc}")


class ExecutionAuditMiddleware(BaseHTTPMiddleware):
    """Intercepts HTTP/Webhook requests and logs to executions.db (n8n-style Executions)."""

    SKIP_PREFIXES = ("/static", "/sw.js", "/manifest.json", "/favicon", "/api/v1/stream")

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path
        if any(path.startswith(p) for p in self.SKIP_PREFIXES):
            return await call_next(request)

        # Only audit API and Webhook endpoints (or when explicitly calling /webhooks)
        should_audit = path.startswith("/api/") or path.startswith("/webhooks")
        if not should_audit:
            return await call_next(request)

        start = time.perf_counter()
        input_text = ""
        try:
            content_type = request.headers.get("content-type", "")
            if "application/json" in content_type:
                body_bytes = await request.body()
                input_text = body_bytes.decode("utf-8", errors="replace")
                # Mask passwords in audit logs
                if "password" in input_text.lower():
                    try:
                        parsed = json.loads(input_text)
                        if isinstance(parsed, dict):
                            for k in list(parsed.keys()):
                                if "password" in k.lower() or "secret" in k.lower():
                                    parsed[k] = "***REDACTED***"
                            input_text = json.dumps(parsed, ensure_ascii=False)
                    except Exception:
                        pass
            elif request.query_params:
                input_text = json.dumps(dict(request.query_params), ensure_ascii=False)
        except Exception:
            input_text = ""

        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000.0

        output_text = ""
        resp_content_type = response.headers.get("content-type", "")
        if "application/json" in resp_content_type and hasattr(response, "body_iterator"):
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            full_body = b"".join(chunks)
            output_text = full_body.decode("utf-8", errors="replace")
            response = Response(
                content=full_body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        record_execution(
            method=request.method,
            endpoint=path,
            status_code=response.status_code,
            input_payload=input_text,
            output_payload=output_text,
            duration_ms=duration_ms,
        )
        return response


@audit_router.get("/executions")
async def list_executions(
    format: str = Query(default="json", description="json ou html"),
    limit: int = Query(default=50, ge=1, le=200),
    user: dict[str, Any] = Depends(verify_token),
) -> Response:
    conn = get_executions_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM executions ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        items = [dict(r) for r in rows]
    finally:
        conn.close()

    if format.lower() == "html":
        rows_html = "".join(
            f"<tr class='border-b border-zinc-800 hover:bg-zinc-900/60'>"
            f"<td class='p-3 font-mono text-xs text-zinc-400'>{r['timestamp']}</td>"
            f"<td class='p-3 font-bold text-xs text-orange-400'>{r['method']}</td>"
            f"<td class='p-3 font-mono text-xs text-zinc-200'>{r['endpoint']}</td>"
            f"<td class='p-3 text-xs'><span class='px-2 py-0.5 rounded-full {'bg-emerald-500/20 text-emerald-300' if r['status_code'] < 400 else 'bg-rose-500/20 text-rose-300'}'>{r['status_code']}</span></td>"
            f"<td class='p-3 font-mono text-xs text-pink-400'>{r['duration_ms']} ms</td>"
            f"<td class='p-3 font-mono text-xs text-zinc-400 max-w-xs truncate'>{r['input_payload'] or '-'}</td>"
            f"<td class='p-3 font-mono text-xs text-zinc-400 max-w-xs truncate'>{r['output_payload'] or '-'}</td>"
            f"</tr>"
            for r in items
        )
        html = f"""<!DOCTYPE html>
        <html lang="pt-BR" class="dark">
        <head>
            <meta charset="UTF-8">
            <title>TaskForge - Executions Log</title>
            <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="bg-zinc-950 text-zinc-100 p-6 font-sans">
            <div class="max-w-7xl mx-auto">
                <h1 class="text-2xl font-bold bg-gradient-to-r from-orange-500 via-pink-500 to-purple-500 bg-clip-text text-transparent mb-4">
                    Observabilidade • Últimas {len(items)} Execuções (executions.db)
                </h1>
                <div class="overflow-x-auto rounded-2xl border border-zinc-800 bg-zinc-900/40 backdrop-blur-md">
                    <table class="w-full text-left border-collapse">
                        <thead>
                            <tr class="border-b border-zinc-800 text-xs uppercase text-zinc-400">
                                <th class="p-3">Timestamp</th>
                                <th class="p-3">Método</th>
                                <th class="p-3">Endpoint</th>
                                <th class="p-3">Status</th>
                                <th class="p-3">Duração</th>
                                <th class="p-3">Entrada</th>
                                <th class="p-3">Saída</th>
                            </tr>
                        </thead>
                        <tbody>{rows_html}</tbody>
                    </table>
                </div>
            </div>
        </body>
        </html>"""
        return HTMLResponse(content=html)

    return JSONResponse(content={"count": len(items), "items": items})
