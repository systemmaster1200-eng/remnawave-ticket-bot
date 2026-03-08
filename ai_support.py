# -*- coding: utf-8 -*-
"""
Интеграция ИИ для автоответов поддержки VPN: Groq и/или Google AI Studio (Gemini).
ИИ отвечает на простые вопросы, видит контекст клиента (без секретов), историю диалога и ответы менеджеров.
"""

import os
import logging
import tempfile
import requests
from typing import Optional, Dict, Any, List

try:
    from groq import Groq
    from groq import PermissionDeniedError, RateLimitError, APIStatusError
    _GROQ_SDK_AVAILABLE = True
except ImportError:
    Groq = None
    PermissionDeniedError = RateLimitError = APIStatusError = None
    _GROQ_SDK_AVAILABLE = False

logger = logging.getLogger(__name__)

# Провайдер: groq | gemini (AI_SUPPORT_API_TYPE или AI_PROVIDER)
_ai_type = (os.getenv("AI_SUPPORT_API_TYPE") or os.getenv("AI_PROVIDER") or "groq").strip().lower()
AI_PROVIDER = "gemini" if _ai_type == "gemini" else "groq"

# --- Groq ---
_def_key = (os.getenv("AI_SUPPORT_API_KEY") or os.getenv("GROQ_API_KEY") or "").strip()
_keys_raw = (os.getenv("AI_SUPPORT_API_KEYS") or "").strip()
GROQ_API_KEYS_LIST = [k.strip() for k in _keys_raw.split(",") if k.strip()] if _keys_raw else ([_def_key] if _def_key else [])
GROQ_API_KEY = GROQ_API_KEYS_LIST[0] if GROQ_API_KEYS_LIST else ""
GROQ_MODEL = (os.getenv("GROQ_MODEL") or "llama-3.1-8b-instant").strip()
_raw_models = (os.getenv("GROQ_MODELS") or "").strip()
GROQ_MODELS_LIST = [m.strip() for m in _raw_models.split(",") if m.strip()] if _raw_models else [GROQ_MODEL]
GROQ_VISION_MODEL = (os.getenv("GROQ_VISION_MODEL") or "").strip()
GROQ_API_BASE = "https://api.groq.com/openai/v1"
GROQ_PROXY = (os.getenv("GROQ_PROXY") or "").strip()

# --- Google AI Studio (Gemini) — https://ai.google.dev/gemini-api/docs/quickstart ---
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_AI_API_KEY") or "").strip()
GEMINI_MODEL = (os.getenv("GEMINI_MODEL") or "gemini-2.0-flash").strip()  # или gemini-3-flash-preview
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Лимит сообщений в истории для контекста (чтобы не превысить лимит токенов)
MAX_HISTORY_MESSAGES = 20

# Регулярка для вырезания тегов <think>...</think> (Telegram не поддерживает, Groq иногда возвращает)
import re
_RE_STRIP_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


# Если ИИ всё же выдал матерную формулировку бана — заменяем на спокойный ответ (успокоить и помочь)
_RE_OFFENSIVE_BAN = re.compile(
    r"(ты\s+пытался\s+нас\s+на\s*[*\u002a]?\s*бать|мы\s+в\s+эти\s+игры\s+не\s+играем|на\s*[*\u002a]?\s*бать|на[*\u002a]?бать)",
    re.IGNORECASE
)
_CALM_REPLY_IF_BAN_PHRASE = "Давай по делу. Опиши, в чём проблема — помогу разобраться."

# Отказ отвечать — заменяем на ответ по делу
_RE_REFUSAL = re.compile(
    r"(я\s+не\s+могу\s+ответить|не\s+могу\s+помочь\s+с\s+этим|обратите\s+внимание\s+на\s+следующую)",
    re.IGNORECASE
)
# Любой ответ про бан — ИИ часто банит за оскорбления; заменяем на «по делу»
_RE_ANY_BAN_REPLY = re.compile(
    r"(бан\s*навсегда|прощается\s*с\s*тобой|eagleguard\s*прощается|"
    r"ты\s*нарушил\s*правила|не\s*допускаются|мат\s*,\s*оскорбления)",
    re.IGNORECASE
)


