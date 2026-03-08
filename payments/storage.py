"""Хранение ожидающих оплаты счетов (payment_id -> данные для уведомления менеджера)."""
import json
import logging
import os
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

_STORAGE_DIR = Path(os.getenv("PAYMENTS_DATA_DIR", "/data"))
_PENDING_FILE = _STORAGE_DIR / "payments_pending.json"


def _ensure_dir():
    _STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def _load() -> Dict[str, Dict[str, Any]]:
    _ensure_dir()
    if not _PENDING_FILE.exists():
        return {}
    try:
        with open(_PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("payments storage load: %s", e)
        return {}


def _save(data: Dict[str, Dict[str, Any]]) -> None:
    _ensure_dir()
    try:
        with open(_PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=0)
    except Exception as e:
        logger.warning("payments storage save: %s", e)


def pending_add(
    payment_id: str,
    manager_id: int,
    client_id: int,
    amount: float,
    currency: str,
    user_uuid: Optional[str] = None,
    provider: str = "freekassa",
) -> None:
    data = _load()
    data[payment_id] = {
        "manager_id": manager_id,
        "client_id": client_id,
        "amount": amount,
        "currency": currency,
        "user_uuid": user_uuid,
        "provider": provider,
    }
    _save(data)


def pending_pop(payment_id: str) -> Optional[Dict[str, Any]]:
    data = _load()
    record = data.pop(payment_id, None)
    if record is not None:
        _save(data)
    return record


def pending_get(payment_id: str) -> Optional[Dict[str, Any]]:
    return _load().get(payment_id)
