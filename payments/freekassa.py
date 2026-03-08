"""
Провайдер Freekassa (api.fk.life).
API: https://docs.freekassa.net/#section/API
  - 2.1: запросы POST на https://api.fk.life/v1/ в JSON; API ключ — со страницы настроек ЛК.
  - 2.2: подпись — сортировка параметров по ключам, значения через "|", HMAC-SHA256(эта_строка, api_key).
  - createOrder: https://docs.freekassa.net/#operation/createOrder
Вебхук оповещения — подпись MD5 (раздел 1.7).
"""
import hashlib
import hmac
import logging
import os
import time
from typing import Optional, Any

import requests

from .base import PaymentProvider, InvoiceResult
from .storage import pending_get

logger = logging.getLogger(__name__)

API_BASE = "https://api.fk.life/v1"
API_URL_ORDERS_CREATE = f"{API_BASE}/orders/create"
API_URL_SHOPS = f"{API_BASE}/shops"
FREEEKASSA_ALLOWED_IPS = {
    "168.119.157.136",
    "168.119.60.227",
    "178.154.197.79",
    "51.250.54.238",
}

# Способы оплаты: 44 — СБП (API), 36 — карты РФ, 43 — SberPay
PAYMENT_METHOD_SBP = 44
PAYMENT_METHOD_CARD_RU = 36
PAYMENT_METHOD_SBER_PAY = 43

_cached_public_ip: Optional[str] = None


def _get_public_ip() -> str:
    """Публичный IP сервера (как в BEDOLAGA — Freekassa может сверять с ним подпись)."""
    global _cached_public_ip
    if _cached_public_ip:
        return _cached_public_ip
    env_ip = (os.getenv("SERVER_PUBLIC_IP") or "").strip()
    if env_ip:
        _cached_public_ip = env_ip
        return env_ip
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            r = requests.get(url, timeout=3)
            if r.status_code == 200 and r.text:
                ip = r.text.strip()
                if ip and len(ip.split(".")) == 4:
                    _cached_public_ip = ip
                    logger.info("Freekassa: публичный IP сервера %s", ip)
                    return ip
        except Exception as e:
            logger.debug("Freekassa get_public_ip %s: %s", url, e)
    fallback = "127.0.0.1"
    _cached_public_ip = fallback
    return fallback