def sanitize_ai_reply_for_telegram(text: Optional[str]) -> str:
    """Убирает теги <think>, матерные формулировки; отказ отвечать и любой бан — ответ «по делу»."""
    if not text or not isinstance(text, str):
        return (text or "").strip()
    s = _RE_STRIP_THINK.sub("", text)
    s = re.sub(r"<think>.*", "", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"</think>", "", s, flags=re.IGNORECASE)
    # Любой ответ про бан или матерную формулировку бана — показываем спокойный ответ
    s_lower = s.lower()
    if (
        _RE_OFFENSIVE_BAN.search(s)
        or _RE_ANY_BAN_REPLY.search(s)
        or ("навсегда" in s_lower and "прощается" in s_lower)
        or ("бан" in s_lower and "прощается" in s_lower)
    ):
        s = _CALM_REPLY_IF_BAN_PHRASE
    # Если ИИ отказался отвечать — даём ответ по делу
    elif _RE_REFUSAL.search(s):
        s = _CALM_REPLY_IF_BAN_PHRASE
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s).strip()
    return s.strip() or text.strip()

# Глобальный пул примеров «клиент → ответ» из ВСЕХ чатов — LLM обучается на них каждый запрос
GLOBAL_EXAMPLES_KEY = "support_ai_global_examples"
MAX_GLOBAL_EXAMPLES = 500
NUM_EXAMPLES_IN_PROMPT = 25


def is_ai_enabled() -> bool:
    if AI_PROVIDER == "gemini":
        return bool(GEMINI_API_KEY)
    return bool(GROQ_API_KEY)


def _get_key_at_request_time() -> str:
    """Ключ на момент запроса. Как в рабочем боте: сначала AI_SUPPORT_API_KEY."""
    k = (os.getenv("AI_SUPPORT_API_KEY") or os.getenv("GROQ_API_KEY") or "").strip()
    if not k and GROQ_API_KEYS_LIST:
        k = GROQ_API_KEYS_LIST[0]
    return k or GROQ_API_KEY


def _get_groq_proxies() -> Optional[Dict[str, str]]:
    """Прокси для запросов к api.groq.com (если GROQ_PROXY задан — с этого сервера идёт 403)."""
    p = (os.getenv("GROQ_PROXY") or GROQ_PROXY or "").strip()
    if not p:
        return None
    return {"http": p, "https": p}


def check_groq_key_at_startup() -> bool:
    """Один тестовый запрос к Groq при старте. Логирует OK или 403 — видно, тот ли ключ видит процесс."""
    key = _get_key_at_request_time()
    if not key:
        return False
    url = f"{GROQ_API_BASE}/chat/completions"
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": "hi"}],
    }
    try:
        r = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=15,
            proxies=_get_groq_proxies(),
        )
        if r.status_code == 200:
            logger.info("Groq: ключ рабочий (проверка при старте OK)")
            return True
        if r.status_code == 403:
            logger.warning(
                "Groq: при старте 403. В этом окружении ключ не принимается. "
                "Скопируй в .env ТОТ ЖЕ ключ, что в рабочем боте (AI_SUPPORT_API_KEY=... или GROQ_API_KEY=...), "
                "сохрани файл и перезапусти: docker compose up -d --force-recreate remnawave-manager-bot"
            )
            return False
        logger.warning("Groq: при старте %s %s", r.status_code, r.text[:150])
        return False
    except Exception as e:
        logger.warning("Groq: проверка при старте — %s", e)
        return False


def check_gemini_key_at_startup() -> bool:
    """Проверка ключа Gemini при старте (как в доке: x-goog-api-key)."""
    if not GEMINI_API_KEY:
        return False
    url = f"{GEMINI_API_BASE}/models/{GEMINI_MODEL}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    payload = {"contents": [{"parts": [{"text": "hi"}]}]}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        if r.status_code == 200:
            logger.info("Gemini: ключ рабочий (проверка при старте OK)")
            return True
        logger.warning("Gemini: при старте %s %s", r.status_code, r.text[:150])
        return False
    except Exception as e:
        logger.warning("Gemini: проверка при старте — %s", e)
        return False


def check_ai_key_at_startup() -> bool:
    """Проверка ключа выбранного провайдера (Groq или Gemini) при старте."""
    if AI_PROVIDER == "gemini":
        return check_gemini_key_at_startup()
    return check_groq_key_at_startup()


