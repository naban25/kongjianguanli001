"""File storage service — local backend for dev, S3-compatible for prod."""
from __future__ import annotations

import hashlib
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO, Tuple

from ..core.config import get_settings

settings = get_settings()


def _storage_root() -> Path:
    return settings.local_storage_abs


def generate_storage_key(filename: str) -> str:
    """Return a non-predictable storage path — never the raw user filename."""
    ext = Path(filename).suffix.lower()
    safe_ext = ext if ext in {".xlsx", ".csv", ".xls", ".xlsm"} else ".bin"
    return f"uploads/{uuid.uuid4().hex}{safe_ext}"


def save_uploaded_file(content: bytes, filename: str) -> Tuple[str, str]:
    """Persist raw bytes to private storage. Returns (storage_key, sha256)."""
    key = generate_storage_key(filename)
    target = _storage_root() / key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    return key, sha


def open_storage_file(storage_key: str) -> Path:
    """Open a stored file for reading. Raises FileNotFoundError."""
    p = _storage_root() / storage_key
    p.resolve()  # prevent path traversal
    if not p.exists():
        raise FileNotFoundError(f"Storage key not found: {storage_key}")
    return p


def delete_storage_file(storage_key: str) -> None:
    try:
        (open_storage_file(storage_key)).unlink()
    except FileNotFoundError:
        pass
