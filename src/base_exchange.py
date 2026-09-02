#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
base_exchange.py — 交易所适配器抽象基类（Phase 7 骨架）

职责：
  - 规定每个交易所适配器必须实现的方法集，返回模型层对象（AccountInfo / Position），
    让上层（watch / risk_board）能用同一套接口处理多交易所。
  - 保留“只读契约”：基类不提供任何写/下单/撤单入口；白名单方法集由各适配器自行
    声明（通常来自 settings.yaml readonly_methods），上层可用它做额外校验。
  - 未来 src/okx_client.py / src/binance_client.py 只需继承本类，框架即能统一处理。

向下兼容铁律：
  - 本模块不构造任何真实 API 客户端、不发起任何网络请求。
  - 不改动 gate_client.py 的任何已有类/方法；Gate 适配器后续在包装层复用老代码。
  - 不在本模块读取 secrets/.env 真值——key 的读取仍由 keystore 负责。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class ExchangeError(Exception):
    """交易所适配器通用错误基类。上层按需捕获，不把交易所异常直接泄给全局循环。"""


class NotReadonlyMethodError(ExchangeError, PermissionError):
    """尝试调用不在白名单里的操作时抛出——防止任何非只读行为滑进来的框架级防线。"""


class BaseExchange(ABC):
    """
    每个交易所适配器的最小合约。

    方法签名尽量对齐 models.py 的标准对象（AccountInfo / Position），
    而不是裸 dict——目的是让上层业务逻辑逐步脱离交易所原始结构。
    """

    # ------------------------------------------------------------------
    # 身份与安全白名单
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def exchange_name(self) -> str:
        """交易所名（如 'GATE' / 'OKX' / 'BINANCE'），用于 symbol 映射与日志前缀。"""

    @property
    @abstractmethod
    def readonly_methods(self) -> frozenset[str]:
        """
        该适配器允许调用的方法集合（安全白名单），通常直接来自 settings.yaml
        里 `exchanges.<name>.readonly_methods`。基类不硬编码，只读约束由配置驱动。
        """

    # ------------------------------------------------------------------
    # 认证/连接构造（子类自行决定：有的用 SDK，有的手动签名）
    # ------------------------------------------------------------------

    @abstractmethod
    def __init__(self, account: "AccountKey", config: "ExchangeConfig"):
        """
        适配器初始化。

        参数约定：
          - account ：来自 keystore 的 AccountKey（api_key/secret/passphrase 齐全）。
          - config  ：ExchangeConfig（含 host / key_prefix / extra / readonly_methods 等）。
        基类不规定如何存；子类按交易所认证方式自行构造客户端/签名器。
        """

    # ------------------------------------------------------------------
    # 标准只读数据接口（返回模型对象）
    # ------------------------------------------------------------------

    @abstractmethod
    def account(self) -> Optional["AccountInfo"]:
        """当前账户快照（AccountInfo）。失败/不可用返回 None，不抛走顶层循环。"""

    @abstractmethod
    def positions(self) -> list["Position"]:
        """当前持仓列表（Position）。空则返回空列表。"""

    @abstractmethod
    def tickers(self, contracts: Optional[list[str]] = None) -> list[dict[str, Any]]:
        """
        行情快照。

        contracts 为 None 时返回所有监控合约；指定列表时尽量只返回这些合约。
        返回的 dict 至少包含归一化键（缺失的用 0/None 填）：

          - contract     ：合约标识（内部标准格式，由适配器自行 normalize）
          - mark_price   ：标记价（float）
          - index_price  ：指数价（float，交易所不提供可与 mark_price 一致或 0）
          - funding_rate ：资金费率（float，比例；例如 0.0001 = 0.01%）
          - oi           ：未平仓量（float，单位依交易所；上层按张/美元面值解读）
        """

    # ------------------------------------------------------------------
    # 可用性/限流检测（带默认实现，适配器可覆盖）
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """
        检测该交易所客户端当前是否可达。默认尝试获取一个轻量 ticker。
        子类可覆盖为更廉价的检查。
        """
        try:
            self.tickers(contracts=["BTC_USDT"])
            return True
        except Exception:
            return False

    def is_rate_limited(self, exc: BaseException) -> bool:
        """
        给出一个交易所异常是否应被视为限流的初步判断。
        默认基于异常类型/消息关键字；各适配器可覆盖以匹配自己交易所的限流语义。
        """
        msg = getattr(exc, "message", "") or str(exc).lower()
        return bool(
            getattr(exc, "status", None) == 429
            or "too many request" in msg
            or "rate limit" in msg
            or "frequency limit" in msg
            or "rate_limit" in msg
        )

    # ------------------------------------------------------------------
    # 白名单校验小工具（供适配器内部调用）
    # ------------------------------------------------------------------

    def _check_readonly(self, method: str) -> None:
        """
        校验某个方法名是否在该交易所的只读白名单里。
        不在白名单里则抛 NotReadonlyMethodError——这是防止写方法滑进来的最后一道框架防线。
        """
        if method not in self.readonly_methods:
            raise NotReadonlyMethodError(
                f"[{self.exchange_name}] 拒绝非只读方法: {method}"
            )


# 前向引用占位（避免循环导入；适配器实现端可从 models import 真类型）
AccountInfo = "AccountInfo"
Position = "Position"
AccountKey = "AccountKey"
ExchangeConfig = "ExchangeConfig"
