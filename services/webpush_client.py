import base64
import json
from typing import Any
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import webpush, WebPushException
from loguru import logger
from core.config import settings


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class WebPushClient:
    """Service node for VAPID key management and sending Web Push notifications."""

    def __init__(self) -> None:
        self.public_key: str = ""
        self.private_key: str = ""
        self.claim_email: str = settings.VAPID_CLAIM_EMAIL
        self._load_or_generate_keys()

    def _load_or_generate_keys(self) -> None:
        if settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY:
            self.public_key = settings.VAPID_PUBLIC_KEY
            self.private_key = settings.VAPID_PRIVATE_KEY
            return

        if settings.VAPID_FILE.exists():
            try:
                data = json.loads(settings.VAPID_FILE.read_text(encoding="utf-8"))
                if data.get("public_key") and data.get("private_key"):
                    self.public_key = data["public_key"]
                    self.private_key = data["private_key"]
                    return
            except Exception as exc:
                logger.warning(f"Failed reading VAPID file, regenerating: {exc}")

        # Generate P-256 EC keypair for VAPID
        private_key_obj = ec.generate_private_key(ec.SECP256R1())
        private_numbers = private_key_obj.private_numbers()
        private_bytes = private_numbers.private_value.to_bytes(32, byteorder="big")
        public_bytes = private_key_obj.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        self.private_key = _b64url_encode(private_bytes)
        self.public_key = _b64url_encode(public_bytes)

        try:
            settings.VAPID_FILE.write_text(
                json.dumps(
                    {
                        "public_key": self.public_key,
                        "private_key": self.private_key,
                        "claim_email": self.claim_email,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            logger.info("Generated and persisted VAPID keypair for Web Push.")
        except Exception as exc:
            logger.warning(f"Could not save VAPID file: {exc}")

    def send_push(
        self,
        subscription_info: dict[str, Any],
        title: str,
        body: str,
        url: str = "/",
        tag: str = "taskforge",
    ) -> dict[str, Any]:
        payload = json.dumps(
            {
                "title": title,
                "body": body,
                "url": url,
                "tag": tag,
                "timestamp": settings.now_iso(),
            },
            ensure_ascii=False,
        )
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=self.private_key,
                vapid_claims={"sub": self.claim_email},
                timeout=8,
            )
            return {"sent": True, "error": ""}
        except WebPushException as exc:
            status_code = getattr(exc.response, "status_code", 0) if getattr(exc, "response", None) else 0
            logger.warning(f"WebPushException (HTTP {status_code}): {exc}")
            return {"sent": False, "status_code": status_code, "error": str(exc)}
        except Exception as exc:
            logger.warning(f"WebPush error: {exc}")
            return {"sent": False, "status_code": 0, "error": str(exc)}


webpush_client = WebPushClient()
