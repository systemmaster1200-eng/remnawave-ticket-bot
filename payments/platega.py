"""
Провайдер Platega.io.
Ориентир по реализации: интеграция из remnawave-STEALTHNET-Bot.
"""

import logging
import os
from typing import Any, Optional

import requests

from .base import InvoiceResult, PaymentProvider

logger = logging.getLogger(__name__)

PLATEGA_API_URL = "https://app.platega.io/transaction/process"
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
PLATEGA_METHOD_SBP = 2
PLATEGA_METHOD_CARD = 11
PLATEGA_METHOD_CARD_INTERNATIONAL = 12
PLATEGA_METHOD_CRYPTO = 13


def _normalize_base_url(raw: str) -> str:
    base_url = (raw or "").strip()
    if not base_url:
        return ""
    if base_url.startswith("https:/") and not base_url.startswith("https://"):
        base_url = "https://" + base_url[7:]
    elif base_url.startswith("http:/") and not base_url.startswith("http://"):
        base_url = "http://" + base_url[6:]
    elif "://" not in base_url:
        base_url = "https://" + base_url
    return base_url.rstrip("/")


def _pick_first_string(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and str(value).strip():
            return str(value).strip()
    return None


class PlategaProvider(PaymentProvider):
    def __init__(
        self,
        merchant_id: str,
        secret: str,
        base_url: str = "",
        default_payment_method: int = PLATEGA_METHOD_SBP,
    ):
        self._merchant_id = (merchant_id or "").strip()
        self._secret = (secret or "").strip()
        self._base_url = _normalize_base_url(base_url)
        self._default_payment_method = default_payment_method

    @property
    def name(self) -> str:
        return "platega"

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
        return_url: Optional[str] = None,
        failed_url: Optional[str] = None,
        description: Optional[str] = None,
        **kwargs: Any,
    ) -> InvoiceResult:
        if not self._merchant_id or not self._secret:
            return InvoiceResult(success=False, error="Platega не настроен (merchant id / secret)")
        amount = round(amount, 2)
        if amount <= 0:
            return InvoiceResult(success=False, error="Сумма должна быть больше 0")

        payment_method = payment_method_id if payment_method_id is not None else self._default_payment_method
        return_url = (return_url or self._base_url or "https://t.me").strip()
        failed_url = (failed_url or return_url).strip()
        body = {
            "paymentMethod": int(payment_method),
            "paymentDetails": {
                "amount": amount,
                "currency": (currency or "RUB").upper(),
            },
            "description": description or f"Оплата заказа {payment_id}",
            "return": return_url,
            "failedUrl": failed_url,
            # payload — единственный стабильный идентификатор, который потом удобно вытащить из webhook.
            "payload": payment_id,
        }
        headers = {
            "Content-Type": "application/json",
            "X-MerchantId": self._merchant_id,
            "X-Secret": self._secret,
        }

        try:
            response = requests.post(PLATEGA_API_URL, json=body, headers=headers, timeout=20)
        except Exception as e:
            logger.exception("Platega create transaction: %s", e)
            return InvoiceResult(success=False, error=str(e))

        text = (response.text or "").strip()
        try:
            data = response.json() if text else {}
        except Exception:
            data = {}

        if response.status_code == 401:
            return InvoiceResult(success=False, error="Platega: неверный Merchant ID или secret")
        if response.status_code != 200:
            error_message = (
                data.get("message")
                or data.get("error")
                or text[:200]
                or f"Ошибка API: {response.status_code}"
            )
            return InvoiceResult(success=False, error=f"Platega: {error_message}")

        payment_url = data.get("redirect") or data.get("url") or data.get("paymentUrl")
        external_id = data.get("transactionId") or data.get("id")
        if not payment_url:
            return InvoiceResult(success=False, error="Platega не вернул ссылку на оплату")

        return InvoiceResult(
            success=True,
            payment_id=payment_id,
            payment_url=str(payment_url),
            external_id=str(external_id) if external_id else None,
        )

    def verify_webhook(self, request: Any) -> tuple[Optional[str], Optional[str]]:
        data = request.get_json(silent=True) if hasattr(request, "get_json") else None
        if not isinstance(data, dict) or not data:
            return None, "empty_body"

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
        payment_id = _pick_first_string(
            data.get("payload"),
            tx_obj.get("payload"),
            nested_obj.get("payload"),
            data.get("orderId"),
            data.get("order_id"),
            data.get("merchant_order_id"),
        )
        if not payment_id:
            return None, "missing_identifiers"
        if status in PLATEGA_FAILED_STATUSES:
            return payment_id, "payment_failed"
        if status not in PLATEGA_SUCCESS_STATUSES:
            return payment_id, "ignored_status"
        return payment_id, None


def get_platega_provider() -> Optional[PlategaProvider]:
    merchant_id = (os.getenv("PLATEGA_MERCHANT_ID") or "").strip()
    secret = (os.getenv("PLATEGA_SECRET") or "").strip()
    if not merchant_id or not secret:
        return None
    base_url = _normalize_base_url(
        (os.getenv("PAYMENTS_BASE_URL") or os.getenv("MINI_APP_DOMAIN") or "").strip()
    )
    method_raw = (os.getenv("PLATEGA_PAYMENT_METHOD") or "").strip()
    default_payment_method = PLATEGA_METHOD_SBP
    if method_raw:
        try:
            default_payment_method = int(method_raw)
        except ValueError:
            logger.warning("PLATEGA_PAYMENT_METHOD is invalid: %s", method_raw)
    return PlategaProvider(
        merchant_id=merchant_id,
        secret=secret,
        base_url=base_url,
        default_payment_method=default_payment_method,
    )