def build_client_context_for_ai(
    api_user: Optional[Dict[str, Any]],
    subscription: Optional[Dict[str, Any]],
    hwid_devices: Optional[List[Dict[str, Any]]],
    bedolaga_user: Optional[Dict[str, Any]],
    service_name: str,
    tariffs_text: Optional[str] = None,
) -> str:
    """
    Собирает контекст о клиенте для ИИ. Ссылку на подписку передаём — клиенту её можно и нужно давать по запросу.
    Тарифы передаём — по запросу «какие тарифы» перечисляй их из контекста.
    Не передаём: UUID, внутренние токены, чужие данные.
    """
    parts = []
    # Явно: есть ли активная подписка (ИИ должен проверять это при «купил подписку, но ничего нет»)
    sub_user = (subscription or {}).get("user", {}) if subscription else {}
    sub_found = subscription and subscription.get("isFound")
    sub_active = sub_user.get("isActive") if sub_user else False
    if sub_found and sub_active:
        parts.append("Активная подписка: да")
        # Ссылка на подписку — даём ИИ, чтобы по запросу клиента («дай ссылку», «сабка», «подписка») отвечать ссылкой
        sub_url = (subscription or {}).get("subscriptionUrl") or ""
        if sub_url:
            parts.append(f"Ссылка на подписку (выдавать клиенту по запросу): {sub_url}")
    else:
        parts.append("Активная подписка: нет (клиент мог только пополнить баланс — подписку нужно оформить отдельно)")
    if api_user:
        status = sub_user.get("userStatus") or api_user.get("status") or "—"
        parts.append(f"Статус в системе: {status}")
        if api_user.get("expireAt"):
            parts.append(f"Подписка истекает: {api_user.get('expireAt')[:10]}")
        ut = api_user.get("userTraffic") or {}
        used = ut.get("usedTrafficBytes", 0)
        limit = api_user.get("trafficLimitBytes", 0)
        if limit > 0:
            used_gb = round(used / (1024**3), 2)
            limit_gb = round(limit / (1024**3), 2)
            parts.append(f"Трафик: {used_gb} ГБ из {limit_gb} ГБ")
        else:
            used_gb = round(used / (1024**3), 2)
            parts.append(f"Трафик использовано: {used_gb} ГБ (безлимит)")
    # Лимит устройств (HWID) из API — для ответа на «сколько устройств доступно»
    if api_user is not None:
        hwid_limit = api_user.get("hwidDeviceLimit")
        if hwid_limit is not None and str(hwid_limit).strip() != "":
            try:
                n_limit = int(hwid_limit)
                if n_limit > 0:
                    parts.append(f"Лимит устройств (из API): {n_limit}")
                else:
                    parts.append("Лимит устройств (из API): без ограничений")
            except (TypeError, ValueError):
                parts.append(f"Лимит устройств (из API): {hwid_limit}")
        else:
            parts.append("Лимит устройств (из API): не указан (уточняй по тарифу или без ограничений)")
    if hwid_devices is not None:
        parts.append(f"Привязано устройств сейчас: {len(hwid_devices)}")
    if hwid_devices is not None and len(hwid_devices) > 0:
        apps = set()
        for d in hwid_devices:
            platform = (d.get("platform") or d.get("deviceModel") or "").strip()
            if platform:
                apps.add(platform)
        if apps:
            parts.append(f"Устройства/приложения: {', '.join(sorted(apps)[:10])}")
    # Сквады (для кейса «No internal squads» на скрине)
    if api_user is not None:
        active_squads = api_user.get("activeInternalSquads") or []
        if active_squads:
            parts.append("Внутренние сквады: выданы (есть в панели)")
        else:
            parts.append("Внутренние сквады: не выданы или не указаны")
    if bedolaga_user is not None:
        rub = bedolaga_user.get("balance_rubles")
        kopeks = bedolaga_user.get("balance_kopeks", 0)
        balance = float(rub) if rub is not None else (int(kopeks) / 100)
        parts.append(f"Баланс на счёте: {balance:.2f} ₽ (с баланса можно оформить подписку в боте)")
    if tariffs_text:
        parts.append(tariffs_text)
    if not parts:
        return "Данных о клиенте в системе пока нет (не найден в панели)."
    return " | ".join(parts)


