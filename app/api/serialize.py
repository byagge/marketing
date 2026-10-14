from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any


def to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, bytes):
        return None
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    if hasattr(obj, "__dict__"):
        return {
            k: to_jsonable(v)
            for k, v in vars(obj).items()
            if not k.startswith("_") and not callable(v)
        }
    return str(obj)
