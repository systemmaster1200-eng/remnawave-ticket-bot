"""Базовый интерфейс для платёжных провайдеров (расширяемо под другие системы)."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Any, Dict


@dataclass
class InvoiceResult:
    """Результат создания счёта."""
    success: bool
    payment_id: Optional[str] = None
    payment_url: Optional[str] = None
    error: Optional[str] = None


class PaymentProvider(ABC):
    """Интерфейс платёжного провайдера."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Идентификатор провайдера (freekassa, ...)."""
        pass

    @abstractmethod
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
        **kwargs: Any,
    ) -> InvoiceResult:
        """Создать счёт и вернуть ссылку на оплату."""
        pass

    @abstractmethod
    def verify_webhook(self, request) -> tuple[Optional[str], Optional[str]]:
        """Проверить вебхук. Возвращает (payment_id, error)."""
        pass
