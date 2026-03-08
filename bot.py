#!/usr/bin/env python3
"""
Telegram бот для менеджеров Remnawave
Позволяет получать информацию о пользователях и их подписках по Telegram ID или username
"""

import os
import base64
import tempfile

# Загружаем .env до импорта ai_support, чтобы GROQ_API_KEY и др. были доступны
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import logging
import time
import requests
from datetime import datetime
from typing import Optional, Dict, Any, List
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp, MenuButtonCommands
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    PicklePersistence,
)

try:
    from payments.freekassa import get_freekassa_provider
    from payments.storage import pending_add
    _PAYMENTS_AVAILABLE = True
except ImportError:
    _PAYMENTS_AVAILABLE = False
    get_freekassa_provider = None
    pending_add = None

try:
    from ai_support import (
        is_ai_enabled,
        build_client_context_for_ai,
        get_system_prompt,
        get_conversation_history,
        add_to_conversation_history,
        get_last_user_message,
        add_global_example,
        transcribe_voice_groq,
        get_ai_reply,
        check_ai_key_at_startup,
        AI_PROVIDER,
    )
    _AI_SUPPORT_AVAILABLE = True
except ImportError:
    _AI_SUPPORT_AVAILABLE = False
    check_ai_key_at_startup = lambda: False
    is_ai_enabled = lambda: False
    build_client_context_for_ai = None
    get_system_prompt = None
    get_conversation_history = lambda bot_data, client_id: []
    add_to_conversation_history = lambda bot_data, client_id, role, content: None
    get_last_user_message = lambda bot_data, client_id: None
    add_global_example = lambda bot_data, user_msg, assistant_msg: None
    transcribe_voice_groq = None
    get_ai_reply = None
    AI_PROVIDER = "groq"

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация из переменных окружения
BOT_TOKEN = os.getenv('BOT_TOKEN')
REMNAWAVE_API_URL = os.getenv('REMNAWAVE_API_URL', '').rstrip('/')
REMNAWAVE_API_TOKEN = os.getenv('REMNAWAVE_API_TOKEN')
BEDOLAGA_API_URL = (os.getenv('BEDOLAGA_API_URL') or '').strip().rstrip('/')
BEDOLAGA_API_TOKEN = (os.getenv('BEDOLAGA_API_TOKEN') or '').strip()
ALLOWED_MANAGER_IDS = set(
    int(id.strip()) for id in os.getenv('ALLOWED_MANAGER_IDS', '').split(',') if id.strip()
)
MINI_APP_DOMAIN = (os.getenv('MINI_APP_DOMAIN') or '').strip()
MINI_APP_URL = ('https://' + MINI_APP_DOMAIN) if MINI_APP_DOMAIN else ''
SERVICE_NAME = (os.getenv('SERVICE_NAME') or 'Remnawave').strip() or 'Remnawave'
_SUPPORT_GROUP_RAW = (os.getenv('SUPPORT_GROUP_ID') or '').strip()
SUPPORT_GROUP_ID = int(_SUPPORT_GROUP_RAW) if _SUPPORT_GROUP_RAW else None


def _support_chat_ids():
    """Возвращает список chat_id для попытки (полный формат супергруппы и как в .env)."""
    if SUPPORT_GROUP_ID is None:
        return []
    ids = [SUPPORT_GROUP_ID]
    # В API супергруппы часто имеют id вида -100xxxxxxxxxx; если передан короткий -N, пробуем -100N
    if -10**9 <= SUPPORT_GROUP_ID < 0 and SUPPORT_GROUP_ID > -10**10:
        full_id = -(10**12 + abs(SUPPORT_GROUP_ID))
        if full_id not in ids:
            ids.append(full_id)
    return ids

# Проверка обязательных переменных
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен")
if not REMNAWAVE_API_URL:
    raise ValueError("REMNAWAVE_API_URL не установлен")
if not REMNAWAVE_API_TOKEN:
    raise ValueError("REMNAWAVE_API_TOKEN не установлен")
if not ALLOWED_MANAGER_IDS:
    raise ValueError("ALLOWED_MANAGER_IDS не установлен (должен содержать хотя бы один ID)")


def check_access(user_id: int) -> bool:
    """Проверка доступа менеджера"""
    return user_id in ALLOWED_MANAGER_IDS


def _bedolaga_configured() -> bool:
    """Проверка, настроен ли веб-API Bedolaga (баланс и транзакции)."""
    return bool(BEDOLAGA_API_URL and BEDOLAGA_API_TOKEN)


def get_bedolaga_user(telegram_id: str) -> Optional[Dict[str, Any]]:
    """Получение пользователя из веб-API Bedolaga по Telegram ID. Возвращает данные с balance_* и id."""
    if not _bedolaga_configured():
        return None
    try:
        url = f"{BEDOLAGA_API_URL}/users/{telegram_id}"
        headers = {"X-API-Key": BEDOLAGA_API_TOKEN}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json()
        if response.status_code == 404:
            return None
        logger.warning("Bedolaga API users: %s %s", response.status_code, response.text[:200])
        return None
    except Exception as e:
        logger.warning("get_bedolaga_user %s: %s", telegram_id, e)
        return None


def get_bedolaga_transactions(bedolaga_user_id: int, limit: int = 30) -> List[Dict[str, Any]]:
    """Список транзакций пользователя из веб-API Bedolaga (user_id — внутренний id из Bedolaga)."""
    if not _bedolaga_configured():
        return []
    try:
        url = f"{BEDOLAGA_API_URL}/transactions"
        headers = {"X-API-Key": BEDOLAGA_API_TOKEN}
        params = {"user_id": bedolaga_user_id, "limit": limit, "offset": 0}
        response = requests.get(url, headers=headers, params=params, timeout=10)
        if response.status_code != 200:
            logger.warning("Bedolaga API transactions: %s %s", response.status_code, response.text[:200])
            return []
        data = response.json()
        return data.get("items") or []
    except Exception as e:
        logger.warning("get_bedolaga_transactions %s: %s", bedolaga_user_id, e)
        return []


def _format_bedolaga_balance(bedolaga_user: Dict[str, Any]) -> str:
    """Строка баланса из ответа Bedolaga (balance_rubles или balance_kopeks)."""
    rub = bedolaga_user.get("balance_rubles")
    if rub is not None:
        return f"{float(rub):.2f}"
    kopeks = bedolaga_user.get("balance_kopeks", 0)
    return f"{int(kopeks) / 100:.2f}"


def _format_bedolaga_transactions_message(transactions: List[Dict[str, Any]], max_len: int = 3800) -> str:
    """Форматирует список транзакций Bedolaga в один текст (обрезает по max_len)."""
    if not transactions:
        return "📜 <b>Транзакции (Bedolaga)</b>\n\nНет транзакций."
    lines = ["📜 <b>Транзакции (Bedolaga)</b>\n"]
    for t in transactions:
        amount = t.get("amount_rubles") or (t.get("amount_kopeks", 0) / 100)
        typ = t.get("type") or "—"
        desc = (t.get("description") or "—")[:50]
        created = (t.get("created_at") or "—")[:19].replace("T", " ")
        lines.append(f"• {created} · {amount:.2f} ₽ · {typ}\n  {desc}")
    text = "\n".join(lines)
    if len(text) > max_len:
        text = text[: max_len - 50] + "\n\n… (показаны последние записи)"
    return text


def _set_awaiting_invoice_by_manager(context: ContextTypes.DEFAULT_TYPE, manager_id: int, payload: dict) -> None:
    """Дублируем состояние «ожидание суммы» в bot_data, чтобы работало в групповых чатах (топики)."""
    if not getattr(context.application, "bot_data", None):
        return
    bot_data = context.application.bot_data
    key = "awaiting_invoice_by_manager"
    if key not in bot_data:
        bot_data[key] = {}
    bot_data[key][manager_id] = payload


def _get_awaiting_invoice(context: ContextTypes.DEFAULT_TYPE, manager_id: int) -> Optional[dict]:
    """Получить состояние «ожидание суммы» из user_data или bot_data (для групповых чатов)."""
    awaiting = (context.user_data or {}).get("awaiting_invoice")
    if awaiting:
        return awaiting
    bot_data = getattr(context.application, "bot_data", None) or {}
    by_manager = bot_data.get("awaiting_invoice_by_manager") or {}
    return by_manager.get(manager_id)


def _clear_awaiting_invoice(context: ContextTypes.DEFAULT_TYPE, manager_id: int) -> None:
    """Очистить состояние «ожидание суммы» в user_data и bot_data."""
    context.user_data.pop("awaiting_invoice", None)
    bot_data = getattr(context.application, "bot_data", None)
    if bot_data:
        by_manager = bot_data.get("awaiting_invoice_by_manager")
        if by_manager is not None and manager_id in by_manager:
            del by_manager[manager_id]


