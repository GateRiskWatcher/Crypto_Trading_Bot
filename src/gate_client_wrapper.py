#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gate_client_wrapper.py — Gate 交易所适配器（BaseExchange 封装）

职责：
  - 把已有的 src/gate_client.py（ReadOnlyGateClient / MultiAccountWatcher）封装成 BaseExchange。
  - 不改动 gate_client.py 的任意已有代码；本包装器只新增一层。
  - readonly_methods 从 settings.yaml 的 GATE 块读取（保持与现有 _READONLY_METHODS 一致）。
  - 返回模型对象（AccountInfo / Position），让上层逐步脱离交易所原始 dict。
  - 故障返回 None / 空列表，不让异常杀掉上层循环。

安全契约（与项目铁律一致）：
  - 仅复用已有的只读客户端；不新增任何写/下单/撤单入口。
  - 不存储/打印 key 真值；打码仅用于启动日志。
  - 若 Gate 不可用，工厂看到的是“该交易所缺失”状态，而非崩溃。
"""

from __future__ import annotations

from typing import Any, Optional

from base_exchange import BaseExchange
from exchange_config import ExchangeConfig
from gate_client import ReadOnlyGateClient
from keystore import AccountKey as _AccountKey
from models import AccountInfo, Position


class GateAdapter(BaseExchange):
    """
    Gate.io USDT 永续只读适配器（已有 ReadOnlyGateClient 的 BaseExchange 封装）。

    每个实例对应 Gate 的一个账户（一个 AccountKey）。
    """

    def __init__(self, account: _AccountKey, config: ExchangeConfig):
        if not account.api_key or not account.api_secret:
            raise ValueError("GateAdapter 需要有效的 api_key / api_secret")

        host = config.host or "https://api.gateio.ws/api/v4"
        self._raw = ReadOnlyGateClient(account, host=host)
        self._account_key = account
        self._config = config

        # readonly_methods 优先从 settings.yaml 读取，若为空则回退到 SDK 白名单集合
        methods = set(config.readonly_methods) if config.readonly_methods else set()
        if not methods:
            # 回退：使用 gate_client._READONLY_METHODS（避免配置丢失时失去约束）
            import gate_client as _gc

            methods = set(getattr(_gc, "_READONLY_METHODS", set())) or methods
        self._readonly_methods = frozenset(methods)

    # ------------------------------------------------------------------
    # BaseExchange 身份
    # ------------------------------------------------------------------

    @property
    def exchange_name(self) -> str:
        return "GATE"

    @property
    def readonly_methods(self) -> frozenset[str]:
        return self._readonly_methods

    # ------------------------------------------------------------------
    # 标准只读接口（模型对象）
    # ------------------------------------------------------------------

    def account(self) -> Optional[AccountInfo]:
        try:
            return self._raw.get_account_model(display_name=self._account_key.name)
        except Exception:
            return None

    def positions(self) -> list[Position]:
        try:
            return self._raw.get_positions_models() or []
        except Exception:
            return []

    def tickers(self, contracts: Optional[list[str]] = None) -> list[dict[str, Any]]:
        try:
            raw = self._raw.get_tickers(contract=contracts[0] if contracts else None) or []
            if contracts and len(contracts) > 1:
                wanted = {c.lower() for c in contracts}
                raw = [t for t in raw if str(t.get("contract", "")).lower() in wanted]
            return raw
        except Exception:
            return []

    # ------------------------------------------------------------------
    # 调试/可观测 small helpers
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        try:
            return self._raw.ping()
        except Exception:
            return False

