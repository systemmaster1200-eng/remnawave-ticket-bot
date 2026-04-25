# Платежи: провайдеры и хранилище ожидающих счетов.
import os
from typing import Callable, Optional

from .base import InvoiceResult, PaymentProvider
from .freekassa import FreekassaProvider, get_freekassa_provider
from .platega import PlategaProvider, get_platega_provider
from .storage import pending_add, pending_pop, pending_get, pending_pop_candidates

_PROVIDER_LOADERS: dict[str, Callable[[], Optional[PaymentProvider]]] = {
    "freekassa": get_freekassa_provider,
    "platega": get_platega_provider,
}


def _get_requested_provider_names() -> list[str]:
    raw = (os.getenv("PAYMENT_PROVIDER") or os.getenv("PAYMENTS_PROVIDER") or "").strip().lower()
    preferred: list[str] = []
    if raw and raw not in {"all", "auto"}:
        for item in raw.replace(";", ",").split(","):
            name = item.strip().lower()
            if name and name in _PROVIDER_LOADERS and name not in preferred:
                preferred.append(name)
    for name in _PROVIDER_LOADERS:
        if name not in preferred:
            preferred.append(name)
    return preferred or list(_PROVIDER_LOADERS.keys())


def get_payment_provider_by_name(name: str) -> Optional[PaymentProvider]:
    provider_name = (name or "").strip().lower()
    loader = _PROVIDER_LOADERS.get(provider_name)
    return loader() if loader else None


def get_available_payment_providers() -> list[PaymentProvider]:
    providers: list[PaymentProvider] = []
    for name in _get_requested_provider_names():
        provider = get_payment_provider_by_name(name)
        if provider:
            providers.append(provider)
    return providers


def get_available_payment_provider_names() -> list[str]:
    return [provider.name for provider in get_available_payment_providers()]


def get_payment_provider_name() -> str:
    providers = get_available_payment_providers()
    return providers[0].name if providers else ""


def get_payment_provider() -> Optional[PaymentProvider]:
    providers = get_available_payment_providers()
    return providers[0] if providers else None

__all__ = [
    "InvoiceResult",
    "PaymentProvider",
    "FreekassaProvider",
    "PlategaProvider",
    "get_freekassa_provider",
    "get_platega_provider",
    "get_payment_provider_name",
    "get_payment_provider_by_name",
    "get_payment_provider",
    "get_available_payment_providers",
    "get_available_payment_provider_names",
    "pending_add",
    "pending_pop",
    "pending_get",
    "pending_pop_candidates",
]
