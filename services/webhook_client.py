import hashlib
import hmac
import json
import time
from typing import Any
import httpx
from loguru import logger


class WebhookClient:
    """Reusable HTTP Webhook sender with HMAC-SHA256 signature verification header."""

    def __init__(self, timeout_seconds: float = 10.0) -> None:
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def sign_payload(secret: str, payload_bytes: bytes) -> str:
        if not secret:
            return ""
        mac = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256)
        return f"sha256={mac.hexdigest()}"

    async def send(
        self,
        target_url: str,
        event_name: str,
        payload: dict[str, Any],
        secret: str = "",
        delivery_id: str = "",
    ) -> dict[str, Any]:
        body_str = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        body_bytes = body_str.encode("utf-8")
        signature = self.sign_payload(secret, body_bytes)

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "TaskForge-Webhook/1.0",
            "X-TaskForge-Event": event_name,
            "X-TaskForge-Delivery": delivery_id,
        }
        if signature:
            headers["X-TaskForge-Signature"] = signature

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(target_url, content=body_bytes, headers=headers)
                duration_ms = (time.perf_counter() - start) * 1000.0
                success = 200 <= resp.status_code < 300
                return {
                    "success": success,
                    "status_code": resp.status_code,
                    "response_body": (resp.text or "")[:2000],
                    "error": "" if success else f"HTTP {resp.status_code}",
                    "duration_ms": round(duration_ms, 2),
                }
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000.0
            logger.warning(f"Webhook delivery error for {target_url}: {exc}")
            return {
                "success": False,
                "status_code": 0,
                "response_body": "",
                "error": str(exc)[:500],
                "duration_ms": round(duration_ms, 2),
            }


webhook_client = WebhookClient()
