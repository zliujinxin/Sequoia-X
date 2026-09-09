"""按配置创建行情 Provider。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import MarketDataProvider, ProviderError

if TYPE_CHECKING:
    from sequoia_x.core.config import Settings


def create_provider(settings: "Settings", name: str | None = None) -> MarketDataProvider:
    provider_name = name or settings.data_provider
    if provider_name == "baostock":
        from .baostock import BaostockProvider

        return BaostockProvider(adjustment=settings.data_adjustment)
    if provider_name == "easy_tdx":
        from .easy_tdx import EasyTdxProvider

        return EasyTdxProvider(
            adjustment=settings.data_adjustment,
            timeout=settings.easy_tdx_timeout,
            host=settings.easy_tdx_host,
            mac_host=settings.easy_tdx_mac_host,
            port=settings.easy_tdx_port,
        )
    raise ProviderError(f"不支持的数据源：{provider_name}")
