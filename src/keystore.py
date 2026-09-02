#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
keystore.py — 只读加载 secrets/.env 里的 key，按“实际存在的 KEY 数量”决定账户列表。
不打印真值，仅在 debug 时打码。所有读写都基于它。
注意：此模块原名 secrets.py，但会遮蔽 Python 标准库 secrets 模块（edge-tts 等库依赖
stdlib secrets.token_hex），导致运行时崩溃并静默回退。已重命名为 keystore 彻底规避。
"""
import os
from dataclasses import dataclass

from models import AccountInfo


@dataclass
class AccountKey:
    index: int          # 1, 2, 3 ...
    name: str           # 显示名（来自 settings.yaml accounts）
    api_key: str
    api_secret: str
    passphrase: str = ""  # 额外认证参数（如 OKX 密码短语），为空表示不需要
    exchange: str = ""  # 交易所名（如 'GATE' / 'OKX'），用于路由/日志/抑制键空间隔离


def _mask(s: str) -> str:
    if not s:
        return "<空>"
    return s[:4] + "****" + s[-4:] if len(s) > 8 else "****"


def load_accounts(settings: dict, prefix: str = "GATE") -> list[AccountKey]:
    """从 secrets/.env 读取交易所 API key，返回账户列表。

    prefix 默认为 'GATE'，以保持现有老系统不动；
    后续多交易所场景由上层显式传入对应 key_prefix 调用。
    """
    prefix = prefix or "GATE"
    HERE = os.path.dirname(os.path.abspath(__file__))
    ROOT = os.path.dirname(HERE)
    env_path = os.path.join(ROOT, "secrets", ".env")
    env: dict[str, str] = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()

    accounts_cfg = (settings.get("accounts") or {}) if settings else {}
    accounts: list[AccountKey] = []
    idx = 1
    while True:
        suffix = "" if idx == 1 else f"_{idx}"
        key = env.get(f"{prefix}_API_KEY{suffix}")
        secret = env.get(f"{prefix}_API_SECRET{suffix}")
        if not key or not secret:
            break
        name = accounts_cfg.get(f"account_{idx}", f"account_{idx}")
        pf = env.get(f"{prefix}_PASSPHRASE{suffix}", "")
        accounts.append(AccountKey(index=idx, name=name, api_key=key, api_secret=secret, passphrase=pf, exchange=prefix))
        idx += 1

    return accounts


def describe(accounts: list[AccountKey]) -> str:
    """打码后的账户摘要（用于日志/启动信息）。"""
    return "; ".join(
        f"[{a.exchange or 'UNKNOWN'}/{a.name}] {_mask(a.api_key)}"
        for a in accounts
    )
