import os
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo


class Settings:
    def __init__(self) -> None:
        self.APP_NAME: str = os.getenv("APP_NAME", "TaskForge")
        self.APP_ENV: str = os.getenv("APP_ENV", "development")
        self.PORT: int = int(os.getenv("PORT", "8000"))
        self.TZ_NAME: str = os.getenv("TZ", "America/Sao_Paulo")
        try:
            self.TZ = ZoneInfo(self.TZ_NAME)
        except Exception:
            self.TZ = ZoneInfo("UTC")

        base_default = str(Path(__file__).resolve().parent.parent / "data")
        self.DATA_DIR: Path = Path(os.getenv("DATA_DIR", base_default))
        self.DB_PATH: Path = Path(os.getenv("DB_PATH", str(self.DATA_DIR / "tasks.db")))
        self.EXECUTIONS_DB_PATH: Path = Path(
            os.getenv("EXECUTIONS_DB_PATH", str(self.DATA_DIR / "executions.db"))
        )
        self.ATTACHMENTS_DIR: Path = Path(
            os.getenv("ATTACHMENTS_DIR", str(self.DATA_DIR / "attachments"))
        )
        self.BACKUPS_DIR: Path = Path(
            os.getenv("BACKUPS_DIR", str(self.DATA_DIR / "backups"))
        )
        self.VAPID_FILE: Path = Path(
            os.getenv("VAPID_FILE", str(self.DATA_DIR / "vapid.json"))
        )

        self.SECRET_KEY: str = os.getenv(
            "SECRET_KEY", "taskforge-super-secret-jwt-key-change-in-prod-2026"
        )
        self.WEBHOOK_RECEIVER_TOKEN: str = os.getenv(
            "WEBHOOK_RECEIVER_TOKEN", "wh_sec_token_taskforge_default"
        )

        self.DEFAULT_ADMIN_USER: str = os.getenv("DEFAULT_ADMIN_USER", "admin")
        self.DEFAULT_ADMIN_EMAIL: str = os.getenv(
            "DEFAULT_ADMIN_EMAIL", "admin@taskforge.local"
        )
        self.DEFAULT_ADMIN_PASSWORD: str = os.getenv(
            "DEFAULT_ADMIN_PASSWORD", "Admin@123456"
        )

        self.BACKUP_RETENTION_COUNT: int = int(
            os.getenv("BACKUP_RETENTION_COUNT", "10")
        )
        self.AUTO_BACKUP_CRON: str = os.getenv("AUTO_BACKUP_CRON", "0 3 * * *")

        self.VAPID_PUBLIC_KEY: str = os.getenv("VAPID_PUBLIC_KEY", "")
        self.VAPID_PRIVATE_KEY: str = os.getenv("VAPID_PRIVATE_KEY", "")
        self.VAPID_CLAIM_EMAIL: str = os.getenv(
            "VAPID_CLAIM_EMAIL", "mailto:admin@taskforge.local"
        )

        self.ensure_dirs()

    def ensure_dirs(self) -> None:
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
        self.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        self.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.EXECUTIONS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    def now(self) -> datetime:
        return datetime.now(self.TZ)

    def now_iso(self) -> str:
        return self.now().isoformat()


settings = Settings()
