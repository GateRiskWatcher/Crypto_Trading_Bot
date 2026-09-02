#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
exchange_config.py — 交易所配置加载与适配器注册表（Phase 2 雏形）

职责：
  - 从 settings.yaml 的 `exchanges:` 块读取每个交易所的 host / key 前缀 /
    认证参数 / 只读 methods 白名单 / fallback / 启用开关。
  - 按“启用 + 拥有 key”自动注册该交易所的客户端（后续接入真实
    okx_client.py / binance_client.py 时只需实现 BaseExchange 即可）。
  - 为 watch.py / risk_board.py 提供统一入口：
      load_enabled_exchanges()   -> 启用的交易所配置列表
      get_exchange_client(name)   -> 按名称取已注册客户端（带 fallback）
      list_all_registered()       -> 当前已注册的所有交易所名

向下兼容铁律（不触老系统）：
  - 若 settings.yaml 里没有 `exchanges:` 块，行为 = 全空列表，原有
    gate_client / keystore / watch 不受任何影响。
  - 不在此模块构造任何真实 API 客户端实例，防止无 key 时产生网络误调。
  - 所有 key/secret/passphrase 仍仅由 keystore 加载，配置块只读“如何连”。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ------------------------------------------------------------------
# 配置数据类
# ------------------------------------------------------------------

@dataclass
class ExchangeConfig:
    """
    一个交易所在 settings.yaml 里的配置块。

    字段约定：
      name               ：配置块的键名（如 'GATE', 'OKX', 'BINANCE'）
      host               ：API 宿主（如 https://api.gateio.ws/api/v4）
      key_prefix         ：.env 里 key/secret 的前缀（如 'GATE' → GATE_API_KEY）
      auth_type          ：认证方式标签（'gate' / 'okx' / 'binance' / 'custom'），
                          后续用它选择签名策略，不硬编码在主程序里。
      readonly_methods   ：该交易所允许调用的只读方法名列表（安全白名单）。
      fallback           ：本交易所不可用时的备选交易所名（空 = 不 fallback）。
      enabled            ：是否参与加载（默认 False，显式开启才注册）。
      priority           ：轮询优先级（数字越小越先），用于多交易所排序。
      account_name_key   ：settings.accounts 里对应的 key 后缀名前缀，
                          默认与 name 一致（如 'GATE' → account_1/GATE_...）。
      extra              ：携带给适配器实现的自由扩展字段（不由框架解释）。
    """

    name: str = ""
    host: str = ""
    key_prefix: str = ""
    auth_type: str = "custom"
    readonly_methods: List[str] = field(default_factory=list)
    fallback: str = ""
    enabled: bool = False
    priority: int = 100
    account_name_key: str = ""
    # 自由扩展：OKX passphrase、Binance recvWindow、特定交易所特有参数都放在这里
    extra: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 工厂：从 settings.yaml 里的任意嵌套 dict 构建（宽容解析，缺字段用默认）
    # ------------------------------------------------------------------

    @classmethod
    def from_yaml_block(cls, name: str, block: dict) -> "ExchangeConfig":
        """将 cfg['exchanges'][name] 的 dict → ExchangeConfig。"""
        if not isinstance(block, dict):
            block = {}
        host = str(block.get("host", "")).strip()
        key_prefix = str(block.get("key_prefix", name)).strip().upper()
        # 容忍 auth_type 为空，默认从 key_prefix 猜一个（仅作为显示/路由线索）
        auth_type = str(block.get("auth_type", "custom")).strip().lower()
        if not auth_type or auth_type == "custom":
            # 简单映射：按前缀猜测认证类型（仅供参考，真实判断以实现为准）
            if key_prefix.upper() in ("GATE", "GT"):
                auth_type = "gate"
            elif key_prefix.upper() in ("OKX", "OK"):
                auth_type = "okx"
            elif key_prefix.upper() in ("BINANCE", "BNB"):
                auth_type = "binance"
            else:
                auth_type = "custom"

        methods = block.get("readonly_methods")
        if not isinstance(methods, list):
            methods = []
        methods = [str(m).strip() for m in methods if str(m).strip()]

        fallback = str(block.get("fallback", "")).strip().upper()
        enabled = bool(block.get("enabled", False))
        try:
            priority = int(block.get("priority", 100))
        except (TypeError, ValueError):
            priority = 100

        account_key = str(block.get("account_name_key", "")).strip()
        if not account_key:
            account_key = key_prefix

        extra: dict = {}
        raw_extra = block.get("extra")
        if isinstance(raw_extra, dict):
            for k, v in raw_extra.items():
                extra[str(k)] = v

        return cls(
            name=name,
            host=host,
            key_prefix=key_prefix,
            auth_type=auth_type,
            readonly_methods=methods,
            fallback=fallback,
            enabled=enabled,
            priority=priority,
            account_name_key=account_key,
            extra=extra,
        )