def get_system_prompt(service_name: str) -> str:
    bot_username = (os.getenv("BOT_USERNAME") or "").strip().lstrip("@")
    bot_line = ""
    if bot_username:
        bot_line = f"\nСсылка на бота (давать клиенту при запросе «где написать» / «как в бот»): @{bot_username}, https://t.me/{bot_username}\n"
    return f"""Ты — EagleGuard Админ. Твой стиль: суровый минимализм, ирония, общение на "ТЫ". Ты — профи, и ты не любишь, когда тебе парят мозг.{bot_line}

МЫ НЕ БАНИМ ЗА МАТ. За мат, грубость и оскорбления — только успокоить и помочь по делу. Бан — только за мошенничество или обман сервиса (не за слова).

### ИСТОЧНИК ДАННЫХ О КЛИЕНТЕ (ОБЯЗАТЕЛЬНО):
Все данные о подписке, датах, ссылках, тарифах, лимитах бери ТОЛЬКО из блока «Контекст клиента» в этом сообщении (он подгружается из API при каждом сообщении). НЕ бери даты, ссылки, статус из истории чата — там старые данные или другой человек.
• Один контекст = одна правда. Если в текущем блоке «Контекст клиента» написано «Активная подписка: да» — никогда не пиши «подписки нет» или «оформи подписку». Если написано «Активная подписка: нет» — не пиши «подписка есть» или «у тебя до 2026». Не противоречь текущему контексту.
• Не выдумывай: «заблокирован за оскорбления», «ты был забанен» и т.п. — в контексте этого нет, не пиши.
• Если в контексте нет «Подписка истекает» — не придумывай дату. Если «Активная подписка: нет» — не давай ссылку на подписку и не давай инструкции по подключению; скажи оформить подписку в боте.

### ОСНОВНЫЕ ПРАВИЛА:
1. КРАТКОСТЬ: Один вопрос — один короткий ответ. Без "Привет" и "Пожалуйста".
2. ВСТРЕЧНЫЕ ВОПРОСЫ: Если клиент тупит, отвечай вопросом на вопрос: "А что ты делаешь?", "Что именно требует оплатить?".
3. ТЫКАНИЕ В ФАКТЫ: Если клиент не видит очевидного на своем же скрине, пиши: "Тут посмотрите", "На скрин свой посмотри".
4. ПРИЗНАНИЕ ОШИБОК: Если есть баг системы (разница статусов), отвечай сухо: "Это системная ошибка, работаем. У тебя всё активно".
5. НЕ АГРЕССИВНИЧАТЬ И НЕ МАТЕРИТЬСЯ: Тон — сухой, по делу, без оскорблений. Никакой мат, грубости и угроз. Даже при нарушителях — формулировка жёсткая, но без нецензурной лексики.
6. МАТ / ГРУБОСТЬ ОТ КЛИЕНТА: Мы не баним за мат. Если клиент ругается или матерится — не писать про бан, не угрожать. Успокой и переведи в дело: «Ок, давай по делу. В чём проблема?» или «Опиши, что не работает — помогу разобраться.» Помоги решить вопрос.
7. НИКОГДА не отвечай «Я не могу ответить на это» и не отказывайся отвечать. На жалобы и оскорбления («кинули», «наебщики» и т.п.) — не скидывай ссылку и не уходи от темы. Ответь по делу: «Давай по делу. Опиши, что произошло — разберёмся.» Дай ссылку на подписку или инструкцию только если в контексте есть подписка и клиент спрашивает про подключение.

### КЕЙСЫ ДЛЯ ОТВЕТОВ (ПО ТВОИМ СКРИНАМ / ПО КОНТЕКСТУ КЛИЕНТА):

- НЕ РАБОТАЕТ VPN / СЕРВЕРЫ (приоритет): Если клиент пишет, что не работает впн, vpn, не подключается, сервер не работает, всё не работает — всем один ответ: "Сейчас разбираемся с вашей проблемой, как будет решение менеджер ответит вам в чате."

- КЛИЕНТ НЕ ВИДИТ СРОК:
  К: "Почему нет подписки, я оплатил до марта?"
  ИИ: Смотри в блоке «Контекст клиента» строку «Подписка истекает: …». Ответь кратко с этой датой, например: "Тут посмотрите. У тебя до [дата из контекста] всё активно." Если даты в контексте нет — не выдумывай; скажи: "В карточке даты нет, уточни у менеджера."

- КЛИЕНТ ТЕРЯЕТ КНОПКИ:
  К: "Как настроить / где посмотреть?"
  ИИ: "В кабинете посмотри, там всё написано." или "Жми кнопку 'Подписка'."

- ТЕХНИЧЕСКИЙ ТУПНЯК:
  К: "Почему не работает?"
  ИИ: "Может потому что ты не выбрал сервер?"

- БАГ МИНИ-ПРИЛОЖЕНИЯ:
  К: "В боте активно, в приложении нет. Почему?"
  ИИ: "У тебя активна подписка. Так пока у всех показывает, мы скоро это исправим."

- ТРЕБОВАНИЕ СКРИНОВ:
  К: "У меня не работает."
  ИИ: "Скинь скриншот из VPN-приложения. Без данных тикет будет закрыт."

- СКРИН «REMNAWAVE / VLESS / INTERNAL SQUADS» (на скрине видно: Remnawave, VLESS, "Did you forget to add internal squads", "No internal squads found", "User has no internal squads"):
  Смотри в контексте: «Активная подписка: да/нет» и «Внутренние сквады: выданы» или «не выданы».
  • Если в контексте «Активная подписка: да» И «Внутренние сквады: выданы» — пиши: "У тебя есть и подписка, и сквад. Обнови подписку в Happ: перевыпусти или заново добавь подписку по ссылке. После этого должно подхватить."
  • Если «Активная подписка: нет» ИЛИ «Внутренние сквады: не выданы» ИЛИ есть подписка но сквадов нет — пиши: "Передал менеджеру для решения. Сквады или подписку настроят, напишут сюда."

- ССЫЛКА / ПОДКЛЮЧЕНИЕ: Если в контексте «Активная подписка: да» и есть «Ссылка на подписку» — дай ссылку и коротко: страница подписки, приложение, добавить по ссылке. Если «Активная подписка: нет» — не давай ссылку; пиши: оформи подписку в боте (тариф, оплата), потом подключайся.

- БАН: Только для реальных нарушителей (мошенничество, обман сервиса). За мат, грубость, оскорбления — НЕ банить, только успокоить и помочь (правило 2). Фраза про бан — только если в контексте явно указано мошенничество/обман.

### ТАБУ:
- МЫ НЕ БАНИМ ЗА МАТ. Не писать «бан», «нарушил правила», «прощается с тобой» в ответ на мат или оскорбления — только «Давай по делу», помочь решить проблему.
- Никаких: "Здравствуйте", "Извините", "Постараюсь помочь", "Хорошего дня".
- Запрещено: «Я не могу ответить на это», «не могу помочь с этим» — всегда отвечай по делу, успокой и помоги.
- Никакого мата, грубостей, оскорблений и агрессии. Тон — уверенный и по делу, не хамский.
- Не противоречить текущему контексту: не писать «подписки нет», если в контексте «Активная подписка: да», и наоборот.
- Не выдумывать «заблокирован за оскорбления», «ты был забанен» — в контексте этого нет.
- Не выдавай UUID, внутренние токены. Ссылку на подписку — только если она есть в текущем блоке «Контекст клиента»."""


