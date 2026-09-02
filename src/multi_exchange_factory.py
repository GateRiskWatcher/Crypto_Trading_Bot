#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
multi_exchange_factory.py — 启用交易所客户端的统一工厂入口（Phase 7 工厂）

职责：
  - 从 settings.yaml + keystore 读取“启用 + 有 key”的交易所列表。
  - 构造每个交易所的适配器客户端实例（仅在此处实例化）。
  - 提供统一访问接口：按交易所名取客户端、遍历所有客户端、客户端的只读方法白名单。
  - 保持 Gate 旧系统完全隔离可用：Gate 适配器内部复用已有 ReadOnlyGateClient，
    不改动 gate_client.py 的任何已有代码。

安全契约（与项目铁律一致）：
  - 本模块不存储/打印任何 key 真值。
  - 不自动发起任何网络请求——客户端实例化后是否调用、调哪些方法，由上层控制。
  - 交易所适配器必须实现 BaseExchange 且自带 readonly_methods 约束；
    实例化阶段即可校验白名单非空，防止配置错误导致的无约束客户端。
  - 若某个交易所不可用/实例化失败，不让异常杀掉整个进程——记录错误并跳过，
    上层看到的是“该交易所缺失”的状态。

向下兼容：
  - 若 settings 中没有 exchanges 块，或者没有任何交易所启用+有 key，
    返回空工厂；旧版 watch.py 的 MultiAccountWatcher 路径不受影响。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from base_exchange import BaseExchange
from exchange_config import (
    ExchangeConfig,
    clear_registered,
    load_accounts_for_exchange,
    load_from_settings,
    load_enabled_with_keys,
)
from models import AccountKey as _AccountKey


# ------------------------------------------------------------------
# 工厂产出：每个交易所的“已实例化适配器”包装
# ------------------------------------------------------------------

class ExchangeClientEntry:
    """
    一个已就绪的交易所适配器入口。

    属性全是只读信息（不含 key 真值），供上层遍历/路由使用。
    """

    def __init__(
        self,
        exchange_name: str,
        client: BaseExchange,
        cfg: ExchangeConfig,
        account: _AccountKey,
    ):
        self.exchange_name = exchange_name
        self.client = client
        self.cfg = cfg
        self.account = account

    @property
    def readonly_methods(self) -> frozenset[str]:
        return self.client.readonly_methods

    @property
    def host(self) -> str:
        return self.cfg.host

    @property
    def priority(self) -> int:
        return self.cfg.priority

    def __repr__(self) -> str:
        return (
            f"ExchangeClientEntry(exchange={self.exchange_name!r}, "
            f"host={self.host!r}, priority={self.priority}, "
            f"readonly_methods={self.readonly_methods})"
        )


# ------------------------------------------------------------------
# 多交易所工厂
# ------------------------------------------------------------------

