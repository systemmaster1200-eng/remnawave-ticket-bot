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

# Freekassa webhook
FREEEKASSA_WEBHOOK_SECRET = (os.getenv("FREEEKASSA_WEBHOOK_SECRET") or "").strip()
FREEEKASSA_ALLOWED_IPS = {
    "168.119.157.136",
    "168.119.60.227",
    "178.154.197.79",
    "51.250.54.238",
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


def _payments_pending_pop(payment_id: str):
    """Удаляет запись об ожидающем платеже и возвращает её (manager_id, amount, ...)."""
    _PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not _PENDING_FILE.exists():
        return None
    try:
        with open(_PENDING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("payments pending load: %s", e)
        return None
    record = data.pop(payment_id, None)
    if record is not None:
        try:
            with open(_PENDING_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=0)
        except Exception as e:
            logger.warning("payments pending save: %s", e)
    return record


@app.route("/webhook/freekassa", methods=["GET", "POST"])
def webhook_freekassa():
    """Вебхук Freekassa: оповещение об оплате. Проверка подписи и IP, уведомление менеджеру в Telegram."""
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
    record = _payments_pending_pop(merchant_order_id)
    if not record:
        logger.info("Freekassa webhook: unknown or already processed order %s", merchant_order_id)
        return "YES", 200
    manager_id = record.get("manager_id")
    client_id = record.get("client_id")
    user_uuid = (record.get("user_uuid") or "").strip()
    amount_val = record.get("amount", amount)

    # После успешной оплаты: разблокировка клиента и перевыпуск подписки
    unblock_ok = revoke_ok = False
    if user_uuid and REMNAWAVE_API_URL and REMNAWAVE_API_TOKEN:
        unblock_ok, _ = api_enable_user(user_uuid)
        if unblock_ok:
            revoke_ok, _ = api_revoke_user_subscription(user_uuid)
        if not unblock_ok:
            logger.warning("Freekassa webhook: api_enable_user failed for uuid %s", user_uuid)
        elif not revoke_ok:
            logger.warning("Freekassa webhook: api_revoke_user_subscription failed for uuid %s", user_uuid)

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
            logger.warning("Freekassa webhook: notify client %s: %s", client_id, e)

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
                logger.warning("Freekassa webhook: sendMessage %s %s", r.status_code, r.text)
        except Exception as e:
            logger.exception("Freekassa webhook: notify manager: %s", e)
    return "YES", 200


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


def _payments_pending_pop(payment_id: str):
    """Удаляет запись об ожидающем платеже и возвращает её (manager_id, amount, ...)."""
    _PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not _PENDING_FILE.exists():
        return None
    try:
        with open(_PENDING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("payments pending load: %s", e)
        return None
    record = data.pop(payment_id, None)
    if record is not None:
        try:
            with open(_PENDING_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=0)
        except Exception as e:
            logger.warning("payments pending save: %s", e)
    return record


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