def get_conversation_history(bot_data: Dict[str, Any], client_id: int) -> List[Dict[str, str]]:
    """Возвращает последние сообщения диалога для контекста ИИ."""
    key = "support_ai_conversations"
    if key not in bot_data:
        bot_data[key] = {}
    history = (bot_data[key].get(client_id) or [])[-MAX_HISTORY_MESSAGES:]
    return history


def get_last_user_message(bot_data: Dict[str, Any], client_id: int) -> Optional[str]:
    """Последнее сообщение клиента в диалоге (для привязки ответа менеджера к паре примеров)."""
    history = get_conversation_history(bot_data, client_id)
    for m in reversed(history):
        if m.get("role") == "user":
            return (m.get("content") or "").strip()
    return None


def add_global_example(bot_data: Dict[str, Any], user_message: str, assistant_message: str) -> None:
    """Добавить пару «клиент — ответ» в глобальный пул примеров из всех чатов. ИИ использует их как few-shot."""
    if not (user_message or "").strip() or not (assistant_message or "").strip():
        return
    if GLOBAL_EXAMPLES_KEY not in bot_data:
        bot_data[GLOBAL_EXAMPLES_KEY] = []
    bot_data[GLOBAL_EXAMPLES_KEY].append({
        "user": (user_message or "").strip()[:2000],
        "assistant": (assistant_message or "").strip()[:2000],
    })
    bot_data[GLOBAL_EXAMPLES_KEY] = bot_data[GLOBAL_EXAMPLES_KEY][-MAX_GLOBAL_EXAMPLES:]


