from app.config import PROVIDER
from app.providers.base import CallProvider
from app.providers.retell import RetellProvider

_PROVIDERS = {
    "retell": RetellProvider,
}


def get_provider() -> CallProvider:
    try:
        provider_cls = _PROVIDERS[PROVIDER]
    except KeyError:
        raise ValueError(f"Unknown PROVIDER: {PROVIDER!r}")
    return provider_cls()