# ------------------------------------------------------------------
# 全局注册表（仅配置级别注册，不实例化客户端）
# ------------------------------------------------------------------

_registered: Dict[str, ExchangeConfig] = {}


def register_exchange(cfg: ExchangeConfig) -> None:
    """把一个交易所配置放入注册表（名称去重）。"""
    if not cfg.name:
        return
    _registered[cfg.name.upper()] = cfg


def load_from_settings(settings: dict) -> List[ExchangeConfig]:
    """
    从 settings dict 的 `exchanges:` 块解析所有交易所配置，并写入全局注册表。

    返回：解析出的配置列表（与注册表内容一致，按 name 排序）。
    若 settings 中没有 `exchanges:` 键或为空，则注册表不变、返回空列表。
    """
    raw = settings.get("exchanges")
    if not isinstance(raw, dict):
        return []

    clear_registered()
    out: List[ExchangeConfig] = []
    for name, block in raw.items():
        cfg = ExchangeConfig.from_yaml_block(name, block)
        register_exchange(cfg)
        out.append(cfg)
    out.sort(key=lambda c: (c.priority, c.name.lower()))
    return out


def clear_registered() -> None:
    """清空注册表（主要供测试/重载使用）。"""
    _registered.clear()


def list_registered() -> List[str]:
    """返回当前注册表里的所有交易所名（已排序）。"""
    names = list(_registered.keys())
    names.sort(key=lambda n: (_registered[n].priority, n.lower()))
    return names


def get_registered(name: str) -> Optional[ExchangeConfig]:
    """按名称取注册配置（名称不区分大小写）。"""
    return _registered.get(name.upper())


# ------------------------------------------------------------------
# 按“启用 + 拥有 key”筛选真正可用的交易所
# ------------------------------------------------------------------

def load_enabled_with_keys(
    settings: dict,
    keystore_available: Dict[str, bool],
) -> List[ExchangeConfig]:
    """
    返回“启用了 + 在 keystore 里检测到对应前缀 key”的交易所配置列表。

    keystore_available 的键是 key_prefix（如 'GATE'），值是该前缀下是否
    存在至少一对 API_KEY / API_SECRET（由 keystore 查询结果填入）。

    忽略未启用、未配置 host、或无 key 的交易所——它们不会进入轮询。
    """
    all_cfgs = load_from_settings(settings)
    out: List[ExchangeConfig] = []
    for cfg in all_cfgs:
        if not cfg.enabled:
            continue
        if not cfg.host:
            # 没有 host 则无法连接——排除（保留在注册表里仅作文档/扩展用）
            continue
        prefix = cfg.key_prefix.upper()
        if not keystore_available.get(prefix):
            # 该交易所所需 key 不存在——跳过（但不报错，防止误觑）
            continue
        out.append(cfg)
    out.sort(key=lambda c: (c.priority, c.name.lower()))
    return out


# ------------------------------------------------------------------
# 符号标注转换工具（交易所适配器间标准化合约标识用的小工具集）
# ------------------------------------------------------------------

# 常见交易所合约标识风格（仅作 symbol 映射的默认规则，适配器可覆盖）
_DEFAULT_SYMBOL_RULES: Dict[str, Dict[str, Any]] = {
    # GATE: BTC_USDT 风格（_ 分隔，币种_计价）
    "GATE": {
        "separator": "_",
        "quote_currency": "USDT",
        "strip_quote_suffix": True,
        "contract_is_symbol": True,  # 合约码（BTC_USDT）即标的符号
    },
    # OKX: BTC-USDT-SWAP 风格（- 分隔，含产品后缀）
    "OKX": {
        "separator": "-",
        "quote_currency": "USDT",
        "strip_quote_suffix": False,
        "product_suffix": "SWAP",
        "strip_product_suffix": True,  # 从 BTC-USDT-SWAP → BTC-USDT 或 BTC
    },
    # Binance: BTCUSDT 风格（无分隔）
    "BINANCE": {
        "separator": "",
        "quote_currency": "USDT",
        "strip_quote_suffix": True,
        "strip_quote_at_end": True,
    },
}