def get_global_examples_for_prompt(bot_data: Dict[str, Any], num: int = NUM_EXAMPLES_IN_PROMPT) -> str:
    """Форматирует последние N пар из глобального пула для вставки в системный промпт (обучение на всех чатах)."""
    examples = bot_data.get(GLOBAL_EXAMPLES_KEY) or []
    if not examples:
        return "(Пока нет примеров — отвечай кратко и по делу.)"
    take = examples[-num:]
    lines = []
    for i, pair in enumerate(take, 1):
        u = (pair.get("user") or "").strip()
        a = (pair.get("assistant") or "").strip()
        if u and a:
            lines.append(f"Клиент: {u}")
            lines.append(f"Поддержка: {a}")
            lines.append("")
    return "\n".join(lines).strip() or "(Нет примеров.)"


def add_to_conversation_history(bot_data: Dict[str, Any], client_id: int, role: str, content: str) -> None:
    """Добавляет сообщение в историю (role: user или assistant)."""
    key = "support_ai_conversations"
    if key not in bot_data:
        bot_data[key] = {}
    if client_id not in bot_data[key]:
        bot_data[key][client_id] = []
    bot_data[key][client_id].append({"role": role, "content": content[:4000]})
    # Оставляем только последние N сообщений
    bot_data[key][client_id] = bot_data[key][client_id][-MAX_HISTORY_MESSAGES:]