def format_bytes(bytes_value: int) -> str:
    """Форматирование байтов в читаемый формат"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_value < 1024.0:
            return f"{bytes_value:.2f} {unit}"
        bytes_value /= 1024.0
    return f"{bytes_value:.2f} PB"


def format_datetime(dt_str: Optional[str]) -> str:
    """Форматирование даты и времени"""
    if not dt_str:
        return "Не указано"
    try:
        dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        return dt.strftime('%d.%m.%Y %H:%M:%S')
    except:
        return dt_str


def get_user_by_telegram_id(telegram_id: str) -> Optional[Dict[str, Any]]:
    """Получение пользователя по Telegram ID. Поддерживает response как массив или один объект."""
    try:
        url = f"{REMNAWAVE_API_URL}/api/users/by-telegram-id/{telegram_id}"
        headers = {"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            raw = data.get("response")
            if raw is None:
                return None
            # API может вернуть массив пользователей или один объект
            if isinstance(raw, list):
                return raw[0] if raw else None
            if isinstance(raw, dict) and raw.get("uuid"):
                return raw
            return None
        elif response.status_code == 404:
            return None
        else:
            logger.error(
                "Ошибка API при получении пользователя по Telegram ID: %s, body=%s",
                response.status_code,
                response.text[:500] if response.text else "",
            )
            return None
    except Exception as e:
        logger.error("Исключение при получении пользователя по Telegram ID %s: %s", telegram_id, e)
        return None


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """Получение пользователя по username"""
    try:
        url = f"{REMNAWAVE_API_URL}/api/users/by-username/{username}"
        headers = {"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            return data.get('response')
        elif response.status_code == 404:
            return None
        else:
            logger.error(f"Ошибка API при получении пользователя по username: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"Исключение при получении пользователя по username: {e}")
        return None


def get_subscription_by_uuid(uuid: str) -> Optional[Dict[str, Any]]:
    """Получение подписки по UUID пользователя"""
    try:
        url = f"{REMNAWAVE_API_URL}/api/subscriptions/by-uuid/{uuid}"
        headers = {"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            return data.get('response')
        elif response.status_code == 404:
            return None
        else:
            logger.error(f"Ошибка API при получении подписки: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"Исключение при получении подписки: {e}")
        return None


def get_subscription_page_configs() -> Optional[List[Dict[str, Any]]]:
    """Список тарифов (конфигов страницы подписки) из API — для выдачи клиенту по запросу «какие тарифы»."""
    try:
        url = f"{REMNAWAVE_API_URL}/api/subscription-page-configs"
        headers = {"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return None
        data = response.json()
        resp = data.get("response") or {}
        configs = resp.get("configs") or []
        return configs if isinstance(configs, list) else None
    except Exception as e:
        logger.debug("get_subscription_page_configs: %s", e)
        return None


def get_hwid_devices(user_uuid: str) -> Optional[List[Dict[str, Any]]]:
    """Получение HWID устройств пользователя"""
    try:
        url = f"{REMNAWAVE_API_URL}/api/hwid/devices/{user_uuid}"
        headers = {"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            response_data = data.get('response', {})
            devices = response_data.get('devices', [])
            return devices
        elif response.status_code == 404:
            return []
        else:
            logger.error(f"Ошибка API при получении HWID устройств: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"Исключение при получении HWID устройств: {e}")
        return None


def api_reset_user_traffic(user_uuid: str) -> tuple:
    """Сброс трафика пользователя. Возвращает (успех, сообщение)."""
    try:
        url = f"{REMNAWAVE_API_URL}/api/users/{user_uuid}/actions/reset-traffic"
        r = requests.post(url, headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}, timeout=15)
        if r.status_code == 200:
            return True, "Трафик сброшен."
        return False, f"Ошибка API: {r.status_code}"
    except Exception as e:
        logger.exception("reset_user_traffic")
        return False, str(e)


def api_revoke_user_subscription(user_uuid: str) -> tuple:
    """Перевыпуск подписки пользователя. Возвращает (успех, сообщение)."""
    try:
        url = f"{REMNAWAVE_API_URL}/api/users/{user_uuid}/actions/revoke"
        r = requests.post(url, json={}, headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}, timeout=15)
        if r.status_code == 200:
            return True, "Подписка перевыпущена."
        return False, f"Ошибка API: {r.status_code}"
    except Exception as e:
        logger.exception("revoke_user_subscription")
        return False, str(e)


def api_delete_hwid_device(user_uuid: str, hwid: str) -> tuple:
    """Удаление одного HWID устройства. Возвращает (успех, сообщение)."""
    try:
        url = f"{REMNAWAVE_API_URL}/api/hwid/devices/delete"
        r = requests.post(url, json={"userUuid": user_uuid, "hwid": hwid}, headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}, timeout=15)
        if r.status_code == 200:
            return True, "Устройство удалено."
        return False, f"Ошибка API: {r.status_code}"
    except Exception as e:
        logger.exception("delete_hwid_device")
        return False, str(e)


def api_delete_all_hwid(user_uuid: str) -> tuple:
    """Удаление всех HWID устройств пользователя. Возвращает (успех, сообщение)."""
    try:
        url = f"{REMNAWAVE_API_URL}/api/hwid/devices/delete-all"
        r = requests.post(url, json={"userUuid": user_uuid}, headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}, timeout=15)
        if r.status_code == 200:
            return True, "Все устройства удалены."
        return False, f"Ошибка API: {r.status_code}"
    except Exception as e:
        logger.exception("delete_all_hwid")
        return False, str(e)


def api_disable_user(user_uuid: str) -> tuple:
    """Отключение профиля пользователя в Remnawave. Возвращает (успех, сообщение)."""
    try:
        url = f"{REMNAWAVE_API_URL}/api/users/{user_uuid}/actions/disable"
        r = requests.post(url, json={}, headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}, timeout=15)
        if r.status_code == 200:
            return True, "Профиль отключён (заблокирован)."
        return False, f"Ошибка API: {r.status_code}"
    except Exception as e:
        logger.exception("disable_user")
        return False, str(e)


def api_enable_user(user_uuid: str) -> tuple:
    """Включение профиля пользователя в Remnawave. Возвращает (успех, сообщение)."""
    try:
        url = f"{REMNAWAVE_API_URL}/api/users/{user_uuid}/actions/enable"
        r = requests.post(url, json={}, headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}, timeout=15)
        if r.status_code == 200:
            return True, "Профиль включён (разблокирован)."
        return False, f"Ошибка API: {r.status_code}"
    except Exception as e:
        logger.exception("enable_user")
        return False, str(e)


def get_internal_squads() -> Optional[List[Dict[str, Any]]]:
    """Список внутренних сквадов Remnawave."""
    try:
        url = f"{REMNAWAVE_API_URL}/api/internal-squads"
        r = requests.get(url, headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        resp = data.get("response") or {}
        return resp.get("internalSquads") or []
    except Exception as e:
        logger.exception("get_internal_squads: %s", e)
        return None


def get_external_squads() -> Optional[List[Dict[str, Any]]]:
    """Список внешних сквадов Remnawave."""
    try:
        url = f"{REMNAWAVE_API_URL}/api/external-squads"
        r = requests.get(url, headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        resp = data.get("response") or {}
        return resp.get("externalSquads") or []
    except Exception as e:
        logger.exception("get_external_squads: %s", e)
        return None


def add_user_to_internal_squad(squad_uuid: str, user_uuid: str) -> tuple:
    """Добавить пользователя во внутренний сквад. Возвращает (успех, сообщение)."""
    try:
        url = f"{REMNAWAVE_API_URL}/api/internal-squads/{squad_uuid}/bulk-actions/add-users"
        r = requests.post(
            url,
            json={"userUuids": [user_uuid]},
            headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"},
            timeout=15,
        )
        if r.status_code == 200:
            return True, "Пользователь добавлен во внутренний сквад."
        return False, f"Ошибка API: {r.status_code}"
    except Exception as e:
        logger.exception("add_user_to_internal_squad")
        return False, str(e)


def add_user_to_external_squad(squad_uuid: str, user_uuid: str) -> tuple:
    """Добавить пользователя во внешний сквад (выдать сквад клиенту). Возвращает (успех, сообщение)."""
    try:
        url = f"{REMNAWAVE_API_URL}/api/external-squads/{squad_uuid}/bulk-actions/add-users"
        r = requests.post(
            url,
            json={"userUuids": [user_uuid]},
            headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"},
            timeout=15,
        )
        if r.status_code == 200:
            return True, "Сквад выдан клиенту."
        return False, f"Ошибка API: {r.status_code}"
    except Exception as e:
        logger.exception("add_user_to_external_squad")
        return False, str(e)


def _is_user_disabled(user: Dict[str, Any], subscription: Optional[Dict[str, Any]]) -> bool:
    """Проверяет, заблокирован ли пользователь (по status / userStatus)."""
    status = (user or {}).get("status") or ""
    sub_user = (subscription or {}).get("user", {}) if subscription else {}
    user_status = sub_user.get("userStatus") or ""
    disabled_values = ("DISABLED", "INACTIVE", "DEACTIVATED", "BANNED", "disabled", "inactive")
    return status.upper() in disabled_values or user_status.upper() in disabled_values


# Ключи секций для кнопок (callback_data до 64 байт)
SECTIONS = ('profile', 'traffic', 'dates', 'subscription', 'hwid')


def _section_profile(user: Dict[str, Any]) -> str:
    """Текст секции: профиль пользователя"""
    lines = [
        "👤 <b>ПРОФИЛЬ</b>\n",
        f"🆔 <b>UUID:</b> <code>{user.get('uuid', 'N/A')}</code>",
        f"📝 <b>Short UUID:</b> <code>{user.get('shortUuid', 'N/A')}</code>",
        f"🔢 <b>ID:</b> {user.get('id', 'N/A')}",
        f"👤 <b>Username:</b> @{user.get('username', 'N/A')}",
        f"📧 <b>Email:</b> {user.get('email') or 'Не указан'}",
        f"💬 <b>Telegram ID:</b> {user.get('telegramId') or 'Не указан'}",
        f"📊 <b>Статус:</b> {user.get('status', 'N/A')}",
        f"🏷️ <b>Тег:</b> {user.get('tag') or 'Не указан'}",
        f"📄 <b>Описание:</b> {user.get('description') or 'Не указано'}",
    ]
    if user.get('hwidDeviceLimit'):
        lines.append(f"📱 <b>Лимит устройств:</b> {user.get('hwidDeviceLimit')}")
    if user.get('subLastUserAgent'):
        lines.append(f"🌐 <b>Последний User-Agent:</b> {user.get('subLastUserAgent')}")
    active_squads = user.get('activeInternalSquads', [])
    if active_squads:
        lines.append("\n👥 <b>Активные сквады:</b>")
        for squad in active_squads:
            lines.append(f"  • {squad.get('name', 'N/A')}")
    return "\n".join(lines)


def _section_traffic(user: Dict[str, Any]) -> str:
    """Текст секции: трафик"""
    lines = ["📊 <b>ТРАФИК</b>\n"]
    user_traffic = user.get('userTraffic', {})
    if user_traffic:
        used = user_traffic.get('usedTrafficBytes', 0)
        lifetime = user_traffic.get('lifetimeUsedTrafficBytes', 0)
        limit = user.get('trafficLimitBytes', 0)
        lines.extend([
            f"📥 <b>Использовано:</b> {format_bytes(used)}",
            f"📈 <b>Всего использовано:</b> {format_bytes(lifetime)}",
            f"📊 <b>Лимит:</b> {format_bytes(limit) if limit > 0 else 'Безлимит'}",
            f"🔄 <b>Стратегия сброса:</b> {user.get('trafficLimitStrategy', 'NO_RESET')}",
        ])
        if user_traffic.get('onlineAt'):
            lines.append(f"🟢 <b>Онлайн:</b> {format_datetime(user_traffic.get('onlineAt'))}")
        if user_traffic.get('firstConnectedAt'):
            lines.append(f"🔌 <b>Первое подключение:</b> {format_datetime(user_traffic.get('firstConnectedAt'))}")
    else:
        lines.append("Нет данных о трафике.")
    return "\n".join(lines)


def _section_dates(user: Dict[str, Any]) -> str:
    """Текст секции: даты"""
    lines = [
        "📅 <b>ДАТЫ</b>\n",
        f"⏰ <b>Истекает:</b> {format_datetime(user.get('expireAt'))}",
        f"📅 <b>Создан:</b> {format_datetime(user.get('createdAt'))}",
        f"🔄 <b>Обновлен:</b> {format_datetime(user.get('updatedAt'))}",
    ]
    if user.get('subRevokedAt'):
        lines.append(f"🚫 <b>Подписка отозвана:</b> {format_datetime(user.get('subRevokedAt'))}")
    if user.get('subLastOpenedAt'):
        lines.append(f"📱 <b>Последнее открытие:</b> {format_datetime(user.get('subLastOpenedAt'))}")
    if user.get('lastTrafficResetAt'):
        lines.append(f"🔄 <b>Последний сброс трафика:</b> {format_datetime(user.get('lastTrafficResetAt'))}")
    return "\n".join(lines)


def _section_subscription(subscription: Optional[Dict[str, Any]]) -> str:
    """Текст секции: подписка"""
    lines = ["🔗 <b>ПОДПИСКА</b>\n"]
    if not subscription:
        lines.append("Данные о подписке недоступны.")
        return "\n".join(lines)
    sub_user = subscription.get('user', {})
    lines.append(f"✅ <b>Найдена:</b> {'Да' if subscription.get('isFound') else 'Нет'}")
    if sub_user:
        lines.extend([
            f"📊 <b>Дней осталось:</b> {sub_user.get('daysLeft', 'N/A')}",
            f"📥 <b>Использовано:</b> {sub_user.get('trafficUsed', 'N/A')}",
            f"📊 <b>Лимит:</b> {sub_user.get('trafficLimit', 'N/A')}",
            f"📈 <b>Всего использовано:</b> {sub_user.get('lifetimeTrafficUsed', 'N/A')}",
            f"✅ <b>Активна:</b> {'Да' if sub_user.get('isActive') else 'Нет'}",
            f"📊 <b>Статус:</b> {sub_user.get('userStatus', 'N/A')}",
        ])
    url = subscription.get('subscriptionUrl')
    if url:
        lines.append(f"\n🔗 <b>URL подписки:</b>\n<code>{url}</code>")
    return "\n".join(lines)


def _section_hwid(hwid_devices: Optional[List[Dict[str, Any]]]) -> str:
    """Текст секции: HWID устройства"""
    lines = ["📱 <b>ПРИВЯЗАННЫЕ УСТРОЙСТВА (HWID)</b>\n"]
    if hwid_devices is None:
        lines.append("Данные недоступны.")
        return "\n".join(lines)
    if not hwid_devices:
        lines.append("Устройства не найдены.")
        return "\n".join(lines)
    lines.append(f"Всего устройств: {len(hwid_devices)}\n")
    for i, device in enumerate(hwid_devices, 1):
        lines.append(f"<b>Устройство {i}:</b>")
        lines.append(f"  🔑 <b>HWID:</b> <code>{device.get('hwid', 'N/A')}</code>")
        if device.get('platform'):
            lines.append(f"  📱 <b>Платформа:</b> {device.get('platform')}")
        if device.get('osVersion'):
            lines.append(f"  💻 <b>ОС:</b> {device.get('osVersion')}")
        if device.get('deviceModel'):
            lines.append(f"  🖥️ <b>Модель:</b> {device.get('deviceModel')}")
        if device.get('userAgent'):
            ua = device.get('userAgent', '')
            lines.append(f"  🌐 <b>User-Agent:</b> {ua[:60]}{'...' if len(ua) > 60 else ''}")
        if device.get('createdAt'):
            lines.append(f"  📅 <b>Добавлено:</b> {format_datetime(device.get('createdAt'))}")
        if device.get('updatedAt'):
            lines.append(f"  🔄 <b>Обновлено:</b> {format_datetime(device.get('updatedAt'))}")
        if i < len(hwid_devices):
            lines.append("")
    return "\n".join(lines)


def get_section_text(section: str, user: Dict[str, Any], subscription: Optional[Dict[str, Any]], hwid_devices: Optional[List[Dict[str, Any]]]) -> str:
    """Возвращает текст для выбранной секции."""
    if section == 'profile':
        return _section_profile(user)
    if section == 'traffic':
        return _section_traffic(user)
    if section == 'dates':
        return _section_dates(user)
    if section == 'subscription':
        return _section_subscription(subscription)
    if section == 'hwid':
        return _section_hwid(hwid_devices)
    return "Неизвестная секция."


def build_section_keyboard(current_section: str, last_user_data: Optional[Dict[str, Any]] = None) -> InlineKeyboardMarkup:
    """Клавиатура навигации по секциям + действия (сброс трафика, перевыпуск, HWID)."""
    labels = [
        ("👤 Профиль", "profile"),
        ("📊 Трафик", "traffic"),
        ("📅 Даты", "dates"),
        ("🔗 Подписка", "subscription"),
        ("📱 Устройства", "hwid"),
    ]
    row1, row2 = [], []
    for i, (label, section) in enumerate(labels):
        text = f"✓ {label}" if section == current_section else label
        btn = InlineKeyboardButton(text=text, callback_data=f"s:{section}")
        if i < 3:
            row1.append(btn)
        else:
            row2.append(btn)
    rows = [row1, row2]
    # Действия (только если есть данные пользователя)
    if last_user_data and last_user_data.get("user", {}).get("uuid"):
        user = last_user_data.get("user", {})
        sub = last_user_data.get("subscription")
        block_btn = (
            InlineKeyboardButton("🔓 Разблокировать", callback_data="act:enable")
            if _is_user_disabled(user, sub)
            else InlineKeyboardButton("🔒 Заблокировать", callback_data="act:disable")
        )
        rows.append([
            InlineKeyboardButton("🔄 Сброс трафика", callback_data="act:reset_traffic"),
            InlineKeyboardButton("🔗 Перевыпуск подписки", callback_data="act:revoke_sub"),
        ])
        invoice_btn = InlineKeyboardButton("💰 Выставить счёт", callback_data="act:invoice")
        rows.append([block_btn, invoice_btn])
        if last_user_data.get("bedolaga_user"):
            rows.append([InlineKeyboardButton("📜 Транзакции (Bedolaga)", callback_data="act:bedolaga_tx")])
        rows.append([InlineKeyboardButton("👥 Сквады", callback_data="act:squads")])
        rows.append([InlineKeyboardButton("🗑 Удалить все HWID", callback_data="act:hwid_all")])
        hwid_devices = last_user_data.get("hwid_devices") or []
        if current_section == "hwid" and hwid_devices:
            del_row = []
            for i in range(min(len(hwid_devices), 8)):
                del_row.append(InlineKeyboardButton(f"Уд. {i + 1}", callback_data=f"hwid_del:{i}"))
            if del_row:
                rows.append(del_row)
    return InlineKeyboardMarkup(rows)


def _get_message_content(update: Update) -> Optional[tuple]:
    """Возвращает (type, payload) для сообщения: ('text', str) или ('photo', file_id, caption) и т.д."""
    if not update.message:
        return None
    msg = update.message
    if msg.text:
        return ("text", (msg.text or "").strip())
    if msg.caption:
        return ("caption_only", (msg.caption or "").strip())
    if msg.photo:
        return ("photo", msg.photo[-1].file_id, (msg.caption or "").strip())
    if msg.document:
        return ("document", msg.document.file_id, (msg.caption or "").strip())
    if msg.video:
        return ("video", msg.video.file_id, (msg.caption or "").strip())
    if msg.voice:
        return ("voice", msg.voice.file_id, "")
    if msg.audio:
        return ("audio", msg.audio.file_id, (msg.caption or "").strip())
    return None


async def _send_content_to_topic(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_thread_id: int,
    content: tuple,
    prefix: str = "",
) -> bool:
    """Отправляет контент (результат _get_message_content) в топик. prefix — подпись перед текстом."""
    try:
        kind = content[0]
        if kind == "text":
            text = content[1]
            body = f"{prefix}\n\n{text}" if prefix else text
            await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                text=body,
                parse_mode="HTML" if prefix else None,
            )
        elif kind == "caption_only":
            text = content[1]
            body = f"{prefix}\n\n{text}" if prefix else text
            await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                text=body,
                parse_mode="HTML" if prefix else None,
            )
        elif kind == "photo":
            file_id, caption = content[1], content[2]
            cap = f"{prefix}\n\n{caption}" if (prefix or caption) else (prefix or caption or None)
            await context.bot.send_photo(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                photo=file_id,
                caption=cap,
                parse_mode="HTML" if cap else None,
            )
        elif kind == "document":
            file_id, caption = content[1], content[2]
            cap = f"{prefix}\n\n{caption}" if (prefix or caption) else (prefix or caption or None)
            await context.bot.send_document(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                document=file_id,
                caption=cap,
                parse_mode="HTML" if cap else None,
            )
        elif kind == "video":
            file_id, caption = content[1], content[2]
            cap = f"{prefix}\n\n{caption}" if (prefix or caption) else (prefix or caption or None)
            await context.bot.send_video(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                video=file_id,
                caption=cap,
                parse_mode="HTML" if cap else None,
            )
        elif kind == "voice":
            await context.bot.send_voice(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                voice=content[1],
                caption=prefix or None,
                parse_mode="HTML" if prefix else None,
            )
        elif kind == "audio":
            await context.bot.send_audio(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                audio=content[1],
                caption=prefix or content[2] or None,
                parse_mode="HTML" if prefix else None,
            )
        else:
            return False
        return True
    except Exception as e:
        logger.warning("_send_content_to_topic: %s", e)
        return False


async def _forward_content_to_client(
    context: ContextTypes.DEFAULT_TYPE,
    client_id: int,
    content: tuple,
    manager_name: str,
) -> bool:
    """Пересылает контент от менеджера клиенту с подписью «Поддержка (WarpX)»."""
    prefix = "💬 <b>Поддержка</b> (WarpX)"
    try:
        kind = content[0]
        if kind == "text":
            text = content[1]
            await context.bot.send_message(
                chat_id=client_id,
                text=f"{prefix}:\n\n{text}",
                parse_mode="HTML",
            )
        elif kind == "caption_only":
            text = content[1]
            await context.bot.send_message(
                chat_id=client_id,
                text=f"{prefix}:\n\n{text}",
                parse_mode="HTML",
            )
        elif kind == "photo":
            file_id, caption = content[1], content[2]
            cap = f"{prefix}\n\n{caption}" if caption else prefix
            await context.bot.send_photo(
                chat_id=client_id,
                photo=file_id,
                caption=cap,
                parse_mode="HTML",
            )
        elif kind == "document":
            file_id, caption = content[1], content[2]
            cap = f"{prefix}\n\n{caption}" if caption else prefix
            await context.bot.send_document(
                chat_id=client_id,
                document=file_id,
                caption=cap,
                parse_mode="HTML",
            )
        elif kind == "video":
            file_id, caption = content[1], content[2]
            cap = f"{prefix}\n\n{caption}" if caption else prefix
            await context.bot.send_video(
                chat_id=client_id,
                video=file_id,
                caption=cap,
                parse_mode="HTML",
            )
        elif kind == "voice":
            await context.bot.send_voice(
                chat_id=client_id,
                voice=content[1],
                caption=prefix,
                parse_mode="HTML",
            )
        elif kind == "audio":
            await context.bot.send_audio(
                chat_id=client_id,
                audio=content[1],
                caption=prefix or content[2] or None,
                parse_mode="HTML",
            )
        else:
            return False
        return True
    except Exception as e:
        logger.warning("_forward_content_to_client: %s", e)
        return False


def build_support_keyboard(
    client_id: int,
    current_section: str,
    last_user_data: Dict[str, Any],
    ai_stopped: bool = False,
    support_blocked: bool = False,
) -> InlineKeyboardMarkup:
    """Клавиатура для сообщения от клиента в поддержку: секции + действия + блок в ТП + ИИ + Завершить тикет."""
    labels = [
        ("👤 Профиль", "profile"),
        ("📊 Трафик", "traffic"),
        ("📅 Даты", "dates"),
        ("🔗 Подписка", "subscription"),
        ("📱 Устройства", "hwid"),
    ]
    row1, row2 = [], []
    for i, (label, section) in enumerate(labels):
        text = f"✓ {label}" if section == current_section else label
        btn = InlineKeyboardButton(text=text, callback_data=f"sup:{client_id}:{section}")
        if i < 3:
            row1.append(btn)
        else:
            row2.append(btn)
    rows = [row1, row2]
    # Кнопки действий показываем всегда (при отсутствии пользователя в API по нажатию будет сообщение)
    user = (last_user_data or {}).get("user") or {}
    sub = (last_user_data or {}).get("subscription")
    block_btn = (
        InlineKeyboardButton("🔓 Разблокировать", callback_data=f"sup_act:{client_id}:enable")
        if user and _is_user_disabled(user, sub)
        else InlineKeyboardButton("🔒 Заблокировать", callback_data=f"sup_act:{client_id}:disable")
    )
    rows.append([
        InlineKeyboardButton("🔄 Сброс трафика", callback_data=f"sup_act:{client_id}:reset_traffic"),
        InlineKeyboardButton("🔗 Перевыпуск", callback_data=f"sup_act:{client_id}:revoke_sub"),
    ])
    rows.append([block_btn, InlineKeyboardButton("💰 Выставить счёт", callback_data=f"sup_act:{client_id}:invoice")])
    if (last_user_data or {}).get("bedolaga_user"):
        rows.append([InlineKeyboardButton("📜 Транзакции (Bedolaga)", callback_data=f"sup_act:{client_id}:bedolaga_tx")])
    rows.append([InlineKeyboardButton("👥 Сквады", callback_data=f"sup_act:{client_id}:squads")])
    rows.append([InlineKeyboardButton("🗑 Удалить все HWID", callback_data=f"sup_act:{client_id}:hwid_all")])
    hwid_devices = (last_user_data or {}).get("hwid_devices") or []
    if current_section == "hwid" and hwid_devices:
        del_row = [InlineKeyboardButton(f"Уд. {i + 1}", callback_data=f"sup_hwid:{client_id}:{i}") for i in range(min(len(hwid_devices), 8))]
        if del_row:
            rows.append(del_row)
    # Заблокировать в ТП / Разблокировать в ТП (клиент не сможет писать в поддержку)
    if support_blocked:
        rows.append([InlineKeyboardButton("✅ Разблокировать в ТП", callback_data=f"sup_act:{client_id}:unblock_support")])
    else:
        rows.append([InlineKeyboardButton("🚫 Заблокировать в ТП", callback_data=f"sup_act:{client_id}:block_support")])
    # Остановить ИИ / Включить ИИ — перейти в чат самому
    if ai_stopped:
        rows.append([InlineKeyboardButton("▶️ Включить ИИ", callback_data=f"sup_act:{client_id}:start_ai")])
    else:
        rows.append([InlineKeyboardButton("🛑 Остановить ИИ · перейти в чат", callback_data=f"sup_act:{client_id}:stop_ai")])
    rows.append([InlineKeyboardButton("✅ Завершить тикет", callback_data=f"close_ticket:{client_id}")])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user_id = update.effective_user.id

    # Клиент (не менеджер) — приветствие поддержки, кнопку Mini App не показываем
    if not check_access(user_id):
        try:
            await context.bot.set_chat_menu_button(
                chat_id=update.effective_chat.id,
                menu_button=MenuButtonCommands(),
            )
        except Exception as e:
            logger.debug("set_chat_menu_button commands: %s", e)
        await update.message.reply_text(
            f"👋 Здравствуйте! Это поддержка {SERVICE_NAME}.\n\n"
            "Напишите ваше сообщение — менеджер ответит здесь в боте."
        )
        return

    text = (
        f"👋 <b>Добро пожаловать в бот для менеджеров {SERVICE_NAME}!</b>\n\n"
        "📝 <b>Использование:</b>\n"
        "Отправьте Telegram ID или username пользователя, и я пришлю всю доступную информацию о нем и его подписке.\n\n"
        "💡 <b>Примеры:</b>\n"
        "• <code>123456789</code> (Telegram ID)\n"
        "• <code>@username</code> или <code>username</code> (username)\n\n"
        "Используйте /help для справки."
    )
    reply_markup = None
    if MINI_APP_URL:
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("📱 Открыть мини-приложение", web_app=WebAppInfo(url=MINI_APP_URL))
        ]])
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
    if MINI_APP_URL:
        try:
            await context.bot.set_chat_menu_button(
                chat_id=update.effective_chat.id,
                menu_button=MenuButtonWebApp(text="📱 Приложение", web_app=WebAppInfo(url=MINI_APP_URL)),
            )
        except Exception as e:
            logger.debug("set_chat_menu_button: %s", e)


async def _refresh_last_user_data(context: ContextTypes.DEFAULT_TYPE) -> Optional[Dict[str, Any]]:
    """Обновляет last_user_data из API и возвращает его."""
    last = context.user_data.get("last_user_data")
    if not last or not last.get("user", {}).get("uuid"):
        return None
    user_uuid = last["user"]["uuid"]
    user = get_user_by_telegram_id(str(last["user"].get("telegramId") or "")) or get_user_by_username(last["user"].get("username") or "")
    if not user:
        return last
    subscription = get_subscription_by_uuid(user_uuid)
    hwid_devices = get_hwid_devices(user_uuid) or []
    telegram_id_b = user.get("telegramId") or None
    bedolaga_user = get_bedolaga_user(str(telegram_id_b)) if telegram_id_b and _bedolaga_configured() else last.get("bedolaga_user")
    new_last = {"user": user, "subscription": subscription, "hwid_devices": hwid_devices, "bedolaga_user": bedolaga_user}
    context.user_data["last_user_data"] = new_last
    return new_last


async def action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопок действий: сброс трафика, перевыпуск подписки, удаление HWID."""
    query = update.callback_query
    user_id = update.effective_user.id
    if not check_access(user_id):
        await query.answer("Доступ запрещён.", show_alert=True)
        return
    data = query.data
    last = context.user_data.get("last_user_data")
    if not last or not last.get("user", {}).get("uuid"):
        await query.answer("Данные устарели. Выполните новый поиск.", show_alert=True)
        return
    user_uuid = last["user"]["uuid"]
    subscription = last.get("subscription")
    hwid_devices = last.get("hwid_devices") or []
    ok, action_msg = False, ""

    if data == "act:reset_traffic":
        await query.answer("Сброс трафика...")
        ok, action_msg = api_reset_user_traffic(user_uuid)
    elif data == "act:revoke_sub":
        await query.answer("Перевыпуск подписки...")
        ok, action_msg = api_revoke_user_subscription(user_uuid)
    elif data == "act:disable":
        await query.answer("Блокировка профиля...")
        ok, action_msg = api_disable_user(user_uuid)
    elif data == "act:enable":
        await query.answer("Разблокировка профиля...")
        ok, action_msg = api_enable_user(user_uuid)
    elif data == "act:invoice":
        await query.answer()
        user = last.get("user", {})
        client_id = user.get("telegramId")
        if isinstance(client_id, str) and client_id.isdigit():
            client_id = int(client_id)
        elif not isinstance(client_id, int):
            client_id = None
        manager_id = update.effective_user.id
        payload = {
            "client_id": client_id,
            "manager_id": manager_id,
            "user_uuid": user.get("uuid"),
        }
        context.user_data["awaiting_invoice"] = payload
        _set_awaiting_invoice_by_manager(context, manager_id, payload)
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="💰 <b>Выставить счёт</b>\n\nВведите сумму в рублях (число):",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("send invoice prompt to manager: %s", e)
        return
    elif data == "act:bedolaga_tx":
        await query.answer("Загрузка транзакций...")
        bedolaga_user = last.get("bedolaga_user")
        if not bedolaga_user:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="Нет данных Bedolaga для этого пользователя.",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("send bedolaga_tx error: %s", e)
            return
        bedolaga_id = bedolaga_user.get("id")
        if not bedolaga_id:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="Не удалось получить id пользователя в Bedolaga.",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("send bedolaga_tx error: %s", e)
            return
        transactions = get_bedolaga_transactions(int(bedolaga_id), limit=30)
        text = _format_bedolaga_transactions_message(transactions)
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("send bedolaga transactions: %s", e)
        return
    elif data == "act:squads":
        await query.answer()
        context.user_data["squads_target_uuid"] = user_uuid
        internal = get_internal_squads() or []
        external = get_external_squads() or []
        rows = []
        for s in internal:
            name = (s.get("name") or s.get("uuid", ""))[:32]
            rows.append([InlineKeyboardButton(f"📁 {name}", callback_data=f"squad:i:{s.get('uuid', '')}")])
        for s in external:
            name = (s.get("name") or s.get("uuid", ""))[:32]
            rows.append([InlineKeyboardButton(f"🌐 {name}", callback_data=f"squad:e:{s.get('uuid', '')}")])
        if not rows:
            try:
                await query.edit_message_text(
                    (query.message.text or "") + "\n\n👥 <b>Сквады</b>\n\nНет доступных сквадов.",
                    parse_mode="HTML",
                )
            except Exception:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="👥 Нет доступных сквадов.",
                )
            return
        rows.append([InlineKeyboardButton("◀️ Назад", callback_data="squad_back")])
        text = "👥 <b>Выберите сквад для выдачи клиенту:</b>"
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(rows),
            )
        except Exception as e:
            logger.warning("send squads list: %s", e)
        return
    elif data == "act:hwid_all":
        await query.answer("Удаление всех устройств...")
        ok, action_msg = api_delete_all_hwid(user_uuid)
    elif data.startswith("hwid_del:"):
        try:
            idx = int(data.split(":")[1])
        except (IndexError, ValueError):
            await query.answer("Ошибка", show_alert=True)
            return
        if idx < 0 or idx >= len(hwid_devices):
            await query.answer("Устройство не найдено.", show_alert=True)
            return
        hwid = hwid_devices[idx].get("hwid")
        if not hwid:
            await query.answer("Ошибка данных.", show_alert=True)
            return
        await query.answer("Удаление устройства...")
        ok, action_msg = api_delete_hwid_device(user_uuid, hwid)
    else:
        return

    last = await _refresh_last_user_data(context)
    if not last:
        last = context.user_data.get("last_user_data")
    user = last["user"]
    subscription = last.get("subscription")
    hwid_devices = last.get("hwid_devices") or []
    username_display = user.get("username", "N/A")
    header = f"✅ <b>Пользователь</b> @{username_display}\n\n"
    if action_msg:
        header = header + (f"✅ <i>{action_msg}</i>\n\n" if ok else f"❌ <i>{action_msg}</i>\n\n")
    if last.get("bedolaga_user"):
        header += f"💰 <b>Баланс (Bedolaga):</b> {_format_bedolaga_balance(last['bedolaga_user'])} ₽\n\n"
    section = (
        "hwid" if data.startswith("hwid_del") or data == "act:hwid_all"
        else ("traffic" if data == "act:reset_traffic"
              else ("profile" if data in ("act:disable", "act:enable") else "subscription"))
    )
    section_text = get_section_text(section, user, subscription, hwid_devices)
    full_text = header + section_text
    keyboard = build_section_keyboard(section, last)
    try:
        await query.edit_message_text(full_text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Ошибка при редактировании сообщения: {e}")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий кнопок секций."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    if not check_access(user_id):
        await query.edit_message_text("❌ Доступ запрещён.")
        return
    data = query.data
    if not data.startswith("s:"):
        return
    section = data[2:]
    if section not in SECTIONS:
        return
    last = context.user_data.get("last_user_data")
    if not last:
        await query.edit_message_text("⏳ Данные устарели. Выполните новый поиск по ID или username.")
        return
    user = last["user"]
    subscription = last.get("subscription")
    hwid_devices = last.get("hwid_devices")
    username_display = user.get("username", "N/A")
    header = f"✅ <b>Пользователь</b> @{username_display}\n\n"
    if last.get("bedolaga_user"):
        header += f"💰 <b>Баланс (Bedolaga):</b> {_format_bedolaga_balance(last['bedolaga_user'])} ₽\n\n"
    section_text = get_section_text(section, user, subscription, hwid_devices)
    full_text = header + section_text
    keyboard = build_section_keyboard(section, last)
    try:
        await query.edit_message_text(
            full_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"Ошибка при редактировании сообщения: {e}")


async def handle_client_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Клиент написал в поддержку: 1) карточка профиля, 2) вторым сообщением — текст/фото/файл."""
    client_id = update.effective_user.id
    # Заблокирован в ТП — не принимаем сообщения
    blocked = context.application.bot_data.get("support_blocked_clients")
    if isinstance(blocked, set) and client_id in blocked:
        await update.message.reply_text("Вы заблокированы в поддержке. Писать сюда больше нельзя.")
        return
    client_username = update.effective_user.username or ""
    client_name = (update.effective_user.first_name or "") + (" " + (update.effective_user.last_name or "")).strip()
    content = _get_message_content(update)
    if not content:
        await update.message.reply_text("Напишите текст или отправьте фото/файл.")
        return
    # У клиентов кнопка Mini App не показывается — ставим кнопку «Команды»
    try:
        await context.bot.set_chat_menu_button(
            chat_id=update.effective_chat.id,
            menu_button=MenuButtonCommands(),
        )
    except Exception as e:
        logger.debug("set_chat_menu_button commands: %s", e)

    api_user = get_user_by_telegram_id(str(client_id))
    user_uuid = api_user.get("uuid") if api_user else None
    subscription = get_subscription_by_uuid(user_uuid) if user_uuid else None
    hwid_devices = get_hwid_devices(user_uuid) if user_uuid else []
    bedolaga_user = get_bedolaga_user(str(client_id)) if _bedolaga_configured() else None

    # Текст сообщения клиента для ИИ (и для истории)
    if content[0] in ("text", "caption_only"):
        user_text_for_ai = (content[1] or "").strip()
    elif content[0] == "voice" and _AI_SUPPORT_AVAILABLE and transcribe_voice_groq:
        user_text_for_ai = (transcribe_voice_groq(context.bot, content[1]) or "").strip() or "[Голосовое сообщение]"
    elif content[0] == "voice":
        user_text_for_ai = "[Голосовое сообщение]"
    elif content[0] == "photo":
        user_text_for_ai = "Клиент отправил изображение." + (" " + (content[2] or "").strip() if len(content) > 2 and (content[2] or "").strip() else "")
    else:
        user_text_for_ai = "Клиент отправил вложение (файл/видео)." + (" " + (content[2] or "").strip() if len(content) > 2 and (content[2] or "").strip() else "")

    # Для фото при Groq — скачиваем и передаём в vision
    image_base64_arg = None
    image_mime_arg = None
    if content[0] == "photo" and _AI_SUPPORT_AVAILABLE and AI_PROVIDER == "groq" and is_ai_enabled():
        try:
            tg_file = await context.bot.get_file(content[1])
            fd, path = tempfile.mkstemp(suffix=".jpg")
            os.close(fd)
            await tg_file.download_to_drive(path)
            with open(path, "rb") as f:
                data = f.read()
            os.unlink(path)
            if len(data) <= 4 * 1024 * 1024:
                image_base64_arg = base64.b64encode(data).decode("utf-8")
                image_mime_arg = "image/jpeg"
        except Exception as e:
            logger.warning("Support: failed to download photo for vision: %s", e)

    ai_reply = None
    # Если клиент уже нажал «Позвать менеджера» — ИИ не отвечает, только менеджер
    client_wants_manager = (context.application.bot_data.get("support_client_wants_manager") or set())
    if client_id in client_wants_manager:
        pass  # ai_reply остаётся None
    elif _AI_SUPPORT_AVAILABLE and is_ai_enabled() and (user_text_for_ai or image_base64_arg) and build_client_context_for_ai and get_system_prompt and get_ai_reply:
        try:
            tariffs_configs = get_subscription_page_configs() or []
            tariff_names = [str(c.get("name") or "").strip() for c in tariffs_configs if c.get("name")]
            tariffs_text = f"Доступные тарифы в боте: {', '.join(tariff_names)}" if tariff_names else None
            client_context = build_client_context_for_ai(
                api_user, subscription, hwid_devices, bedolaga_user, SERVICE_NAME, tariffs_text=tariffs_text
            )
            history = get_conversation_history(context.application.bot_data, client_id)
            prompt_text = (content[2] or "").strip() if content[0] == "photo" and len(content) > 2 else user_text_for_ai
            ai_reply = get_ai_reply(
                get_system_prompt(SERVICE_NAME),
                client_context,
                history,
                prompt_text or user_text_for_ai,
                SERVICE_NAME,
                bot_data=context.application.bot_data,
                image_base64=image_base64_arg,
                image_mime=image_mime_arg,
            )
            add_to_conversation_history(context.application.bot_data, client_id, "user", user_text_for_ai)
            if ai_reply:
                add_to_conversation_history(context.application.bot_data, client_id, "assistant", ai_reply)
                if add_global_example:
                    add_global_example(context.application.bot_data, user_text_for_ai, ai_reply)
            elif user_text_for_ai or image_base64_arg:
                logger.warning("AI support: get_ai_reply returned None for client %s. Check GROQ_API_KEY and API limits.", client_id)
        except Exception as e:
            logger.exception("AI support error for client %s: %s", client_id, e)
    elif user_text_for_ai and (not _AI_SUPPORT_AVAILABLE or not is_ai_enabled()):
        if not _AI_SUPPORT_AVAILABLE:
            logger.debug("AI support: module not loaded (ai_support import failed)")
        elif not is_ai_enabled():
            logger.debug("AI support: GROQ_API_KEY not set or empty (add to .env and restart)")

    # Заголовок карточки без текста сообщения — текст/фото идут вторым сообщением
    support_header = (
        f"📩 <b>Сообщение от клиента</b>\n"
        f"Telegram ID: <code>{client_id}</code>\n"
        f"Имя: {client_name or '—'}\n"
        f"Username: @{client_username or '—'}\n\n"
    )
    if bedolaga_user:
        balance_str = _format_bedolaga_balance(bedolaga_user)
        support_header += f"💰 <b>Баланс (Bedolaga):</b> {balance_str} ₽\n\n"
    if api_user or subscription:
        support_header += f"📋 <b>Данные из API {SERVICE_NAME}:</b>\n"
        if api_user:
            support_header += f"🆔 UUID: <code>{api_user.get('uuid', 'N/A')}</code>\n"
            sub_user = (subscription or {}).get("user", {}) if subscription else {}
            status = sub_user.get("userStatus") or api_user.get("status", "N/A")
            uname = api_user.get("username") or "—"
            support_header += f"👤 @{uname} · {status}\n"
            support_header += f"📅 Истекает: {format_datetime(api_user.get('expireAt'))}\n"
            ut = api_user.get("userTraffic") or {}
            used = ut.get("usedTrafficBytes", 0)
            limit = api_user.get("trafficLimitBytes", 0)
            limit_str = format_bytes(limit) if limit > 0 else "∞"
            support_header += f"📊 Трафик: {format_bytes(used)} / {limit_str}\n"
        support_header += "\n"
    if "support_clients" not in context.application.bot_data:
        context.application.bot_data["support_clients"] = {}
    if "support_has_card" not in context.application.bot_data:
        context.application.bot_data["support_has_card"] = set()
    if "support_topic_by_client" not in context.application.bot_data:
        context.application.bot_data["support_topic_by_client"] = {}
    if "support_thread_to_client" not in context.application.bot_data:
        context.application.bot_data["support_thread_to_client"] = {}
    if "support_ticket_counter" not in context.application.bot_data:
        context.application.bot_data["support_ticket_counter"] = 0
    context.application.bot_data["support_clients"][client_id] = {
        "user": api_user or {},
        "subscription": subscription,
        "hwid_devices": hwid_devices,
        "support_header": support_header,
        "bedolaga_user": bedolaga_user,
    }

    last_data = {"user": api_user or {}, "subscription": subscription, "hwid_devices": hwid_devices, "bedolaga_user": bedolaga_user}
    sent_ok = False
    topic_by_client = context.application.bot_data["support_topic_by_client"]
    thread_to_client = context.application.bot_data["support_thread_to_client"]

    # Ответ ИИ клиенту (до создания/отправки в топик) + кнопки «Позвать менеджера» и «Закрыть тикет»
    if ai_reply:
        try:
            client_buttons = InlineKeyboardMarkup([[
                InlineKeyboardButton("👤 Позвать менеджера", callback_data="call_manager"),
                InlineKeyboardButton("✅ Закрыть тикет", callback_data="client_close_ticket"),
            ]])
            await update.message.reply_text(
                f"💬 <b>Ответ поддержки:</b>\n\n{ai_reply}",
                parse_mode="HTML",
                reply_markup=client_buttons,
            )
        except Exception as e:
            logger.warning("send AI reply to client: %s", e)

    if SUPPORT_GROUP_ID:
        # Логика с группой поддержки: каждый тикет — топик в группе
        existing = topic_by_client.get(client_id)
        if existing:
            # Уже есть топик — отправляем контент вторым сообщением
            sent_ok = False
            try:
                sent_ok = await _send_content_to_topic(
                    context,
                    existing["chat_id"],
                    existing["message_thread_id"],
                    content,
                    prefix="💬 <b>Новое сообщение от клиента</b>",
                )
                if sent_ok and ai_reply:
                    await context.bot.send_message(
                        chat_id=existing["chat_id"],
                        message_thread_id=existing["message_thread_id"],
                        text=f"🤖 <b>Ответ ИИ (отправлено клиенту):</b>\n\n{ai_reply}",
                        parse_mode="HTML",
                    )
            except Exception as e:
                logger.warning("send to support topic: %s", e)
            if not sent_ok:
                topic_by_client.pop(client_id, None)
                thread_to_client.pop((existing["chat_id"], existing["message_thread_id"]), None)
        if not sent_ok and not existing:
            # Создаём новый топик: "Тикет #N • Имя • ID{client_id}"
            context.application.bot_data["support_ticket_counter"] = (
                context.application.bot_data["support_ticket_counter"] + 1
            )
            ticket_num = context.application.bot_data["support_ticket_counter"]
            client_display = (client_name or "").strip() or (f"@{client_username}" if client_username else "—")
            topic_name = (f"⁉️ Тикет #{ticket_num} • {client_display} • ID{client_id}")[:128]
            chat_ids_to_try = _support_chat_ids()
            last_error = None
            for group_chat_id in chat_ids_to_try:
                try:
                    logger.info("Создание топика в группе поддержки chat_id=%s, название=%s", group_chat_id, topic_name)
                    forum_topic = await context.bot.create_forum_topic(
                        chat_id=group_chat_id,
                        name=topic_name,
                    )
                    thread_id = forum_topic.message_thread_id
                    username_display = (api_user or {}).get("username") or "—"
                    data_header = f"✅ <b>Пользователь</b> @{username_display}\n\n"
                    section_text = get_section_text("profile", api_user or {}, subscription, hwid_devices)
                    full_text = support_header + data_header + section_text
                    ai_stopped = client_id in (context.application.bot_data.get("support_client_wants_manager") or set())
                    support_blocked = client_id in (context.application.bot_data.get("support_blocked_clients") or set())
                    keyboard = build_support_keyboard(client_id, "profile", last_data, ai_stopped=ai_stopped, support_blocked=support_blocked)
                    await context.bot.send_message(
                        chat_id=group_chat_id,
                        message_thread_id=thread_id,
                        text=full_text,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                    # Вторым сообщением — текст/фото/файл клиента
                    await _send_content_to_topic(
                        context,
                        group_chat_id,
                        thread_id,
                        content,
                        prefix="💬 <b>Сообщение клиента</b>",
                    )
                    if ai_reply:
                        try:
                            await context.bot.send_message(
                                chat_id=group_chat_id,
                                message_thread_id=thread_id,
                                text=f"🤖 <b>Ответ ИИ (отправлено клиенту):</b>\n\n{ai_reply}",
                                parse_mode="HTML",
                            )
                        except Exception as e:
                            logger.warning("send AI reply to topic: %s", e)
                    topic_by_client[client_id] = {
                        "chat_id": group_chat_id,
                        "message_thread_id": thread_id,
                        "topic_name": topic_name,
                    }
                    thread_to_client[(group_chat_id, thread_id)] = client_id
                    context.application.bot_data["support_has_card"].add(client_id)
                    sent_ok = True
                    logger.info("Топик создан: chat_id=%s, thread_id=%s", group_chat_id, thread_id)
                    break
                except Exception as e:
                    last_error = e
                    logger.warning("create_forum_topic chat_id=%s: %s", group_chat_id, e)
            if not sent_ok and last_error:
                logger.exception("Не удалось создать топик поддержки: %s", last_error)
                err_str = str(last_error).lower()
                if "manage" in err_str or "topic" in err_str or "right" in err_str or "admin" in err_str:
                    logger.warning(
                        "Проверьте: бот — админ группы с правом «Управление топиками»; в группе включены топики (режим форума)."
                    )
                if "chat not found" in err_str or "400" in err_str:
                    logger.warning(
                        "Проверьте SUPPORT_GROUP_ID: для супергруппы используйте полный id вида -100xxxxxxxxxx (можно взять из логов при сообщении в группу)."
                    )
    if not sent_ok:
        # Нет SUPPORT_GROUP_ID или ошибка — fallback: отправка каждому менеджеру в личку
        has_card = client_id in context.application.bot_data["support_has_card"]
        content_line = (
            f"💬 <b>Текст:</b>\n{content[1]}\n\n"
            if content[0] == "text"
            else "💬 <b>Вложение:</b> фото/файл\n\n"
        )
        support_header_fallback = support_header + content_line
        if not has_card:
            username_display = (api_user or {}).get("username") or "—"
            data_header = f"✅ <b>Пользователь</b> @{username_display}\n\n"
            section_text = get_section_text("profile", api_user or {}, subscription, hwid_devices)
            full_text = support_header_fallback + data_header + section_text
            ai_stopped = client_id in (context.application.bot_data.get("support_client_wants_manager") or set())
            support_blocked = client_id in (context.application.bot_data.get("support_blocked_clients") or set())
            keyboard = build_support_keyboard(client_id, "profile", last_data, ai_stopped=ai_stopped, support_blocked=support_blocked)
            context.application.bot_data["support_has_card"].add(client_id)
            for manager_id in ALLOWED_MANAGER_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=manager_id,
                        text=full_text,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                    sent_ok = True
                except Exception as e:
                    logger.warning("send to manager %s: %s", manager_id, e)
        else:
            preview = (content[1][:200] + "…") if content[0] == "text" and len(content[1]) > 200 else (content[1] if content[0] == "text" else "фото/файл")
            short_text = (
                f"📩 <b>Новое сообщение от клиента</b>\n"
                f"ID: <code>{client_id}</code> · @{client_username or '—'}\n\n"
                f"💬 <b>Текст:</b>\n{preview}"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Завершить тикет", callback_data=f"close_ticket:{client_id}"),
            ]])
            for manager_id in ALLOWED_MANAGER_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=manager_id,
                        text=short_text,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                    sent_ok = True
                except Exception as e:
                    logger.warning("send to manager %s: %s", manager_id, e)
    if not sent_ok:
        await update.message.reply_text("Не удалось отправить обращение. Попробуйте позже.")
        return
    # Кнопка «Закрыть тикет» уже под ответом ИИ — не дублируем вторым сообщением
    if not ai_reply:
        close_ticket_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Закрыть тикет (вопрос решён)", callback_data="client_close_ticket"),
        ]])
        await update.message.reply_text(
            "✅ Ваше сообщение передано в поддержку. Ответ придет здесь в боте.",
            reply_markup=close_ticket_kb,
        )
    # Если был ответ ИИ — кнопки уже под ним, отдельное сообщение не отправляем


async def call_manager_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Клиент нажал «Позвать менеджера». Дальше отвечает только менеджер, ИИ не пишет."""
    query = update.callback_query
    if not query:
        return
    await query.answer("Запрос отправлен менеджеру.")
    client_id = query.from_user.id if query.from_user else 0
    if not client_id:
        return
    if "support_client_wants_manager" not in context.application.bot_data:
        context.application.bot_data["support_client_wants_manager"] = set()
    context.application.bot_data["support_client_wants_manager"].add(client_id)
    try:
        await context.bot.send_message(
            chat_id=client_id,
            text="✅ Менеджер уведомлён, ожидайте ответа.",
        )
    except Exception as e:
        logger.warning("call_manager reply to client: %s", e)
    topic_by_client = context.application.bot_data.get("support_topic_by_client") or {}
    existing = topic_by_client.get(client_id)
    if existing:
        try:
            await context.bot.send_message(
                chat_id=existing["chat_id"],
                message_thread_id=existing["message_thread_id"],
                text="👤 <b>Клиент хочет связаться с менеджером.</b>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("call_manager to topic: %s", e)
    else:
        for manager_id in ALLOWED_MANAGER_IDS:
            try:
                await context.bot.send_message(
                    chat_id=manager_id,
                    text=f"👤 Клиент (ID <code>{client_id}</code>) хочет связаться с менеджером. Напишите ему в боте или попросите написать сюда — тикет создастся.",
                    parse_mode="HTML",
                )
                break
            except Exception as e:
                logger.warning("call_manager to manager %s: %s", manager_id, e)


async def client_close_ticket_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Клиент нажал «Закрыть тикет» — в топик пишем что вопрос решён, закрываем топик, уведомляем клиента."""
    query = update.callback_query
    if not query:
        return
    client_id = query.from_user.id if query.from_user else 0
    if not client_id:
        try:
            await query.answer("Ошибка.", show_alert=True)
        except Exception:
            pass
        return
    try:
        await query.answer("Тикет закрыт.")
    except Exception as e:
        logger.debug("client_close_ticket answer_callback_query: %s", e)
    topic_by_client = context.application.bot_data.get("support_topic_by_client") or {}
    thread_to_client = context.application.bot_data.get("support_thread_to_client") or {}
    existing = topic_by_client.get(client_id)
    if existing:
        try:
            await context.bot.send_message(
                chat_id=existing["chat_id"],
                message_thread_id=existing["message_thread_id"],
                text="✅ <b>Клиент закрыл тикет (вопрос решён).</b>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("client_close_ticket message to topic: %s", e)
        try:
            topic_name = existing.get("topic_name") or ""
            if topic_name:
                closed_name = (topic_name.replace("⁉️ ", "🔥 ", 1))[:128]
                await context.bot.edit_forum_topic(
                    chat_id=existing["chat_id"],
                    message_thread_id=existing["message_thread_id"],
                    name=closed_name,
                )
            await context.bot.close_forum_topic(
                chat_id=existing["chat_id"],
                message_thread_id=existing["message_thread_id"],
            )
        except Exception as e:
            logger.warning("client_close_ticket close_forum_topic: %s", e)
        topic_by_client.pop(client_id, None)
        thread_to_client.pop((existing["chat_id"], existing["message_thread_id"]), None)
    support_has_card = context.application.bot_data.get("support_has_card")
    if isinstance(support_has_card, set):
        support_has_card.discard(client_id)
    support_client_wants_manager = context.application.bot_data.get("support_client_wants_manager")
    if isinstance(support_client_wants_manager, set):
        support_client_wants_manager.discard(client_id)
    try:
        await context.bot.send_message(
            chat_id=client_id,
            text="✅ Тикет закрыт. Если понадобится помощь — напишите снова.",
        )
    except Exception as e:
        logger.warning("client_close_ticket notify client: %s", e)


async def support_card_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключение секций в карточке поддержки (sup:client_id:section) или действие (sup_act, sup_hwid)."""
    query = update.callback_query
    if not check_access(update.effective_user.id):
        await query.answer("Доступ запрещён.", show_alert=True)
        return
    data = query.data
    support_clients = context.application.bot_data.get("support_clients") or {}

    if data.startswith("sup_act:"):
        # sup_act:client_id:action (reset_traffic | revoke_sub | stop_ai | start_ai | ...)
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer()
            return
        try:
            client_id = int(parts[1])
        except ValueError:
            await query.answer()
            return
        action = parts[2]

        # Остановить ИИ / Включить ИИ — перейти в чат самому (не требует uuid)
        if action == "stop_ai":
            if "support_client_wants_manager" not in context.application.bot_data:
                context.application.bot_data["support_client_wants_manager"] = set()
            context.application.bot_data["support_client_wants_manager"].add(client_id)
            await query.answer("ИИ остановлен. Отвечайте в чате сами.")
            sc = support_clients.get(client_id)
            if sc:
                last_data = {"user": sc.get("user"), "subscription": sc.get("subscription"), "hwid_devices": sc.get("hwid_devices", []), "bedolaga_user": sc.get("bedolaga_user")}
                keyboard = build_support_keyboard(client_id, "profile", last_data, ai_stopped=True, support_blocked=client_id in (context.application.bot_data.get("support_blocked_clients") or set()))
                try:
                    await query.edit_message_reply_markup(reply_markup=keyboard)
                except Exception:
                    pass
            return
        if action == "start_ai":
            want_mgr = context.application.bot_data.get("support_client_wants_manager")
            if isinstance(want_mgr, set):
                want_mgr.discard(client_id)
            await query.answer("ИИ включён снова.")
            sc = support_clients.get(client_id)
            if sc:
                last_data = {"user": sc.get("user"), "subscription": sc.get("subscription"), "hwid_devices": sc.get("hwid_devices", []), "bedolaga_user": sc.get("bedolaga_user")}
                keyboard = build_support_keyboard(client_id, "profile", last_data, ai_stopped=False, support_blocked=client_id in (context.application.bot_data.get("support_blocked_clients") or set()))
                try:
                    await query.edit_message_reply_markup(reply_markup=keyboard)
                except Exception:
                    pass
            return
        if action == "block_support":
            if "support_blocked_clients" not in context.application.bot_data:
                context.application.bot_data["support_blocked_clients"] = set()
            context.application.bot_data["support_blocked_clients"].add(client_id)
            await query.answer("Клиент заблокирован в поддержке — писать сюда больше не сможет.")
            sc = support_clients.get(client_id)
            if sc:
                last_data = {"user": sc.get("user"), "subscription": sc.get("subscription"), "hwid_devices": sc.get("hwid_devices", []), "bedolaga_user": sc.get("bedolaga_user")}
                keyboard = build_support_keyboard(client_id, "profile", last_data, ai_stopped=client_id in (context.application.bot_data.get("support_client_wants_manager") or set()), support_blocked=True)
                try:
                    await query.edit_message_reply_markup(reply_markup=keyboard)
                except Exception:
                    pass
            return
        if action == "unblock_support":
            blocked = context.application.bot_data.get("support_blocked_clients")
            if isinstance(blocked, set):
                blocked.discard(client_id)
            await query.answer("Клиент разблокирован — снова может писать в поддержку.")
            sc = support_clients.get(client_id)
            if sc:
                last_data = {"user": sc.get("user"), "subscription": sc.get("subscription"), "hwid_devices": sc.get("hwid_devices", []), "bedolaga_user": sc.get("bedolaga_user")}
                keyboard = build_support_keyboard(client_id, "profile", last_data, ai_stopped=client_id in (context.application.bot_data.get("support_client_wants_manager") or set()), support_blocked=False)
                try:
                    await query.edit_message_reply_markup(reply_markup=keyboard)
                except Exception:
                    pass
            return

        sc = support_clients.get(client_id)
        if not sc or not (sc.get("user") or {}).get("uuid"):
            await query.answer(
                "Пользователь не найден в системе Remnawave. Добавьте его в панель или обновите карточку.",
                show_alert=True,
            )
            return
        user_uuid = sc["user"]["uuid"]
        if action == "bedolaga_tx":
            await query.answer("Загрузка транзакций...")
            bedolaga_user = sc.get("bedolaga_user")
            if not bedolaga_user or not bedolaga_user.get("id"):
                try:
                    thread_id = getattr(query.message, "message_thread_id", None)
                    kwargs = {"chat_id": update.effective_chat.id, "text": "Нет данных Bedolaga для этого клиента.", "parse_mode": "HTML"}
                    if thread_id:
                        kwargs["message_thread_id"] = thread_id
                    await context.bot.send_message(**kwargs)
                except Exception as e:
                    logger.warning("send bedolaga_tx error: %s", e)
                return
            transactions = get_bedolaga_transactions(int(bedolaga_user["id"]), limit=30)
            text = _format_bedolaga_transactions_message(transactions)
            try:
                thread_id = getattr(query.message, "message_thread_id", None)
                kwargs = {"chat_id": update.effective_chat.id, "text": text, "parse_mode": "HTML"}
                if thread_id:
                    kwargs["message_thread_id"] = thread_id
                await context.bot.send_message(**kwargs)
            except Exception as e:
                logger.warning("send bedolaga transactions: %s", e)
            return
        if action == "invoice":
            await query.answer()
            manager_id = update.effective_user.id
            payload = {
                "client_id": client_id,
                "manager_id": manager_id,
                "user_uuid": user_uuid,
            }
            context.user_data["awaiting_invoice"] = payload
            _set_awaiting_invoice_by_manager(context, manager_id, payload)
            try:
                chat_id = update.effective_chat.id
                thread_id = getattr(query.message, "message_thread_id", None)
                kwargs = {"chat_id": chat_id, "text": "💰 <b>Выставить счёт</b>\n\nВведите сумму в рублях (число):", "parse_mode": "HTML"}
                if thread_id:
                    kwargs["message_thread_id"] = thread_id
                await context.bot.send_message(**kwargs)
            except Exception as e:
                logger.warning("send invoice prompt to manager: %s", e)
            return
        if action == "squads":
            await query.answer()
            context.user_data["squads_target_uuid"] = user_uuid
            internal = get_internal_squads() or []
            external = get_external_squads() or []
            rows = []
            for s in internal:
                name = (s.get("name") or s.get("uuid", ""))[:32]
                rows.append([InlineKeyboardButton(f"📁 {name}", callback_data=f"squad:i:{s.get('uuid', '')}")])
            for s in external:
                name = (s.get("name") or s.get("uuid", ""))[:32]
                rows.append([InlineKeyboardButton(f"🌐 {name}", callback_data=f"squad:e:{s.get('uuid', '')}")])
            if not rows:
                try:
                    await query.edit_message_text(
                        sc["support_header"] + "\n👥 <b>Сквады</b>\n\nНет доступных сквадов.",
                        parse_mode="HTML",
                    )
                except Exception:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="👥 Нет доступных сквадов.",
                        reply_to_message_id=query.message.message_id if query.message else None,
                    )
                return
            rows.append([InlineKeyboardButton("◀️ Назад", callback_data="squad_back")])
            text = sc["support_header"] + "\n👥 <b>Выберите сквад для выдачи клиенту:</b>"
            try:
                thread_id = getattr(query.message, "message_thread_id", None)
                kwargs = {"chat_id": update.effective_chat.id, "text": text, "parse_mode": "HTML", "reply_markup": InlineKeyboardMarkup(rows)}
                if thread_id:
                    kwargs["message_thread_id"] = thread_id
                await context.bot.send_message(**kwargs)
            except Exception as e:
                logger.warning("send squads list: %s", e)
            return
        ok, msg = False, ""
        if action == "reset_traffic":
            await query.answer("Сброс трафика...")
            ok, msg = api_reset_user_traffic(user_uuid)
        elif action == "revoke_sub":
            await query.answer("Перевыпуск подписки...")
            ok, msg = api_revoke_user_subscription(user_uuid)
        elif action == "disable":
            await query.answer("Блокировка профиля...")
            ok, msg = api_disable_user(user_uuid)
        elif action == "enable":
            await query.answer("Разблокировка профиля...")
            ok, msg = api_enable_user(user_uuid)
        elif action == "hwid_all":
            await query.answer("Удаление всех устройств...")
            ok, msg = api_delete_all_hwid(user_uuid)
        else:
            await query.answer()
            return
        api_user = get_user_by_telegram_id(str(client_id))
        subscription = get_subscription_by_uuid(user_uuid) if user_uuid else None
        hwid_devices = get_hwid_devices(user_uuid) if user_uuid else []
        support_clients[client_id] = {
            "user": api_user or sc["user"],
            "subscription": subscription,
            "hwid_devices": hwid_devices,
            "support_header": sc.get("support_header", ""),
            "bedolaga_user": sc.get("bedolaga_user"),
        }
        last_data = {"user": support_clients[client_id]["user"], "subscription": subscription, "hwid_devices": hwid_devices, "bedolaga_user": sc.get("bedolaga_user")}
        section = (
            "traffic" if action == "reset_traffic"
            else ("subscription" if action == "revoke_sub"
                  else ("profile" if action in ("disable", "enable") else "hwid"))
        )
        header_extra = (f"✅ <i>{msg}</i>\n\n" if ok else f"❌ <i>{msg}</i>\n\n")
        username_display = last_data["user"].get("username") or "—"
        data_header = f"✅ <b>Пользователь</b> @{username_display}\n\n{header_extra}"
        section_text = get_section_text(section, last_data["user"], subscription, hwid_devices)
        full_text = sc["support_header"] + data_header + section_text
        ai_stopped = client_id in (context.application.bot_data.get("support_client_wants_manager") or set())
        support_blocked = client_id in (context.application.bot_data.get("support_blocked_clients") or set())
        keyboard = build_support_keyboard(client_id, section, last_data, ai_stopped=ai_stopped, support_blocked=support_blocked)
        try:
            await query.edit_message_text(full_text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as e:
            logger.error("edit support message: %s", e)
        return

    if data.startswith("sup_hwid:"):
        # sup_hwid:client_id:index
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer()
            return
        try:
            client_id = int(parts[1])
            idx = int(parts[2])
        except ValueError:
            await query.answer()
            return
        sc = support_clients.get(client_id)
        if not sc:
            await query.answer("Данные устарели.", show_alert=True)
            return
        hwid_devices = sc.get("hwid_devices") or []
        if idx < 0 or idx >= len(hwid_devices):
            await query.answer("Устройство не найдено.", show_alert=True)
            return
        hwid = hwid_devices[idx].get("hwid")
        user_uuid = (sc.get("user") or {}).get("uuid")
        if not hwid or not user_uuid:
            await query.answer("Ошибка данных.", show_alert=True)
            return
        await query.answer("Удаление устройства...")
        ok, msg = api_delete_hwid_device(user_uuid, hwid)
        api_user = get_user_by_telegram_id(str(client_id))
        subscription = get_subscription_by_uuid(user_uuid) if user_uuid else None
        hwid_devices = get_hwid_devices(user_uuid) if user_uuid else []
        support_clients[client_id] = {
            "user": api_user or sc["user"],
            "subscription": subscription,
            "hwid_devices": hwid_devices,
            "support_header": sc.get("support_header", ""),
            "bedolaga_user": sc.get("bedolaga_user"),
        }
        last_data = {"user": support_clients[client_id]["user"], "subscription": subscription, "hwid_devices": hwid_devices, "bedolaga_user": sc.get("bedolaga_user")}
        header_extra = (f"✅ <i>{msg}</i>\n\n" if ok else f"❌ <i>{msg}</i>\n\n")
        username_display = last_data["user"].get("username") or "—"
        data_header = f"✅ <b>Пользователь</b> @{username_display}\n\n{header_extra}"
        section_text = get_section_text("hwid", last_data["user"], subscription, hwid_devices)
        full_text = sc["support_header"] + data_header + section_text
        ai_stopped = client_id in (context.application.bot_data.get("support_client_wants_manager") or set())
        support_blocked = client_id in (context.application.bot_data.get("support_blocked_clients") or set())
        keyboard = build_support_keyboard(client_id, "hwid", last_data, ai_stopped=ai_stopped, support_blocked=support_blocked)
        try:
            await query.edit_message_text(full_text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as e:
            logger.error("edit support message: %s", e)
        return

    # sup:client_id:section — переключение секции
    if not data.startswith("sup:"):
        return
    parts = data.split(":", 2)
    if len(parts) != 3:
        await query.answer()
        return
    try:
        client_id = int(parts[1])
        section = parts[2]
    except ValueError:
        await query.answer()
        return
    if section not in SECTIONS:
        await query.answer()
        return
    sc = support_clients.get(client_id)
    if not sc:
        await query.answer("Данные устарели.", show_alert=True)
        return
    await query.answer()
    user = sc.get("user") or {}
    subscription = sc.get("subscription")
    hwid_devices = sc.get("hwid_devices") or []
    last_data = {"user": user, "subscription": subscription, "hwid_devices": hwid_devices, "bedolaga_user": sc.get("bedolaga_user")}
    username_display = user.get("username") or "—"
    data_header = f"✅ <b>Пользователь</b> @{username_display}\n\n"
    section_text = get_section_text(section, user, subscription, hwid_devices)
    full_text = sc.get("support_header", "") + data_header + section_text
    ai_stopped = client_id in (context.application.bot_data.get("support_client_wants_manager") or set())
    support_blocked = client_id in (context.application.bot_data.get("support_blocked_clients") or set())
    keyboard = build_support_keyboard(client_id, section, last_data, ai_stopped=ai_stopped, support_blocked=support_blocked)
    try:
        await query.edit_message_text(full_text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.error("edit support message: %s", e)


async def squad_assign_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выдача сквада клиенту: squad:i:uuid, squad:e:uuid или squad_back (Назад/Закрыть)."""
    query = update.callback_query
    if not check_access(update.effective_user.id):
        await query.answer("Доступ запрещён.", show_alert=True)
        return
    if query.data == "squad_back":
        await query.answer()
        try:
            await query.message.delete()
        except Exception as e:
            logger.debug("delete squads message: %s", e)
        return
    user_uuid = context.user_data.get("squads_target_uuid")
    if not user_uuid:
        await query.answer("Сессия истекла. Выберите пользователя заново и нажмите «Сквады».", show_alert=True)
        return
    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.answer()
        return
    squad_type, squad_uuid = parts[1], parts[2]
    if not squad_uuid:
        await query.answer("Ошибка данных.", show_alert=True)
        return
    if squad_type == "i":
        await query.answer("Добавление во внутренний сквад...")
        ok, msg = add_user_to_internal_squad(squad_uuid, user_uuid)
    elif squad_type == "e":
        await query.answer("Выдача сквада клиенту...")
        ok, msg = add_user_to_external_squad(squad_uuid, user_uuid)
    else:
        await query.answer()
        return
    context.user_data.pop("squads_target_uuid", None)
    close_btn = InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ Закрыть", callback_data="squad_back"),
    ]])
    try:
        await query.edit_message_text(
            (query.message.text or "") + f"\n\n{'✅' if ok else '❌'} {msg}",
            parse_mode="HTML",
            reply_markup=close_btn,
        )
    except Exception:
        try:
            thread_id = getattr(query.message, "message_thread_id", None) if query.message else None
            kwargs = {
                "chat_id": update.effective_chat.id,
                "text": f"{'✅' if ok else '❌'} {msg}",
                "reply_markup": close_btn,
            }
            if thread_id:
                kwargs["message_thread_id"] = thread_id
            elif query.message:
                kwargs["reply_to_message_id"] = query.message.message_id
            await context.bot.send_message(**kwargs)
        except Exception as e:
            logger.warning("send squad result: %s", e)


async def close_ticket_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Закрытие тикета: закрываем топик в группе, уведомляем клиента, очищаем маппинг."""
    query = update.callback_query
    if not check_access(update.effective_user.id):
        await query.answer("Доступ запрещён.", show_alert=True)
        return
    if not query.data.startswith("close_ticket:"):
        return
    try:
        client_id = int(query.data.split(":")[1])
    except (IndexError, ValueError):
        await query.answer()
        return
    # Сразу отвечаем Telegram (иначе «Query is too old» при долгих операциях)
    try:
        await query.answer("Тикет закрыт. Клиент уведомлён.")
    except Exception as e:
        logger.debug("close_ticket answer_callback_query: %s", e)
    topic_by_client = context.application.bot_data.get("support_topic_by_client") or {}
    thread_to_client = context.application.bot_data.get("support_thread_to_client") or {}
    existing = topic_by_client.get(client_id)
    if existing:
        try:
            topic_name = existing.get("topic_name") or ""
            if topic_name:
                closed_name = (topic_name.replace("⁉️ ", "🔥 ", 1))[:128]
                await context.bot.edit_forum_topic(
                    chat_id=existing["chat_id"],
                    message_thread_id=existing["message_thread_id"],
                    name=closed_name,
                )
            await context.bot.close_forum_topic(
                chat_id=existing["chat_id"],
                message_thread_id=existing["message_thread_id"],
            )
        except Exception as e:
            logger.warning("close_forum_topic / edit_forum_topic: %s", e)
        topic_by_client.pop(client_id, None)
        thread_to_client.pop((existing["chat_id"], existing["message_thread_id"]), None)
    support_has_card = context.application.bot_data.get("support_has_card")
    if isinstance(support_has_card, set):
        support_has_card.discard(client_id)
    support_client_wants_manager = context.application.bot_data.get("support_client_wants_manager")
    if isinstance(support_client_wants_manager, set):
        support_client_wants_manager.discard(client_id)
    try:
        await context.bot.send_message(
            chat_id=client_id,
            text="✅ Тикет поддержки закрыт.\n\nВы можете написать новое сообщение в любое время.",
        )
    except Exception as e:
        logger.debug("notify client ticket closed: %s", e)
    try:
        await query.edit_message_text("✅ Тикет закрыт. Клиент уведомлён.")
    except Exception:
        pass


async def handle_support_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сообщения в группе поддержки в топике: ответ менеджера (текст/фото/файл) пересылаем клиенту."""
    if not update.message:
        return
    allowed_chat_ids = set(_support_chat_ids())
    if not allowed_chat_ids or update.effective_chat.id not in allowed_chat_ids:
        return
    # Если менеджер ввёл сумму для «Выставить счёт» — обрабатываем как выставление счёта, не пересылаем клиенту
    manager_id = update.effective_user.id
    if check_access(manager_id) and _get_awaiting_invoice(context, manager_id):
        if await _handle_invoice_amount(update, context):
            return
    content = _get_message_content(update)
    if not content:
        return
    thread_id = getattr(update.message, "message_thread_id", None)
    if not thread_id:
        return
    thread_to_client = context.application.bot_data.get("support_thread_to_client") or {}
    client_id = thread_to_client.get((update.effective_chat.id, thread_id))
    if not client_id:
        return
    if not check_access(update.effective_user.id):
        return
    if getattr(update.effective_user, "is_bot", False):
        return
    manager_name = update.effective_user.first_name or "Поддержка"
    await _forward_content_to_client(context, client_id, content, manager_name)
    # Сохраняем ответ менеджера в историю и в глобальный пул примеров (LLM обучается на всех чатах)
    if _AI_SUPPORT_AVAILABLE and add_to_conversation_history:
        manager_text = None
        if content[0] in ("text", "caption_only") and (content[1] or "").strip():
            manager_text = (content[1] or "").strip()
            add_to_conversation_history(context.application.bot_data, client_id, "assistant", manager_text)
        elif content[0] == "photo" and (len(content) > 2 and (content[2] or "").strip()):
            manager_text = "Менеджер отправил изображение. " + (content[2] or "").strip()
            add_to_conversation_history(context.application.bot_data, client_id, "assistant", manager_text)
        elif content[0] not in ("text", "caption_only", "photo"):
            add_to_conversation_history(context.application.bot_data, client_id, "assistant", "[Менеджер отправил вложение]")
        if manager_text and add_global_example and get_last_user_message:
            last_user = get_last_user_message(context.application.bot_data, client_id)
            if last_user:
                add_global_example(context.application.bot_data, last_user, manager_text)


async def _handle_invoice_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Обработка ввода суммы для выставления счёта. Возвращает True, если сообщение обработано."""
    manager_id = update.effective_user.id
    awaiting = _get_awaiting_invoice(context, manager_id)
    if not awaiting:
        return False
    text = (update.message.text or "").strip()
    try:
        amount = float(text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Введите число (сумму в рублях).")
        return True
    if amount <= 0:
        await update.message.reply_text("Сумма должна быть больше 0.")
        return True
    _clear_awaiting_invoice(context, manager_id)
    client_id = awaiting.get("client_id")
    manager_id = awaiting.get("manager_id")
    user_uuid = awaiting.get("user_uuid")
    if not _PAYMENTS_AVAILABLE or not get_freekassa_provider:
        await update.message.reply_text("Платёжная система не настроена (Freekassa).")
        return True
    provider = get_freekassa_provider()
    if not provider:
        await update.message.reply_text("Платёжная система не настроена (Freekassa API key / shop ID).")
        return True
    payment_id = f"rmb_{int(time.time() * 1000)}_{client_id or manager_id}"
    email = f"{client_id}@telegram.org" if client_id else f"{manager_id}@telegram.org"
    base_url = (os.getenv("PAYMENTS_BASE_URL") or os.getenv("MINI_APP_DOMAIN") or "").strip()
    if base_url:
        if base_url.startswith("https:/") and not base_url.startswith("https://"):
            base_url = "https://" + base_url[7:]
        elif base_url.startswith("http:/") and not base_url.startswith("http://"):
            base_url = "http://" + base_url[6:]
        elif "://" not in base_url:
            base_url = "https://" + base_url
    notification_url = f"{base_url.rstrip('/')}/webhook/freekassa" if base_url else None
    result = provider.create_invoice(
        amount=amount,
        currency="RUB",
        payment_id=payment_id,
        email=email,
        ip="127.0.0.1",
        client_id=client_id or 0,
        manager_id=manager_id,
        user_uuid=user_uuid,
        notification_url=notification_url,
    )
    if not result.success:
        await update.message.reply_text(f"Ошибка создания счёта: {result.error or 'неизвестно'}.")
        return True
    if pending_add:
        pending_add(
            payment_id=payment_id,
            manager_id=manager_id,
            client_id=client_id if client_id is not None else 0,
            amount=amount,
            currency="RUB",
            user_uuid=user_uuid,
            provider="freekassa",
        )
    if client_id and result.payment_url:
        try:
            await context.bot.send_message(
                chat_id=client_id,
                text=f"💰 <b>Счёт на оплату</b>\n\nСумма: <b>{amount:.2f} ₽</b>\n\nОплатить: {result.payment_url}",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("send invoice link to client: %s", e)
    if result.payment_url:
        await update.message.reply_text(
            f"✅ Счёт на <b>{amount:.2f} ₽</b> выставлен.\n\n"
            + ("Ссылка отправлена клиенту." if client_id else f"Ссылка для клиента: {result.payment_url}"),
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("Счёт создан, но ссылка не получена.")
    return True


async def dispatch_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Маршрутизация: менеджер — поиск; клиент — в поддержку (текст/фото/файл). Ответы в топиках — handle_support_group_message."""
    if not update.message:
        return
    user_id = update.effective_user.id

    if check_access(user_id):
        if _get_awaiting_invoice(context, user_id):
            if await _handle_invoice_amount(update, context):
                return
        if update.message.text:
            await handle_message(update, context)
        return

    await handle_client_message(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    user_id = update.effective_user.id

    if not check_access(user_id):
        try:
            await context.bot.set_chat_menu_button(
                chat_id=update.effective_chat.id,
                menu_button=MenuButtonCommands(),
            )
        except Exception as e:
            logger.debug("set_chat_menu_button commands: %s", e)
        await update.message.reply_text("Напишите ваше сообщение — менеджер ответит здесь.")
        return
    
    text = (
        "📖 <b>Справка</b>\n\n"
        "🔍 <b>Поиск пользователя:</b>\n"
        "Отправьте Telegram ID (число) или username (с @ или без) пользователя.\n\n"
        "📋 <b>Что вы получите:</b>\n"
        "• Полная информация о пользователе\n"
        "• Статистика трафика\n"
        "• Информация о подписке\n"
        "• Привязанные устройства (HWID)\n"
        "• Даты создания, обновления, истечения\n\n"
        "💡 <b>Команды:</b>\n"
        "/start - Начать работу\n"
        "/help - Показать эту справку"
    )
    reply_markup = None
    if MINI_APP_URL:
        text += "\n\n📱 Или откройте <b>мини-приложение</b> — там тот же поиск в удобном интерфейсе."
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("📱 Открыть мини-приложение", web_app=WebAppInfo(url=MINI_APP_URL))
        ]])
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    user_id = update.effective_user.id
    
    if not check_access(user_id):
        await update.message.reply_text(
            "❌ У вас нет доступа к этому боту.\n"
            "Обратитесь к администратору для получения доступа."
        )
        return
    
    query = update.message.text.strip()
    
    if not query:
        await update.message.reply_text("❌ Пожалуйста, отправьте Telegram ID или username.")
        return
    
    # Отправляем сообщение о начале обработки
    processing_msg = await update.message.reply_text("⏳ Обрабатываю запрос...")
    
    user = None
    subscription = None
    
    # Определяем, что это: Telegram ID или username
    if query.isdigit():
        # Это Telegram ID
        logger.info(f"Поиск пользователя по Telegram ID: {query}")
        user = get_user_by_telegram_id(query)
    else:
        # Это username (убираем @ если есть)
        username = query.lstrip('@')
        logger.info(f"Поиск пользователя по username: {username}")
        user = get_user_by_username(username)
    
    if not user:
        await processing_msg.edit_text(
            f"❌ Пользователь не найден.\n\n"
            f"Проверьте правильность введенных данных:\n"
            f"• Telegram ID должен быть числом\n"
            f"• Username должен быть без пробелов"
        )
        return
    
    user_uuid = user.get('uuid')
    subscription = None
    hwid_devices = None
    if user_uuid:
        subscription = get_subscription_by_uuid(user_uuid)
        hwid_devices = get_hwid_devices(user_uuid)
    
    telegram_id_for_bedolaga = user.get("telegramId") or (query if query.isdigit() else None)
    bedolaga_user = get_bedolaga_user(str(telegram_id_for_bedolaga)) if telegram_id_for_bedolaga and _bedolaga_configured() else None
    
    # Сохраняем данные для навигации по кнопкам
    context.user_data["last_user_data"] = {
        "user": user,
        "subscription": subscription,
        "hwid_devices": hwid_devices,
        "bedolaga_user": bedolaga_user,
    }
    
    try:
        await processing_msg.delete()
        username_display = user.get('username', 'N/A')
        header = f"✅ <b>Пользователь</b> @{username_display}\n\n"
        if bedolaga_user:
            balance_str = _format_bedolaga_balance(bedolaga_user)
            header += f"💰 <b>Баланс (Bedolaga):</b> {balance_str} ₽\n\n"
        section_text = get_section_text("profile", user, subscription, hwid_devices)
        full_text = header + section_text
        keyboard = build_section_keyboard("profile", context.user_data["last_user_data"])
        await update.message.reply_text(
            full_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        if MINI_APP_URL:
            try:
                await context.bot.set_chat_menu_button(
                    chat_id=update.effective_chat.id,
                    menu_button=MenuButtonWebApp(text="📱 Приложение", web_app=WebAppInfo(url=MINI_APP_URL)),
                )
            except Exception as e:
                logger.debug("set_chat_menu_button: %s", e)
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения: {e}")
        await update.message.reply_text(
            f"❌ Произошла ошибка.\nОшибка: {str(e)}"
        )


async def post_init(application: Application) -> None:
    """Глобальная кнопка меню — «Команды». Кнопку Mini App ставим только менеджерам при /start и поиске."""
    try:
        await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as e:
        logger.warning("post_init set_chat_menu_button: %s", e)


def main():
    """Основная функция запуска бота"""
    logger.info("Запуск бота...")

    # Сохранение состояния между перезапусками (топики поддержки, счётчик тикетов, user_data)
    persistence_path = os.getenv("PERSISTENCE_PATH", "/data/bot_state.pickle")
    try:
        persistence = PicklePersistence(filepath=persistence_path)
        logger.info("Persistence: %s", persistence_path)
    except Exception as e:
        logger.warning("Persistence отключена (%s): %s", persistence_path, e)
        persistence = None

    builder = Application.builder().token(BOT_TOKEN).post_init(post_init)
    if persistence:
        builder = builder.persistence(persistence)
    application = builder.build()
    
    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(support_card_callback, pattern="^sup"))
    application.add_handler(CallbackQueryHandler(close_ticket_callback, pattern="^close_ticket:"))
    application.add_handler(CallbackQueryHandler(call_manager_callback, pattern="^call_manager$"))
    application.add_handler(CallbackQueryHandler(client_close_ticket_callback, pattern="^client_close_ticket$"))
    application.add_handler(CallbackQueryHandler(squad_assign_callback, pattern="^squad"))
    application.add_handler(CallbackQueryHandler(action_callback, pattern="^(act:|hwid_del:)"))
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^s:"))
    # Текст, фото, документы и т.д. в группе поддержки (топики)
    support_content = (
        filters.TEXT | filters.PHOTO | filters.Document.ALL
        | filters.VIDEO | filters.VOICE | filters.AUDIO
    ) & ~filters.COMMAND
    if SUPPORT_GROUP_ID:
        support_chat_ids = _support_chat_ids()
        application.add_handler(MessageHandler(
            support_content & filters.Chat(support_chat_ids),
            handle_support_group_message,
        ))
    # Текст, фото, документы от клиентов; текст от менеджеров (поиск)
    application.add_handler(MessageHandler(support_content, dispatch_message))
    
    # Проверка ключа ИИ (Groq или Gemini) при старте (видно в логах: OK или ошибка)
    if _AI_SUPPORT_AVAILABLE and is_ai_enabled():
        check_ai_key_at_startup()
    # Запускаем бота
    logger.info("Бот запущен и готов к работе")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
