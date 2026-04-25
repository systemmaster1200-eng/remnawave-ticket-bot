#!/usr/bin/env python3
"""
Mini App backend для менеджеров Remnawave.
Проверяет Telegram initData, разрешает доступ только менеджерам, отдаёт данные из API Remnawave.
"""

import json
import os
import re
import hmac
import hashlib
import logging
import urllib.parse
import requests
from flask import Flask, request, jsonify, send_from_directory
from pathlib import Path

app = Flask(__name__, static_folder="static")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
REMNAWAVE_API_URL = (os.getenv("REMNAWAVE_API_URL", "") or "").rstrip("/")
REMNAWAVE_API_TOKEN = os.getenv("REMNAWAVE_API_TOKEN", "")
ALLOWED_MANAGER_IDS = set(
    int(x.strip()) for x in (os.getenv("ALLOWED_MANAGER_IDS", "") or "").split(",") if x.strip()
)

# Payment webhooks
FREEEKASSA_WEBHOOK_SECRET = (os.getenv("FREEEKASSA_WEBHOOK_SECRET") or "").strip()
FREEEKASSA_ALLOWED_IPS = {
    "168.119.157.136",
    "168.119.60.227",
    "178.154.197.79",
    "51.250.54.238",
}
PLATEGA_SUCCESS_STATUSES = {
    "APPROVED",
    "COMPLETED",
    "CONFIRMED",
    "PAID",
    "SUCCESS",
    "SUCCEEDED",
    "SUCCESSFUL",
}
PLATEGA_FAILED_STATUSES = {
    "CANCELED",
    "CANCELLED",
    "CHARGEBACK",
    "CHARGEBACKED",
    "DECLINED",
    "ERROR",
    "EXPIRED",
    "FAILED",
    "REJECTED",
}
PAYMENTS_DATA_DIR = Path(os.getenv("PAYMENTS_DATA_DIR", "/data"))
_PENDING_FILE = PAYMENTS_DATA_DIR / "payments_pending.json"
SERVICE_NAME = (os.getenv("SERVICE_NAME") or "Remnawave").strip() or "Remnawave"