def transcribe_voice_groq(bot, file_id: str) -> Optional[str]:
    """Скачивает голосовое сообщение и переводит в текст через Groq Whisper."""
    if not GROQ_API_KEY:
        return None
    try:
        f = bot.get_file(file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            f.download_to_drive(tmp.name)
        try:
            with open(tmp.name, "rb") as audio_file:
                r = requests.post(
                    f"{GROQ_API_BASE}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    files={"file": ("voice.ogg", audio_file, "audio/ogg")},
                    data={"model": "whisper-large-v3"},
                    timeout=30,
                    proxies=_get_groq_proxies(),
                )
            if r.status_code == 200:
                data = r.json()
                return (data.get("text") or "").strip()
            logger.warning("Groq Whisper: %s %s", r.status_code, r.text[:200])
            return None
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
    except Exception as e:
        logger.warning("transcribe_voice_groq: %s", e)
        return None


def _groq_chat(
    messages: List[Dict[str, Any]],
    models: Optional[List[str]] = None,
) -> Optional[str]:
    """Вызов Groq API в том же формате, что и рабочий бот: requests.post, max_tokens, temperature 0.7."""
    return _groq_chat_requests(messages, models)


def _groq_chat_requests(
    messages: List[Dict[str, Any]],
    models: Optional[List[str]] = None,
) -> Optional[str]:
    """Groq chat/completions. Если в messages есть content в виде list (мультимедиа) — используем vision-модель."""
    api_key = _get_key_at_request_time()
    if not api_key:
        return None
    # Проверяем, есть ли в сообщениях изображение (content как list)
    use_vision = any(
        isinstance((m.get("content")), list) for m in messages
    )
    if use_vision and GROQ_VISION_MODEL:
        model_list = [GROQ_VISION_MODEL]
    elif use_vision:
        model_list = ["meta-llama/llama-4-scout-17b-16e-instruct"]
    else:
        model_list = models if models else GROQ_MODELS_LIST
    last_error = None
    url = f"{GROQ_API_BASE}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    for model in model_list:
        try:
            payload = {
                "model": model,
                "messages": messages,
            }
            r = requests.post(url, json=payload, headers=headers, timeout=60, proxies=_get_groq_proxies())
            if r.status_code == 200:
                data = r.json()
                choices = data.get("choices", [{}])
                if choices:
                    content = (choices[0].get("message") or {}).get("content", "")
                    if isinstance(content, str) and content:
                        return content.strip()
                    if isinstance(content, list):
                        texts = [p.get("text", "") for p in content if p.get("type") == "text"]
                        if texts:
                            return "\n".join(texts).strip()
                last_error = "empty response"
                continue
            if r.status_code == 403:
                logger.warning(
                    "Groq 403: ключ или Model Permissions. Включите модели: https://console.groq.com/settings/project/limits"
                )
                last_error = r.text[:200]
                break
            if r.status_code == 429 or r.status_code >= 500:
                logger.info("Groq model %s: %s, пробуем следующую", model, r.status_code)
                last_error = r.text[:200]
                continue
            logger.warning("Groq %s: %s %s", model, r.status_code, r.text[:200])
            last_error = r.text[:200]
        except Exception as e:
            logger.warning("Groq chat error %s: %s", model, e)
            last_error = str(e)
    if last_error:
        logger.warning("Groq: all models failed. Last: %s", last_error)
    return None


def _gemini_chat(messages: List[Dict[str, Any]]) -> Optional[str]:
    """Вызов Google AI Studio (Gemini) generateContent. messages: [{"role":"system"|"user"|"assistant","content":"..."}]."""
    if not GEMINI_API_KEY:
        return None
    # systemInstruction — первый system; contents — диалог user/model (assistant -> model)
    system_parts = []
    contents = []
    for m in messages:
        role = (m.get("role") or "user").lower()
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
            continue
        gemini_role = "model" if role == "assistant" else "user"
        contents.append({"role": gemini_role, "parts": [{"text": content}]})
    if not contents:
        return None
    payload = {
        "contents": contents,
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 2048},
    }
    if system_parts:
        payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
    url = f"{GEMINI_API_BASE}/models/{GEMINI_MODEL}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code != 200:
            logger.warning("Gemini %s: %s %s", GEMINI_MODEL, r.status_code, r.text[:200])
            return None
        data = r.json()
        candidates = data.get("candidates") or []
        if not candidates:
            return None
        parts = (candidates[0].get("content") or {}).get("parts") or []
        if not parts:
            return None
        text = (parts[0].get("text") or "").strip()
        return text if text else None
    except Exception as e:
        logger.warning("Gemini chat error: %s", e)
        return None


def get_ai_reply(
    system_prompt: str,
    client_context: str,
    conversation_history: List[Dict[str, str]],
    new_user_message: str,
    service_name: str,
    bot_data: Optional[Dict[str, Any]] = None,
    image_base64: Optional[str] = None,
    image_mime: Optional[str] = None,
) -> Optional[str]:
    """
    Получить ответ ИИ для поддержки.
    new_user_message — текст от клиента (или подпись к фото).
    image_base64 / image_mime — при отправке фото клиентом (только Groq vision; Gemini пока текст).
    """
    if not is_ai_enabled():
        return None
    examples_block = ""
    if bot_data:
        examples_block = "\n\nПримеры ответов поддержки из прошлых диалогов (отвечай в том же стиле):\n" + get_global_examples_for_prompt(bot_data) + "\n\n---\nТекущий диалог:"
    system = (
        system_prompt
        + "\n\n--- Контекст клиента (данные из API/карточки при этом сообщении; дату, ссылку, статус, тарифы бери только отсюда, НЕ из истории чата) ---\n"
        + client_context
        + "\n--- Конец контекста клиента ---"
        + examples_block
    )
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system}]
    for m in conversation_history:
        messages.append({"role": m["role"], "content": m["content"]})
    # Фото: для Groq передаём content как list (text + image_url base64); для Gemini пока только текст
    if image_base64 and image_mime and AI_PROVIDER == "groq":
        prompt = (new_user_message or "Что на скриншоте? Опиши кратко для поддержки VPN.").strip() or "Что на скриншоте?"
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{image_mime};base64,{image_base64}"}},
            ],
        })
    else:
        messages.append({"role": "user", "content": new_user_message or "Клиент отправил изображение."})
    raw = _gemini_chat(messages) if AI_PROVIDER == "gemini" else _groq_chat(messages)
    return sanitize_ai_reply_for_telegram(raw) if raw else None