def normalize_symbol(symbol: str, exchange: str) -> str:
    """
    将各交易所的合约标识统一为“内部标准格式”（暂定 GATE 风格 `BTC_USDT`）。

    目的：不同交易所适配器输出 Position.contract 时，能产生一致的标识，
    方便上层（risk / watch / signal_oi）不用关心交易所差异。

    本函数只做字符串映射，不验证合约真实存在性。映射规则从
    _DEFAULT_SYMBOL_RULES 读取，适配器如有特殊需要可自行覆盖。
    """
    rule = _DEFAULT_SYMBOL_RULES.get(exchange.upper())
    if rule is None:
        # 未知交易所：原样返回（框架不妄自猜测）
        return symbol

    sep = rule.get("separator", "")
    quote_cur = str(rule.get("quote_currency", "USDT")).upper()
    s = symbol.strip()

    if rule.get("strip_product_suffix"):
        # 去掉产品类型后缀如 '-SWAP' / '-FUTURES'
        prod = str(rule.get("product_suffix", "")).upper()
        if prod and s.upper().endswith(f"-{prod}"):
            s = s[: -len(prod) - 1]  # 再掉分隔符

    if rule.get("strip_quote_suffix") or rule.get("strip_quote_at_end"):
        # 去掉报价货币后缀
        upper = s.upper()
        if quote_cur and upper.endswith(quote_cur):
            # 防止误切：确保前面有分隔符或至少长度合理
            cut_pos = len(s) - len(quote_cur)
            if cut_pos >= 1:
                tail = s[cut_pos:]
                if rule.get("strip_quote_at_end") and not rule.get("separator"):
                    # Binance 风格：直接切尾
                    s = s[:cut_pos]
                elif rule.get("separator") and tail.startswith(sep):
                    s = s[:cut_pos]
                else:
                    # 有分隔符但不在末尾恰好？保守保留原样
                    pass
    return s


def denormalize_symbol(standard: str, exchange: str) -> str:
    """
    逆操作：内部标准格式 → 该交易所的合约标识风格。

    主要用于“上层决定了一个合约，想构造出适配器调用所需的合约字符串”。
    规则来自 _DEFAULT_SYMBOL_RULES，同样是预设默认，适配器可自行覆盖。
    """
    rule = _DEFAULT_SYMBOL_RULES.get(exchange.upper())
    if rule is None:
        return standard

    quote_cur = str(rule.get("quote_currency", "USDT")).upper()
    sep = str(rule.get("separator", "")).upper()
    s = standard.strip()
    upper_s = s.upper()

    # 确保输入已经是“标准格式”（含报价货币）的情况下再拼接
    if not rule.get("strip_quote_suffix") and not rule.get("strip_quote_at_end"):
        # OKX 类默认规则认为输入是带报价货币的
        if rule.get("product_suffix") and not upper_s.endswith(rule.get("product_suffix", "").upper()):
            return f"{s}{sep}{rule['product_suffix']}"
        return s

    # Binance / GATE 类：若没带报价货币则补上
    if quote_cur and not upper_s.endswith(quote_cur):
        if rule.get("separator"):
            return f"{s}{sep}{quote_cur}"
        return f"{s}{quote_cur}"
    return s


# ------------------------------------------------------------------
# 基于配置的账户名路由小工具（供后续 keystore 拓展用）
# ------------------------------------------------------------------

def account_name_for_exchange(cfg: ExchangeConfig, accounts_cfg: dict, idx: int) -> str:
    """
    根据 exchange 配置 + settings.accounts + 序号，产生该账户的“显示名key”。

    约定沿用现有 settings.accounts 结构：
      account_1 / account_2 ... 对应 keystore 读出的序号。
    若配置了 account_name_key（如 'GATE'），则优先尝试 accounts_cfg[account_name_key + '_' + idx]，
    没有则回退为 f'account_{idx}'。
    """
    key = cfg.account_name_key or cfg.name
    candidate = f"{key}_{idx}"
    if accounts_cfg and candidate in accounts_cfg:
        return str(accounts_cfg[candidate])
    fallback = f"account_{idx}"
    if accounts_cfg and fallback in accounts_cfg:
        return str(accounts_cfg[fallback])
    return fallback


# ------------------------------------------------------------------
# 多交易所账户加载（网关到 keystore / 凭据读取）
# ------------------------------------------------------------------

