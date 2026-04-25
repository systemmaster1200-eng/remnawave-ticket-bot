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
    external_id: Optional[str] = None,
) -> None:
    data = _load()
    data[payment_id] = {
        "manager_id": manager_id,
        "client_id": client_id,
        "amount": amount,
        "currency": currency,
        "user_uuid": user_uuid,
        "provider": provider,
        "external_id": external_id,
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


def pending_pop_candidates(*candidate_ids: str) -> Optional[Dict[str, Any]]:
    candidates = {str(item).strip() for item in candidate_ids if str(item).strip()}
    if not candidates:
        return None
    data = _load()
    matched_key = next((candidate for candidate in candidates if candidate in data), None)
    if matched_key is None:
        for key, value in data.items():
            external_id = str((value or {}).get("external_id") or "").strip()
            if external_id and external_id in candidates:
                matched_key = key
                break
    if matched_key is None:
        return None
    record = data.pop(matched_key, None)
    if record is not None:
        _save(data)
    return record
