import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any
from loguru import logger
from core.config import settings


class StorageClient:
    """Service node for attachments storage and hot online SQLite backup/restore."""

    def save_attachment(self, filename: str, content: bytes) -> dict[str, Any]:
        settings.ensure_dirs()
        safe_ext = Path(filename).suffix[:15]
        stored_name = f"{uuid.uuid4().hex}{safe_ext}"
        target_path = settings.ATTACHMENTS_DIR / stored_name
        target_path.write_bytes(content)
        return {
            "stored_name": stored_name,
            "size_bytes": len(content),
            "path": str(target_path),
        }

    def delete_attachment(self, stored_name: str) -> bool:
        target_path = settings.ATTACHMENTS_DIR / Path(stored_name).name
        if target_path.exists():
            target_path.unlink()
            return True
        return False

    def get_attachment_path(self, stored_name: str) -> Path:
        return settings.ATTACHMENTS_DIR / Path(stored_name).name

    def create_online_backup(self, include_attachments: bool = True) -> dict[str, Any]:
        """Creates a consistent online backup of SQLite (using sqlite3 backup API) + attachments."""
        settings.ensure_dirs()
        timestamp_str = settings.now().strftime("%Y%m%d_%H%M%S")
        short_id = uuid.uuid4().hex[:6]
        zip_filename = f"taskforge_backup_{timestamp_str}_{short_id}.zip"
        zip_path = settings.BACKUPS_DIR / zip_filename

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_db_path = Path(tmpdir) / "tasks_snapshot.db"
            # Perform hot online SQLite backup safely without stopping the application
            src_conn = sqlite3.connect(str(settings.DB_PATH), timeout=30.0)
            dst_conn = sqlite3.connect(str(tmp_db_path))
            try:
                with dst_conn:
                    src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
                src_conn.close()

            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(tmp_db_path, arcname="tasks.db")
                if include_attachments and settings.ATTACHMENTS_DIR.exists():
                    for file_path in settings.ATTACHMENTS_DIR.iterdir():
                        if file_path.is_file():
                            zf.write(
                                file_path,
                                arcname=f"attachments/{file_path.name}",
                            )

        size_bytes = zip_path.stat().st_size
        logger.info(f"Created online backup {zip_filename} ({size_bytes} bytes)")
        return {
            "filename": zip_filename,
            "path": str(zip_path),
            "size_bytes": size_bytes,
            "includes_attachments": include_attachments,
        }

    def restore_online_backup(self, zip_filename: str) -> dict[str, Any]:
        """Restores SQLite database and attachments from a backup zip online."""
        safe_name = Path(zip_filename).name
        zip_path = settings.BACKUPS_DIR / safe_name
        if not zip_path.exists():
            raise FileNotFoundError(f"Arquivo de backup '{safe_name}' não encontrado.")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_dir_path = Path(tmpdir)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmp_dir_path)

            extracted_db = tmp_dir_path / "tasks.db"
            if not extracted_db.exists():
                raise ValueError("Arquivo de backup inválido: 'tasks.db' ausente no pacote.")

            # Validate SQLite integrity before restoring
            check_conn = sqlite3.connect(str(extracted_db))
            try:
                res = check_conn.execute("PRAGMA integrity_check;").fetchone()
                if not res or res[0] != "ok":
                    raise ValueError(f"Falha de integridade no SQLite do backup: {res}")
            finally:
                check_conn.close()

            # Use sqlite3 online backup API in reverse to overwrite active DB safely
            src_conn = sqlite3.connect(str(extracted_db))
            dst_conn = sqlite3.connect(str(settings.DB_PATH), timeout=30.0)
            try:
                with dst_conn:
                    src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
                src_conn.close()

            # Restore attachments if present in backup
            extracted_attachments = tmp_dir_path / "attachments"
            restored_files = 0
            if extracted_attachments.exists():
                settings.ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
                for item in extracted_attachments.iterdir():
                    if item.is_file():
                        shutil.copy2(item, settings.ATTACHMENTS_DIR / item.name)
                        restored_files += 1

        logger.info(f"Restored backup {safe_name} online ({restored_files} attachments).")
        return {
            "restored": True,
            "filename": safe_name,
            "restored_attachments": restored_files,
        }


storage_client = StorageClient()