class MultiExchangeFactory:
    """
    创建并持有多个交易所适配器实例的工厂。

    用法（伪代码）：
      factory = MultiExchangeFactory.from_settings(settings)
      for entry in factory.entries:
          print(entry.exchange_name, entry.readonly_methods)
          info = entry.client.account()
          ...
    """

    def __init__(self, entries: List[ExchangeClientEntry]):
        self.entries = entries

    @property
    def by_name(self) -> Dict[str, ExchangeClientEntry]:
        return {e.exchange_name: e for e in self.entries}

    def get(self, name: str) -> Optional[ExchangeClientEntry]:
        return self.by_name.get(name.upper())

    def exchanges(self) -> List[str]:
        return [e.exchange_name for e in self.entries]

    # ------------------------------------------------------------------
    # 实例化入口
    # ------------------------------------------------------------------

    @classmethod
    def from_settings(
        cls,
        settings: dict,
        *,
        adapters: Optional[Dict[str, type[BaseExchange]]] = None,
    ) -> "MultiExchangeFactory":
        """
        从 settings 构造已启用+有 key 的交易所适配器列表。

        参数：
          - settings ：完整 settings dict（含 exchanges / accounts）。
          - adapters ：可选的 {交易所名(如 'GATE'/'OKX'/'BINANCE'): BaseExchange 子类}
                       映射。若不传，工厂对 Gate 使用内置包装器，其余交易所若未注册适配器
                       则跳过（不硬编码到工厂内部）。
        """
        clear_registered()
        all_cfgs = load_from_settings(settings)
        keystore_available = cls._detect_keystore_availability_static(settings)
        enabled_cfgs = load_enabled_with_keys(settings, keystore_available)

        if adapters is None:
            adapters = {}

        # 注册用户传入的适配器到 exchange_config 注册表（仅作工厂查找用）
        for name, cls_ in adapters.items():
            if not isinstance(cls_, type) or not issubclass(cls_, BaseExchange):
                raise TypeError(f"adapters 中的条目必须是 BaseExchange 子类: {name}")
            from exchange_config import register_exchange

            # 注册一个虚拟的 ExchangeConfig 以便后续查找；真实配置仍以 settings 为准
            dummy = ExchangeConfig(name=name)
            register_exchange(dummy)

        entries: List[ExchangeClientEntry] = []
        for cfg in enabled_cfgs:
            name = cfg.name.upper()
            account_keys = load_accounts_for_exchange(cfg, settings)
            if not account_keys:
                continue

            adapter_cls = cls._resolve_adapter_cls(name, cfg, adapters)
            if adapter_cls is None:
                # 没有为该交易所注册适配器——跳过（不报错，避免阻塞 Gate）
                continue

            # 为每个账户实例化一个适配器客户端（一个交易所可有多个账户）
            for ak in account_keys:
                try:
                    client = adapter_cls(account=ak, config=cfg)
                except Exception as exc:
                    # 实例化失败不杀进程——该账户不可用，记录后跳过
                    _log_info(
                        f"[{name}] 适配器实例化失败 (account #{ak.index} {ak.name!r}): {exc}"
                    )
                    continue

            # 白名单校验：实例化成功就必须有非空只读方法集
            if not client.readonly_methods:
                _log_info(f"[{name}] 适配器 {adapter_cls.__name__} readonly_methods 为空，实例将被跳过")
                continue

            entries.append(
                ExchangeClientEntry(
                    exchange_name=client.exchange_name,
                    client=client,
                    cfg=cfg,
                    account=account_keys[0] if account_keys else None,
                )
            )

        entries.sort(key=lambda e: (e.priority, e.exchange_name.lower()))
        return cls(entries)

    # ------------------------------------------------------------------
    # 内部帮助
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_keystore_availability_static(settings: dict) -> Dict[str, bool]:
        """
        不依赖外部函数，直接复用 exchange_config._env_var 语义来检测 keystore 可用性。
        保持与 detect_keystore_availability 行为一致。
        """
        from pathlib import Path

        all_cfgs = load_from_settings(settings)
        prefixes = {cfg.key_prefix.upper() for cfg in all_cfgs}
        out: Dict[str, bool] = {}
        for p in prefixes:
            idx = 1
            found = False
            while True:
                suffix = "" if idx == 1 else f"_{idx}"
                k = _env_var_static(p, f"API_KEY{suffix}")
                s = _env_var_static(p, f"API_SECRET{suffix}")
                if k and s:
                    found = True
                    break
                if not k and not s:
                    break
                idx += 1
                if idx > 50:
                    break
            out[p] = found
        return out

    @staticmethod
    def _resolve_adapter_cls(
        name: str,
        cfg: ExchangeConfig,
        adapters: Dict[str, type[BaseExchange]],
    ) -> Optional[type[BaseExchange]]:
        """
        根据交易所名/配置决定使用哪个适配器类。

        优先级：
          1. 用户显式传入 adapters[name]
          2. auth_type 映射到的内置适配器（目前仅 Gate 有内置包装器）
          3. 无匹配则返回 None（工厂跳过该交易所）
        """
        if name in adapters:
            return adapters[name]

        auth = (cfg.auth_type or "").lower()
        if auth == "gate":
            from gate_client_wrapper import GateAdapter

            return GateAdapter
        # OKX / BINANCE / custom：用户需要自行提供适配器类，工厂不硬编码
        return None

    # ------------------------------------------------------------------
    # 启动日志用小工具（不打印真值）
    # ------------------------------------------------------------------

    def summary(self) -> str:
        """
        返回人类可读的已启用交易所摘要（打码，不含真 key）。
        """
        lines: List[str] = []
        for e in self.entries:
            ak = e.account
            key_mask = "****"
            if ak and ak.api_key:
                key_mask = (ak.api_key[:4] + "****" + ak.api_key[-4:]) if len(ak.api_key) > 8 else "****"
            lines.append(
                f"  - {e.exchange_name}: host={e.host}, priority={e.priority}, "
                f"账户={ak.name if ak else '?'}, key={key_mask}, "
                f"passphrase={'有' if ak and ak.passphrase else '无'}, "
                f"readonly_methods={e.readonly_methods}"
            )
        return "\n".join(lines) if lines else "  (无已启用交易所)"


# ------------------------------------------------------------------
# 帮助函数（静态路径，以免循环导入）
# ------------------------------------------------------------------

def _env_var_static(prefix: str, suffix: str) -> str:
    """
    从 secrets/.env 读取单个变量值（不打印真值）。

    与 exchange_config._env_var 保持一致的读取语义。
    """
    from pathlib import Path

    env_path = Path(__file__).resolve().parent / "secrets" / ".env"
    if not env_path.is_file():
        return ""
    data: Dict[str, str] = {}
    with env_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data.get(f"{prefix}_{suffix}", "")


def _log_info(msg: str) -> None:
    """
    轻量启动日志——不依赖项目日志系统，仅 print 到 stdout。
    真值永不出现。
    """
    print(f"[MultiExchangeFactory] {msg}")


# ------------------------------------------------------------------
# 自测（python multi_exchange_factory.py 直接跑）
# ------------------------------------------------------------------

if __name__ == "__main__":
    import yaml

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    settings_path = os.path.join(ROOT, "config", "settings.yaml")
    with open(settings_path, "r", encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    factory = MultiExchangeFactory.from_settings(settings)
    print("=== MultiExchangeFactory 自测 ===")
    print(factory.summary())