def verify_telegram_init_data(init_data: str) -> dict | None:
    """Проверка initData от Telegram Web App. Возвращает распарсенные данные или None."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        parsed = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
        parsed_dict = dict(parsed)
        received_hash = parsed_dict.pop("hash", None)
        if not received_hash:
            return None
        # Data-check-string: key=value, sorted, \n
        pairs = sorted(parsed_dict.items())
        data_check_string = "\n".join(f"{k}={v}" for k, v in pairs)
        # secret_key = HMAC-SHA256("WebAppData", bot_token) — ключ "WebAppData", сообщение bot_token
        secret_key = hmac.new(
            b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
        ).digest()
        calculated_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()
        if calculated_hash != received_hash:
            return None
        # Извлекаем user (JSON)
        user_str = parsed_dict.get("user")
        if not user_str:
            return None
        import json
        user = json.loads(user_str)
        return {"user_id": user.get("id"), "user": user}
    except Exception as e:
        logger.warning("initData verification failed: %s", e)
        return None


def get_user_by_telegram_id(telegram_id: str):
    url = f"{REMNAWAVE_API_URL}/api/users/by-telegram-id/{telegram_id}"
    r = requests.get(url, headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}, timeout=10)
    if r.status_code != 200:
        return None
    data = r.json()
    users = data.get("response", [])
    return users[0] if users else None


def get_user_by_username(username: str):
    url = f"{REMNAWAVE_API_URL}/api/users/by-username/{username}"
    r = requests.get(url, headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}, timeout=10)
    if r.status_code != 200:
        return None
    return r.json().get("response")


def get_subscription_by_uuid(uuid: str):
    url = f"{REMNAWAVE_API_URL}/api/subscriptions/by-uuid/{uuid}"
    r = requests.get(url, headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}, timeout=10)
    if r.status_code != 200:
        return None
    return r.json().get("response")


def get_hwid_devices(user_uuid: str):
    url = f"{REMNAWAVE_API_URL}/api/hwid/devices/{user_uuid}"
    r = requests.get(url, headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}, timeout=10)
    if r.status_code != 200:
        return []
    data = r.json().get("response", {})
    return data.get("devices", [])


def _require_manager():
    """Проверяет initData и возвращает (verified_data, error_response)."""
    init_data = request.headers.get("X-Telegram-Init-Data") or (request.json or {}).get("initData") or ""
    verified = verify_telegram_init_data(init_data)
    if not verified:
        return None, ({"ok": False, "error": "unauthorized"}, 401)
    if verified.get("user_id") not in ALLOWED_MANAGER_IDS:
        return None, ({"ok": False, "error": "forbidden"}, 403)
    return verified, None


def api_reset_user_traffic(user_uuid: str):
    url = f"{REMNAWAVE_API_URL}/api/users/{user_uuid}/actions/reset-traffic"
    r = requests.post(url, headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}, timeout=15)
    return r.status_code == 200, r.json() if r.content else {}


def api_revoke_user_subscription(user_uuid: str):
    url = f"{REMNAWAVE_API_URL}/api/users/{user_uuid}/actions/revoke"
    r = requests.post(url, json={}, headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}, timeout=15)
    return r.status_code == 200, r.json() if r.content else {}


def api_enable_user(user_uuid: str):
    """Включение профиля пользователя (разблокировка)."""
    url = f"{REMNAWAVE_API_URL}/api/users/{user_uuid}/actions/enable"
    r = requests.post(url, json={}, headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}, timeout=15)
    return r.status_code == 200, r.json() if r.content else {}


def api_delete_hwid_device(user_uuid: str, hwid: str):
    url = f"{REMNAWAVE_API_URL}/api/hwid/devices/delete"
    r = requests.post(url, json={"userUuid": user_uuid, "hwid": hwid}, headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}, timeout=15)
    return r.status_code == 200, r.json() if r.content else {}


def api_delete_all_hwid(user_uuid: str):
    url = f"{REMNAWAVE_API_URL}/api/hwid/devices/delete-all"
    r = requests.post(url, json={"userUuid": user_uuid}, headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}, timeout=15)
    return r.status_code == 200, r.json() if r.content else {}


def _load_pending_data() -> dict:
    _PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not _PENDING_FILE.exists():
        return {}
    try:
        with open(_PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("payments pending load: %s", e)
        return {}


def _save_pending_data(data: dict) -> None:
    try:
        with open(_PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=0)
    except Exception as e:
        logger.warning("payments pending save: %s", e)


def _payments_pending_pop_candidates(*candidate_ids: str):
    """Удаляет запись об ожидающем платеже по payment_id или external_id."""
    candidates = {str(item).strip() for item in candidate_ids if str(item).strip()}
    if not candidates:
        return None
    data = _load_pending_data()
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
        _save_pending_data(data)
    return record


def _pick_first_string(*values):
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and str(value).strip():
            return str(value).strip()
    return None


def _get_platega_payload():
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    raw = (request.get_data(cache=True) or b"").decode("utf-8", errors="ignore").strip()
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except Exception:
        return None
    return decoded if isinstance(decoded, dict) else None


def _parse_platega_webhook():
    data = _get_platega_payload()
    if not data:
        return None
    tx_obj = data.get("transaction") if isinstance(data.get("transaction"), dict) else {}
    nested_obj = data.get("data") if isinstance(data.get("data"), dict) else {}
    status_raw = _pick_first_string(
        data.get("status"),
        tx_obj.get("status"),
        data.get("state"),
        data.get("paymentStatus"),
        data.get("payment_status"),
        nested_obj.get("status"),
        nested_obj.get("state"),
    )
    status = (status_raw or "").upper()
    transaction_id = _pick_first_string(
        data.get("id"),
        tx_obj.get("id"),
        data.get("transactionId"),
        data.get("transaction_id"),
        nested_obj.get("id"),
        nested_obj.get("transactionId"),
        nested_obj.get("transaction_id"),
    )
    external_id = _pick_first_string(
        data.get("externalId"),
        tx_obj.get("externalId"),
        nested_obj.get("externalId"),
        data.get("invoiceId"),
        tx_obj.get("invoiceId"),
        nested_obj.get("invoiceId"),
    )
    order_id = _pick_first_string(
        data.get("orderId"),
        data.get("order_id"),
        data.get("order"),
        data.get("merchant_order_id"),
        nested_obj.get("orderId"),
        nested_obj.get("order_id"),
        nested_obj.get("order"),
    )
    payload_id = _pick_first_string(
        data.get("payload"),
        tx_obj.get("payload"),
        nested_obj.get("payload"),
    )
    candidate_ids = []
    for value in (payload_id, transaction_id, external_id, order_id):
        if value and value not in candidate_ids:
            candidate_ids.append(value)
    return {
        "status": status,
        "transaction_id": transaction_id,
        "external_id": external_id,
        "order_id": order_id,
        "payload_id": payload_id,
        "candidate_ids": candidate_ids,
    }


def _process_paid_record(record, amount_value, provider_label: str):
    manager_id = record.get("manager_id")
    client_id = record.get("client_id")
    user_uuid = (record.get("user_uuid") or "").strip()
    amount_val = record.get("amount", amount_value)

    # После успешной оплаты: разблокировка клиента и перевыпуск подписки
    unblock_ok = revoke_ok = False
    if user_uuid and REMNAWAVE_API_URL and REMNAWAVE_API_TOKEN:
        unblock_ok, _ = api_enable_user(user_uuid)
        if unblock_ok:
            revoke_ok, _ = api_revoke_user_subscription(user_uuid)
        if not unblock_ok:
            logger.warning("%s webhook: api_enable_user failed for uuid %s", provider_label, user_uuid)
        elif not revoke_ok:
            logger.warning("%s webhook: api_revoke_user_subscription failed for uuid %s", provider_label, user_uuid)

    # Уведомление клиенту, что он разблокирован
    if client_id and BOT_TOKEN and (unblock_ok or revoke_ok):
        try:
            client_text = (
                f"✅ <b>Оплата получена</b>\n\n"
                f"Ваш аккаунт {SERVICE_NAME} разблокирован, подписка перевыпущена.\n\n"
                "Можете пользоваться сервисом."
            )
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": int(client_id),
                    "text": client_text,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
        except Exception as e:
            logger.warning("%s webhook: notify client %s: %s", provider_label, client_id, e)

    # Уведомление менеджеру
    if manager_id and BOT_TOKEN:
        try:
            auto_text = ""
            if user_uuid:
                if unblock_ok and revoke_ok:
                    auto_text = "Клиент автоматически разблокирован, подписка перевыпущена.\n\n"
                elif unblock_ok:
                    auto_text = "Клиент разблокирован. Перевыпуск подписки не удался.\n\n"
                else:
                    auto_text = "Авторазблокировка не выполнена (проверьте API).\n\n"
            text = (
                f"✅ <b>Оплата прошла успешно</b>\n\n"
                f"Сумма: <b>{amount_val} ₽</b>\n\n"
                f"{auto_text}"
            ).strip()
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": manager_id,
                    "text": text,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
            if r.status_code != 200:
                logger.warning("%s webhook: sendMessage %s %s", provider_label, r.status_code, r.text)
        except Exception as e:
            logger.exception("%s webhook: notify manager: %s", provider_label, e)


@app.route("/webhook/freekassa", methods=["GET", "POST"])
def webhook_freekassa():
    """Вебхук Freekassa: проверка подписи/IP и единая post-payment обработка."""
    remote_ip = (
        request.headers.get("X-Real-IP")
        or (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        or request.remote_addr
    )
    if remote_ip and remote_ip not in FREEEKASSA_ALLOWED_IPS:
        logger.warning("Freekassa webhook from disallowed IP: %s", remote_ip)
        return "invalid ip", 403
    merchant_id = request.values.get("MERCHANT_ID")
    amount = request.values.get("AMOUNT")
    merchant_order_id = request.values.get("MERCHANT_ORDER_ID")
    sign = request.values.get("SIGN")
    if not all([merchant_id, amount, merchant_order_id, sign]):
        return "missing params", 400
    if not FREEEKASSA_WEBHOOK_SECRET:
        return "not configured", 500
    expected = hashlib.md5(
        f"{merchant_id}:{amount}:{FREEEKASSA_WEBHOOK_SECRET}:{merchant_order_id}".encode()
    ).hexdigest()
    if sign.lower() != expected.lower():
        return "wrong sign", 400
    record = _payments_pending_pop_candidates(merchant_order_id)
    if not record:
        logger.info("Freekassa webhook: unknown or already processed order %s", merchant_order_id)
        return "YES", 200
    _process_paid_record(record, amount, "Freekassa")
    return "YES", 200


@app.route("/webhook/platega", methods=["POST"])
def webhook_platega():
    """Вебхук Platega: ищем payload/order/transaction и запускаем ту же post-payment логику."""
    parsed = _parse_platega_webhook()
    if not parsed:
        logger.warning("Platega webhook: empty or invalid body")
        return jsonify({"received": True}), 200
    candidate_ids = parsed["candidate_ids"]
    status = parsed["status"]
    if not candidate_ids:
        logger.warning("Platega webhook: no identifiers in payload")
        return jsonify({"received": True}), 200
    if status in PLATEGA_FAILED_STATUSES:
        logger.info("Platega webhook: failed status=%s ids=%s", status, candidate_ids)
        return jsonify({"received": True}), 200
    if status not in PLATEGA_SUCCESS_STATUSES:
        logger.info("Platega webhook: ignored status=%s ids=%s", status, candidate_ids)
        return jsonify({"received": True}), 200
    record = _payments_pending_pop_candidates(*candidate_ids)
    if not record:
        logger.info("Platega webhook: unknown or already processed ids=%s", candidate_ids)
        return jsonify({"received": True}), 200
    _process_paid_record(record, record.get("amount"), "Platega")
    return jsonify({"received": True}), 200


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(app.static_folder, path)


@app.route("/api/lookup", methods=["POST"])
def lookup():
    init_data = request.headers.get("X-Telegram-Init-Data") or request.json and request.json.get("initData") or ""
    verified = verify_telegram_init_data(init_data)
    if not verified:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    user_id = verified.get("user_id")
    if user_id not in ALLOWED_MANAGER_IDS:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    body = request.json or {}
    query = (body.get("query") or "").strip()
    if not query:
        return jsonify({"ok": False, "error": "query_required"}), 400

    user = None
    if query.isdigit():
        user = get_user_by_telegram_id(query)
    else:
        user = get_user_by_username(query.lstrip("@"))

    if not user:
        return jsonify({"ok": False, "error": "user_not_found"}), 404

    user_uuid = user.get("uuid")
    subscription = get_subscription_by_uuid(user_uuid) if user_uuid else None
    hwid_devices = get_hwid_devices(user_uuid) if user_uuid else []

    return jsonify({
        "ok": True,
        "user": user,
        "subscription": subscription,
        "hwid_devices": hwid_devices,
    })


@app.route("/api/actions/reset-traffic", methods=["POST"])
def action_reset_traffic():
    verified, err = _require_manager()
    if err:
        return jsonify(err[0]), err[1]
    body = request.json or {}
    user_uuid = (body.get("userUuid") or "").strip()
    if not user_uuid:
        return jsonify({"ok": False, "error": "userUuid_required"}), 400
    ok, _ = api_reset_user_traffic(user_uuid)
    return jsonify({"ok": ok, "message": "Трафик сброшен." if ok else "Ошибка API"})


@app.route("/api/actions/revoke-subscription", methods=["POST"])
def action_revoke_subscription():
    verified, err = _require_manager()
    if err:
        return jsonify(err[0]), err[1]
    body = request.json or {}
    user_uuid = (body.get("userUuid") or "").strip()
    if not user_uuid:
        return jsonify({"ok": False, "error": "userUuid_required"}), 400
    ok, _ = api_revoke_user_subscription(user_uuid)
    return jsonify({"ok": ok, "message": "Подписка перевыпущена." if ok else "Ошибка API"})


@app.route("/api/actions/hwid-delete-all", methods=["POST"])
def action_hwid_delete_all():
    verified, err = _require_manager()
    if err:
        return jsonify(err[0]), err[1]
    body = request.json or {}
    user_uuid = (body.get("userUuid") or "").strip()
    if not user_uuid:
        return jsonify({"ok": False, "error": "userUuid_required"}), 400
    ok, _ = api_delete_all_hwid(user_uuid)
    return jsonify({"ok": ok, "message": "Все устройства удалены." if ok else "Ошибка API"})


@app.route("/api/actions/hwid-delete", methods=["POST"])
def action_hwid_delete():
    verified, err = _require_manager()
    if err:
        return jsonify(err[0]), err[1]
    body = request.json or {}
    user_uuid = (body.get("userUuid") or "").strip()
    hwid = (body.get("hwid") or "").strip()
    if not user_uuid or not hwid:
        return jsonify({"ok": False, "error": "userUuid_and_hwid_required"}), 400
    ok, _ = api_delete_hwid_device(user_uuid, hwid)
    return jsonify({"ok": ok, "message": "Устройство удалено." if ok else "Ошибка API"})

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