class FreekassaProvider(PaymentProvider):
    def __init__(
        self,
        api_key: str,
        shop_id: int,
        webhook_secret: str,
        base_url: str = "",
        default_payment_method: int = PAYMENT_METHOD_SBP,
        secret1_for_signature: Optional[str] = None,
        use_api_key_for_sign: bool = False,
        use_secret1_for_sign: bool = False,
    ):
        self._api_key = api_key
        self._shop_id = shop_id
        self._webhook_secret = webhook_secret
        self._base_url = (base_url or "").rstrip("/")
        self._default_payment_method = default_payment_method
        # Как в BEDOLAGA: для API подпись от API ключа. Secret1 — только при use_secret1_for_sign=True (FREEEKASSA_USE_SECRET1_FOR_SIGN=1).
        self._sign_key = (
            (secret1_for_signature or "").strip()
            if ((secret1_for_signature or "").strip() and use_secret1_for_sign)
            else api_key
        )

    @property
    def name(self) -> str:
        return "freekassa"

    def _signature(self, data: dict) -> str:
        """Подпись по доке 2.2: ksort, implode('|', values), HMAC-SHA256(строка, api_key)."""
        keys = sorted(k for k in data if k != "signature")
        # Без нормализации — как при сериализации JSON на сервере (100.0 -> "100.0", 68953 -> "68953")
        payload = "|".join(str(data[k]) for k in keys)
        return hmac.new(
            self._sign_key.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def create_invoice(
        self,
        amount: float,
        currency: str,
        payment_id: str,
        email: str,
        ip: str,
        client_id: int,
        manager_id: int,
        user_uuid: Optional[str] = None,
        payment_method_id: Optional[int] = None,
        notification_url: Optional[str] = None,
        **kwargs: Any,
    ) -> InvoiceResult:
        if not self._api_key or not self._shop_id:
            return InvoiceResult(success=False, error="Freekassa не настроен (API key / shop ID)")
        i = payment_method_id if payment_method_id is not None else self._default_payment_method
        amount = round(amount, 2)
        if amount <= 0:
            return InvoiceResult(success=False, error="Сумма должна быть больше 0")
        # Как в BEDOLAGA: при целой сумме — int (в строке подписи "100", не "100.0").
        final_amount = int(amount) if float(amount).is_integer() else amount
        nonce = int(time.time_ns())
        # Freekassa может сверять подпись с реальным IP запроса — при 127.0.0.1 подставляем публичный IP.
        use_ip = ip
        if not use_ip or use_ip in ("127.0.0.1", "localhost", "::1"):
            use_ip = _get_public_ip()
        # Все поля в теле участвуют в подписи (в т.ч. notification_url, если передан).
        data = {
            "shopId": self._shop_id,
            "nonce": nonce,
            "paymentId": payment_id,
            "i": i,
            "email": email,
            "ip": use_ip,
            "amount": final_amount,
            "currency": currency,
        }
        if notification_url:
            data["notification_url"] = notification_url
        data["signature"] = self._signature(data)
        # По доке 2.1–2.2 авторизация только через signature в теле; заголовок Authorization не используется.
        headers = {"Content-Type": "application/json"}
        try:
            r = requests.post(API_URL_ORDERS_CREATE, json=data, headers=headers, timeout=15)
            resp_text = (r.text or "").strip()[:500]
            if r.status_code != 200:
                # При Wrong signature — строка, от которой считали подпись (для отладки)
                sign_keys = sorted(k for k in data if k != "signature")
                sign_payload = "|".join(str(data[k]) for k in sign_keys)
                logger.warning(
                    "Freekassa orders/create: status=%s shopId=%s body=%s sign_payload=%s",
                    r.status_code,
                    self._shop_id,
                    resp_text or "(пусто)",
                    sign_payload[:200],
                )
                err_msg = f"Ошибка API: {r.status_code}"
                if resp_text:
                    try:
                        rb = r.json()
                        api_msg = rb.get("message") or rb.get("error") or rb.get("msg") or resp_text[:200]
                        err_msg += f" Ответ: {api_msg}"
                    except Exception:
                        err_msg += f" Тело: {resp_text[:200]}"
                if r.status_code == 401:
                    # При «Wrong signature»: попробуйте убрать FREEEKASSA_SECRET1 (подпись от API ключа) или задать FREEEKASSA_USE_API_KEY_FOR_SIGN=1
                    shops_data = {"shopId": self._shop_id, "nonce": int(time.time_ns())}
                    shops_data["signature"] = self._signature(shops_data)
                    shops_r = requests.post(API_URL_SHOPS, json=shops_data, headers=headers, timeout=10)
                    shops_text = (shops_r.text or "").strip()[:300]
                    logger.warning(
                        "Freekassa /shops: status=%s shopId=%s body=%s",
                        shops_r.status_code,
                        self._shop_id,
                        shops_text or "(пусто)",
                    )
                    if shops_r.status_code == 200:
                        try:
                            shops_json = shops_r.json()
                            ids = [s.get("id") for s in (shops_json.get("shops") or []) if s.get("id") is not None]
                            if ids:
                                err_msg += f" Список ID магазинов из API: {ids}. Подставьте один в FREEEKASSA_SHOP_ID."
                            else:
                                err_msg += " /shops вернул пустой список."
                        except Exception as ex:
                            logger.debug("Freekassa shops parse: %s", ex)
                            err_msg += " Неверный shopId или API ключ. FREEEKASSA_SECRET1=секретное_слово_1 для подписи."
                    else:
                        err_msg += (
                            " shopId или ключ для подписи не подходят. "
                            "Попробуйте FREEEKASSA_USE_API_KEY_FOR_SIGN=1 (подпись от API ключа) или проверьте FREEEKASSA_SECRET1=секретное_слово_1."
                        )
                return InvoiceResult(success=False, error=err_msg)
            resp = r.json()
            if resp.get("type") != "success":
                return InvoiceResult(
                    success=False,
                    error=resp.get("message", "Ошибка создания заказа"),
                )
            location = resp.get("location") or (r.headers.get("Location") if r.headers else None)
            if not location:
                return InvoiceResult(success=False, error="Нет ссылки на оплату в ответе")
            return InvoiceResult(
                success=True,
                payment_id=payment_id,
                payment_url=location,
            )
        except Exception as e:
            logger.exception("Freekassa create order: %s", e)
            return InvoiceResult(success=False, error=str(e))

    def verify_webhook(self, request: Any) -> tuple[Optional[str], Optional[str]]:
        """Проверяет вебхук Freekassa. Возвращает (payment_id, error)."""
        remote_ip = (
            request.headers.get("X-Real-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr
        )
        if remote_ip and remote_ip not in FREEEKASSA_ALLOWED_IPS:
            logger.warning("Freekassa webhook from disallowed IP: %s", remote_ip)
            return None, "invalid_ip"
        # form-data: MERCHANT_ID, AMOUNT, MERCHANT_ORDER_ID, SIGN, ...
        merchant_id = request.values.get("MERCHANT_ID")
        amount = request.values.get("AMOUNT")
        merchant_order_id = request.values.get("MERCHANT_ORDER_ID")
        sign = request.values.get("SIGN")
        if not all([merchant_id, amount, merchant_order_id, sign]):
            return None, "missing_params"
        expected = hashlib.md5(
            f"{merchant_id}:{amount}:{self._webhook_secret}:{merchant_order_id}".encode()
        ).hexdigest()
        if sign.lower() != expected.lower():
            return None, "wrong_sign"
        return merchant_order_id, None


def get_freekassa_provider() -> Optional[FreekassaProvider]:
    api_key = (os.getenv("FREEEKASSA_API_KEY") or "").strip()
    shop_raw = (os.getenv("FREEEKASSA_SHOP_ID") or "").strip()
    webhook_secret = (os.getenv("FREEEKASSA_WEBHOOK_SECRET") or "").strip()
    secret1 = (os.getenv("FREEEKASSA_SECRET1") or "").strip()
    use_api_key = (os.getenv("FREEEKASSA_USE_API_KEY_FOR_SIGN") or "").strip().lower() in ("1", "true", "yes")
    use_secret1 = (os.getenv("FREEEKASSA_USE_SECRET1_FOR_SIGN") or "").strip().lower() in ("1", "true", "yes")
    base_url = (os.getenv("PAYMENTS_BASE_URL") or os.getenv("MINI_APP_DOMAIN") or "").strip()
    if base_url:
        if base_url.startswith("https:/") and not base_url.startswith("https://"):
            base_url = "https://" + base_url[7:]
        elif base_url.startswith("http:/") and not base_url.startswith("http://"):
            base_url = "http://" + base_url[6:]
        elif "://" not in base_url:
            base_url = "https://" + base_url
    if not api_key or not shop_raw:
        return None
    try:
        shop_id = int(shop_raw)
    except ValueError:
        return None
    return FreekassaProvider(
        api_key=api_key,
        shop_id=shop_id,
        webhook_secret=webhook_secret,
        base_url=base_url,
        secret1_for_signature=secret1 or None,
        use_api_key_for_sign=use_api_key,
        use_secret1_for_sign=use_secret1,
    )
