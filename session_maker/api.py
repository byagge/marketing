from __future__ import annotations

import re
import sys
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def out_dir() -> Path:
    path = project_root() / "data" / "session_maker"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_api() -> tuple[int, str]:
    """API_ID / API_HASH from .env or app.config."""
    env_path = project_root() / ".env"
    api_id: int | None = None
    api_hash: str | None = None

    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key == "API_ID" and val.isdigit():
                api_id = int(val)
            elif key == "API_HASH" and val:
                api_hash = val

    if api_id and api_hash:
        return api_id, api_hash

    root = str(project_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    from app.config import get_settings

    s = get_settings()
    return int(s.api_id), str(s.api_hash)


def sanitize_session_name(raw: str) -> str:
    name = re.sub(r"[^\w.\-]+", "_", (raw or "").strip()).strip("._") or "account"
    if name.lower().endswith(".session"):
        name = name[: -len(".session")]
    return name


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"[^\d+]", "", (raw or "").strip())
    if not digits.startswith("+"):
        digits = "+" + digits.lstrip("+")
    if len(re.sub(r"\D", "", digits)) < 10:
        raise ValueError("Номер слишком короткий")
    return digits