def load_accounts_for_exchange(
    cfg: ExchangeConfig,
    settings: dict,
) -> list[AccountKey]:
    """
    按一个交易所配置从 keystore 读取该交易所的账户列表（多账户、多前缀支持）。

    读取规则（与 setup_keys.py 及 keystore 保持一致）：
      - API_KEY / API_SECRET 变量名 = <key_prefix>_API_KEY[_N] / <key_prefix>_API_SECRET[_N]
      - 额外认证参数（如 OKX 密码短语）变量名 =
            cfg.extra.passphrase_key（若存在），否则回退为 <key_prefix>_PASSPHRASE[_N]。
        扫描时按 [_N] 增量展开，缺失补为空字符串（不跳过整个账户）。

    返回列表中的每个 AccountKey 都会填入：
      - index / name / api_key / api_secret / passphrase（已按上述规则读取）
      - exchange 属性 = cfg.name（供上层按交易所路由日志/告警/抑制 key）

    不存在任何 key 时返回空列表——上游会在 load_enabled_with_keys 再次过滤，
    此函数只负责“把已知前缀的 key 读出来”，不独立做启用/主机门槛。
    """
    prefix = cfg.key_prefix.upper()
    accounts_cfg = (settings.get("accounts") or {}) if settings else {}

    # --- API key / secret ---
    pairs_by_index: dict[int, tuple[str, str]] = {}
    idx = 1
    while True:
        suffix = "" if idx == 1 else f"_{idx}"
        key = _env_var(f"{prefix}_API_KEY{suffix}")
        secret = _env_var(f"{prefix}_API_SECRET{suffix}")
        if not key or not secret:
            break
        pairs_by_index[idx] = (key, secret)
        idx += 1
        if idx > 50:
            break

    if not pairs_by_index:
        return []

    # --- optional passphrase ---
    pf_key_name = _passphrase_key_name(cfg) or f"{prefix}_PASSPHRASE"
    passphrases_by_index: dict[int, str] = {}
    idx = 1
    while True:
        suffix = "" if idx == 1 else f"_{idx}"
        pf = _env_var(f"{pf_key_name}{suffix}")
        passphrases_by_index[idx] = pf or ""
        # 仅在根本没有 key _pair_ 的索引上才停；否则继续允许“有的账户有 pf、有的是空”
        if idx not in pairs_by_index and not pf:
            break
        idx += 1
        if idx > 50:
            break

    out: list[AccountKey] = []
    for idx, (key, secret) in pairs_by_index.items():
        name = account_name_for_exchange(cfg, accounts_cfg, idx)
        pf = passphrases_by_index.get(idx, "")
        out.append(
            AccountKey(
                index=idx,
                name=name,
                api_key=key,
                api_secret=secret,
                passphrase=pf,
            )
        )
    return out


def _passphrase_key_name(cfg: ExchangeConfig) -> str:
    """从 cfg.extra.passphrase_key 读取密码短语变量名（如 'OKX_PASSPHRASE'），无则返回空。"""
    extra = cfg.extra
    if not isinstance(extra, dict):
        return ""
    name = extra.get("passphrase_key")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return ""


def _env_var(name: str) -> str:
    """
    从 secrets/.env 读取单个变量值（不打印真值）。

    与 keystore.load_accounts() 使用同一条 .env 读取路径，行为一致。
    """
    from pathlib import Path
    import os as _os

    env_path = Path(__file__).resolve().parent.parent / "secrets" / ".env"
    if not env_path.is_file():
        return ""
    data: dict[str, str] = {}
    with env_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data.get(name, "")


# ------------------------------------------------------------------
# 可用交易所检测（纯信息，不构造客户端）
# ------------------------------------------------------------------


def detect_keystore_availability(settings: dict) -> dict[str, bool]:
    """
    返回 {key_prefix: 是否存在至少一对 API_KEY / API_SECRET}。

    供 load_enabled_with_keys() 使用，也可独立用于启动日志里的“有哪些交易所准备好了”。
    """
    all_cfgs = load_from_settings(settings)
    prefixes = {cfg.key_prefix.upper() for cfg in all_cfgs}
    out: dict[str, bool] = {}
    for p in prefixes:
        idx = 1
        found = False
        while True:
            suffix = "" if idx == 1 else f"_{idx}"
            k = _env_var(f"{p}_API_KEY{suffix}")
            s = _env_var(f"{p}_API_SECRET{suffix}")
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


# ------------------------------------------------------------------
# 自测（python exchange_config.py 直接跑）
# ------------------------------------------------------------------

if __name__ == "__main__":
    import yaml

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    settings_path = os.path.join(ROOT, "config", "settings.yaml")
    with open(settings_path, "r", encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    clear_registered()
    cfgs = load_from_settings(settings)
    print("已注册的 exchanges 配置块：", [c.name for c in cfgs])
    for c in cfgs:
        print(
            f"  - {c.name}: enabled={c.enabled}, host={c.host}, "
            f"key_prefix={c.key_prefix}, auth_type={c.auth_type}, "
            f"fallback={c.fallback}, priority={c.priority}, "
            f"readonly_methods={c.readonly_methods}, extra={c.extra}"
        )

    print("\n--- keystore 可用性检测 ---")
    avail = detect_keystore_availability(settings)
    for p, ok in avail.items():
        print(f"  {p}: {'有 key' if ok else '无 key'}")

    print("\n--- 按交易所读取账户（不打印真值，仅摘要）---")
    for cfg in cfgs:
        accts = load_accounts_for_exchange(cfg, settings)
        print(f"  [{cfg.name}] -> {len(accts)} 个账户")
        for a in accts:
            _masked = (a.api_key[:4] + "****" + a.api_key[-4:]) if len(a.api_key) > 8 else "****"
            print(f"      #{a.index} name={a.name!r} key={_masked} passphrase={'有' if a.passphrase else '无'}")
