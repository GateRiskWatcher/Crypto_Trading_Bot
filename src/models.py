#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
models.py — 标准数据模型（Phase 1）

为所有交易所适配器定义一套“标准语言”：
  - AccountInfo   ：账户快照（权益 / 可用 / 未实现盈亏 / 保证金模式 / 维持保证金 …）
  - Position      ：单个持仓（合约 / 方向 / 张数 / 币数 / 杠杆 / 开仓价 / 标记价 /
                    强平价 / 保证金 / 保证金模式 / 未实现盈亏 / ADL 排名 /
                    强平距离% / ROE%）
  - AlertEvent    ：发往 AlertManager 的标准化告警包

设计原则（Roadmap 阶段1/2 兼 Phase5 集成）：
  - 所有业务逻辑（risk.py 等）尽量基于这些模型对象，而不是散落的原始 dict。
  - 但保持向下兼容：现有代码仍可传/接收 dict；模型构造函数可从 dict 一键构建。
  - 数值字段统一走 float；None 表示“该交易所/该窗口不提供该字段”。
  - exchange / account 用字符串标识（来自 keystore），便于多账户/多交易所扩展。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 小工具：数值化（Gate/OKX/Binance 各自返回字符串 or number，统一成 float）
# ---------------------------------------------------------------------------

def _num(x: Any) -> Optional[float]:
    """字符串/数字 → float；无法转换或 None → None（不像 risk._num 那样返回 0.0）。"""
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# AccountInfo
# ---------------------------------------------------------------------------

@dataclass
class AccountInfo:
    """
    一个交易所账户的标准快照。

    字段约定：
      - exchange      ：交易所标识（如 'GATE' / 'OKX' / 'BINANCE'）
      - account_id    ：该交易所内的账户标识（Gate 用 name，OKX 用 uid，Binance 用用户名等）
      - display_name  ：系统显示名（来自 settings.accounts / keystore AccountKey.name）
      - total_equity  ：总权益（含未实现盈亏），USDT 等基准货币
      - available     ：可用余额
      - unrealised_pnl：未实现盈亏
      - margin_mode   ：'isolated' / 'cross' / 'none' / 'unknown'
      - maintenance_margin ：账户级维持保证金（Gate 返回 maintenance_margin）
      - leverage      ：账户默认杠杆或摘要杠杆（None 表示不提供）
      - currency      ：基准货币（默认 'USDT'）
      - raw           ：原 API 返回的 dict（调试/后向兼容用，不作为业务字段依赖）
    """

    exchange: str
    account_id: str
    display_name: str

    total_equity: Optional[float] = None
    available: Optional[float] = None
    unrealised_pnl: Optional[float] = None
    margin_mode: str = "unknown"
    maintenance_margin: Optional[float] = None
    leverage: Optional[float] = None
    currency: str = "USDT"

    # 调试/兼容用；业务逻辑不应直接依赖 raw 的结构
    raw: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 工厂：从 Gate 风格账户 dict 构建（兼容现有 gate_client 返回）
    # ------------------------------------------------------------------

    @classmethod
    def from_gate_account(cls, acct: dict | list, exchange: str = "GATE",
                          display_name: str = "") -> "AccountInfo":
        """Gate list_futures_accounts 返回的账户 dict/list → AccountInfo。

        Gate 可能直接返回单 dict（全仓账户），也可能返回 list（逐仓账户）。
        若为 list，则取第一个元素构建模型（其余元素由上层自行处理）。
        """
        if isinstance(acct, list):
            acct = acct[0] if acct else {}
        acct = acct or {}
        return cls(
            exchange=exchange,
            account_id=str(acct.get("name") or acct.get("id") or ""),
            display_name=display_name,
            total_equity=_num(acct.get("total")),
            available=_num(acct.get("available")),
            unrealised_pnl=_num(acct.get("unrealised_pnl")),
            # Gate 中 maintenance_margin 是账户级维持保证金
            maintenance_margin=_num(acct.get("maintenance_margin")),
            leverage=_num(acct.get("lever")),
            currency=str(acct.get("currency") or "USDT"),
            margin_mode=_guess_margin_mode(acct),
            raw=dict(acct) if acct else {},
        )

    # ------------------------------------------------------------------
    # 工厂：从 OKX 账户/余额信息构建（占位，细节待实测补齐）
    # ------------------------------------------------------------------

    @classmethod
    def from_okx_account(cls, account_id: str, equity: Optional[float],
                         available: Optional[float],
                         unrealised_pnl: Optional[float],
                          exchange: str = "OKX",
                          display_name: str = "",
                          margin_mode: str = "unknown",
                          raw: Optional[dict] = None) -> "AccountInfo":
        """OKX 账户摘要 → AccountInfo（待实测对齐字段）。"""
        return cls(
            exchange=exchange,
            account_id=account_id,
            display_name=display_name,
            total_equity=equity,
            available=available,
            unrealised_pnl=unrealised_pnl,
            margin_mode=margin_mode,
            raw=(dict(raw) if raw else {}),
        )

    # ------------------------------------------------------------------
    # 工厂：从 Binance fapi/v2/account 构建（占位，细节待实测补齐）
    # ------------------------------------------------------------------

    @classmethod
    def from_binance_account(cls, account: dict, exchange: str = "BINANCE",
                             display_name: str = "") -> "AccountInfo":
        """Binance fapi/v2/account 返回 → AccountInfo（待实测对齐字段）。"""
        # Binance 账户摘要字段举例：totalWalletBalance, availableBalance,
        # totalUnrealizedProfit, ...  具体以实测为准。
        return cls(
            exchange=exchange,
            account_id=str(account.get("ownerName") or account.get("operator") or ""),
            display_name=display_name,
            total_equity=_num(account.get("totalWalletBalance")),
            available=_num(account.get("availableBalance")),
            unrealised_pnl=_num(account.get("totalUnrealizedProfit")),
            margin_mode="unknown",  # Binance 保证金模式需单独查询/记录
            raw=dict(account) if account else {},
        )

    # ------------------------------------------------------------------
    # 快照序列化（snapshot.json 向下兼容用的平坦 dict 视图）
    # ------------------------------------------------------------------

    def to_snapshot_dict(self) -> dict:
        """AccountInfo → 兼容 snapshot 的平坦 dict。

        字段尽量与旧代码从 snapshot.json 里读取 account 字典时的expect保持一致：
        risk_board.py / watch.py(maybe_daily_ledger) / account_safety(old path)。
        """
        return {
            "exchange": self.exchange,
            "account_id": self.account_id,
            "display_name": self.display_name,
            "total": self.total_equity,
            "available": self.available,
            "unrealised_pnl": self.unrealised_pnl,
            "maintenance_margin": self.maintenance_margin,
            "lever": self.leverage,
            "currency": self.currency,
            "margin_mode": self.margin_mode,
            "raw": self.raw,
        }

    def to_safety_dict(self) -> dict:
        """AccountInfo → 兼容 account_safety() 旧 dict 输入结构的最小视图。

        主要供未来将 account_safety 完全迁移到仅依赖模型对象时，
        内部回退/测试使用；业务路径現在已可直接传 AccountInfo。
        """
        return {
            "total": self.total_equity,
            "maintenance_margin": self.maintenance_margin,
            "isolated_position_margin": self.raw.get("isolated_position_margin"),
            "cross_margin_balance": self.raw.get("cross_margin_balance"),
            "margin_mode": self.margin_mode,
        }


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """
    单个合约持仓的标准描述。

    字段约定（尽量与现有 risk.position_panel 的键保持一致，方便迁移）：
      - contract       ：合约标识，如 'BTC_USDT'（系统内统一格式）
      - symbol         ：纯标的符号，如 'BTC'（从 contract 拆分或合约元信息获得）
      - side           ：'long' / 'short' / 'none'
      - size           ：合约张数（Gate size 单位是张；OKX/Binance 需换算）
      - amount         ：币数量（size × 面值，可以是小数）
      - leverage       ：杠杆倍数
      - entry_price    ：开仓价
      - mark_price     ：标记价
      - liq_price      ：强平价
      - margin         ：占用保证金
      - margin_mode    ：'全仓' / '逐仓' / '未知'
      - unrealised_pnl ：未实现盈亏
      - adl_ranking    ：ADL 排名（Gate 1-5；其他交易所可能无此字段 → None）
      - liq_distance_pct：强平距离%
      - roe_pct        ：未实现盈亏 / 初始保证金（%）
      - initial_margin ：初始保证金（用于 ROE 计算，None 表示用 margin 替代）
      - raw            ：原 API 返回 dict（调试/兼容用）
    """

    contract: str = ""
    symbol: str = ""
    side: str = "none"
    size: Optional[float] = None
    amount: Optional[float] = None
    leverage: Optional[float] = None
    entry_price: Optional[float] = None
    mark_price: Optional[float] = None
    liq_price: Optional[float] = None
    margin: Optional[float] = None
    margin_mode: str = "未知"
    unrealised_pnl: Optional[float] = None
    adl_ranking: Optional[int] = None
    liq_distance_pct: Optional[float] = None
    roe_pct: Optional[float] = None
    initial_margin: Optional[float] = None

    # 调试/兼容用；业务逻辑不应直接依赖 raw 的结构
    raw: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 工厂：从 Gate 持仓列表项构建（兼容现有 gate_client.get_positions() 返回）
    # ------------------------------------------------------------------

    @classmethod
    def from_gate_position(cls, p: dict | list, contract: Optional[str] = None,
                           mark_price: Optional[float] = None) -> "Position":
        """Gate list_positions 返回的一条持仓 dict/list → Position。

        若传入 list（例如 get_positions() 的原始列表项），取第一个元素构建模型；
        其他元素不在本工厂处理范围。
        """
        if isinstance(p, list):
            p = p[0] if p else {}
        p = p or {}
        contract = contract or str(p.get("contract") or "")
        side = _side_from_gate(p)
        size = _num(p.get("size"))
        mark = _num(p.get("mark_price")) or mark_price
        value = _num(p.get("value"))
        amount = (value / mark) if (mark and value) else size
        initial_margin = _num(p.get("initial_margin"))
        unrealised_pnl = _num(p.get("unrealised_pnl"))
        roe = (unrealised_pnl / initial_margin * 100.0) if initial_margin else None
        return cls(
            contract=contract,
            symbol=_contract_symbol(contract),
            side=side,
            size=size,
            amount=amount,
            leverage=_num(p.get("lever")) or _num(p.get("leverage")),
            entry_price=_num(p.get("entry_price")),
            mark_price=mark,
            liq_price=_num(p.get("liq_price")),
            margin=_num(p.get("margin")),
            margin_mode=_margin_mode_from_gate(p),
            unrealised_pnl=unrealised_pnl,
            adl_ranking=_num(p.get("adl_ranking")),
            initial_margin=initial_margin,
            roe_pct=roe,
            raw=dict(p) if p else {},
        )

    # ------------------------------------------------------------------
    # 业务方法（让模型自己算一部分，减少散落在 risk/wath 中的临时逻辑）
    # ------------------------------------------------------------------

    def liq_distance_pct_from(self, mark_price: Optional[float] = None,
                               side: Optional[str] = None) -> Optional[float]:
        """按本持仓的 side/强平价算出强平距离%。"""
        mp = mark_price or self.mark_price
        lp = self.liq_price
        sd = side or self.side
        if not mp or lp is None:
            return None
        if sd == "long":
            if lp >= mp:
                return None
            return (mp - lp) / mp * 100.0
        # short
        if lp <= mp:
            return None
        return (lp - mp) / mp * 100.0

    # ------------------------------------------------------------------
    # 快照序列化（snapshot.json 向下兼容用的平坦 dict 视图）
    # ------------------------------------------------------------------

    def to_snapshot_dict(self) -> dict:
        """Position → 兼容 snapshot 的平坦 dict（与 position_panel 键保持一致）。"""
        return {
            "contract": self.contract,
            "side": self.side,
            "size": self.size,
            "amount": self.amount,
            "leverage": self.leverage,
            "entry_price": self.entry_price,
            "mark_price": self.mark_price,
            "liq_price": self.liq_price,
            "margin": self.margin,
            "margin_mode": self.margin_mode,
            "unrealised_pnl": self.unrealised_pnl,
            "adl_ranking": self.adl_ranking,
            "liq_distance_pct": self.liq_distance_pct,
            "roe_pct": self.roe_pct,
            "initial_margin": self.initial_margin,
            "raw": self.raw,
        }

    def to_panel_dict(self) -> dict:
        """Position → 兼容 position_panel() 返回结构的 dict（计算量少的场合复用）。"""
        return {
            "contract": self.contract,
            "side": self.side,
            "size": self.size,
            "amount": self.amount,
            "leverage": self.leverage,
            "entry_price": self.entry_price,
            "mark_price": self.mark_price,
            "liq_price": self.liq_price,
            "margin": self.margin,
            "margin_mode": self.margin_mode,
            "unrealised_pnl": self.unrealised_pnl,
            "adl_ranking": self.adl_ranking,
            "liq_distance_pct": self.liq_distance_pct_from(),
            "roe_pct": self.roe_pct,
        }


# ---------------------------------------------------------------------------
# AlertEvent
# ---------------------------------------------------------------------------

@dataclass
class AlertEvent:
    """
    标准化告警包，供 BaseExchange / watch / 适配器统一喂给 AlertManager.fire(...)。

    字段：
      - severity : 'critical' / 'warning' / 'info'
      - exchange : 交易所标识
      - account  : 显示名（账户）
      - title    ：弹窗/日志标题
      - message  ：弹窗/日志正文
      - key      ：抑制/去重键（各交易所应确保同一语义事件 key 空间不冲突）
      - sound    ：可选，覆盖级别默认音效（如 'big_oi' / 'big_oi_trend' / None）
      - ts       ：事件发生时刻（UTC epoch 秒）
    """

    severity: str
    exchange: str
    account: str
    title: str
    message: str
    key: str = ""
    sound: Optional[str] = None
    ts: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())

    def to_fire_args(self) -> dict:
        """转换成 AlertManager.fire(level, title, msg, key=, sound=) 的参数。"""
        return {
            "level": self.severity,
            "title": self.title,
            "msg": self.message,
            "key": self.key or None,
            "sound": self.sound or None,
        }


# ===========================================================================
# 内部小工具（不导出为公开 API）
# ===========================================================================

def _guess_margin_mode(acct: dict) -> str:
    """从 Gate 账户 dict 猜测 margin_mode（isolated / cross / none）。"""
    iso = _num(acct.get("isolated_position_margin"))
    cross = _num(acct.get("cross_margin_balance"))
    if iso and iso > cross:
        return "isolated"
    if cross:
        return "cross"
    return "none"


def _side_from_gate(p: dict) -> str:
    """Gate 多仓/空仓判断：优先 mode 字段（dual long/short），否则由 size 正负判断。"""
    mode = (p.get("mode") or "").lower()
    if "long" in mode:
        return "long"
    if "short" in mode:
        return "short"
    size = _num(p.get("size"))
    return "long" if size and size > 0 else "short"


def _margin_mode_from_gate(p: dict) -> str:
    """Gate 持仓上的 margin_mode 中文映射（全仓 / 逐仓 / 未知）。"""
    mm = (p.get("pos_margin_mode") or p.get("mode") or "").lower()
    if mm.startswith("cross"):
        return "全仓"
    if "isolated" in mm:
        return "逐仓"
    return "未知"


def _contract_symbol(contract: str) -> str:
    """合约标识（如 'BTC_USDT'）→ 纯标的符号（如 'BTC'）。"""
    if not contract:
        return ""
    return contract.split("_")[0].split("-")[0]


# ===========================================================================
# 便捷：模型列表 ↔ 兼容 dict 列表（用于迁移阶段的适配器输出）
# ===========================================================================

def account_info_to_dict(ai: AccountInfo) -> dict:
    """AccountInfo → 平坦 dict（兼容旧代码直接用）。"""
    return {
        "exchange": ai.exchange,
        "account_id": ai.account_id,
        "display_name": ai.display_name,
        "total": ai.total_equity,
        "available": ai.available,
        "unrealised_pnl": ai.unrealised_pnl,
        "maintenance_margin": ai.maintenance_margin,
        "lever": ai.leverage,
        "currency": ai.currency,
        "margin_mode": ai.margin_mode,
        "raw": ai.raw,
    }


def position_to_dict(pos: Position) -> dict:
    """Position → 平坦 dict（键尽量接近现有 position_panel 的输出，方便片段迁移）。"""
    d = {
        "contract": pos.contract,
        "side": pos.side,
        "size": pos.size,
        "amount": pos.amount,
        "leverage": pos.leverage,
        "entry_price": pos.entry_price,
        "mark_price": pos.mark_price,
        "liq_price": pos.liq_price,
        "margin": pos.margin,
        "margin_mode": pos.margin_mode,
        "unrealised_pnl": pos.unrealised_pnl,
        "adl_ranking": pos.adl_ranking,
        "liq_distance_pct": pos.liq_distance_pct,
        "roe_pct": pos.roe_pct,
        "initial_margin": pos.initial_margin,
        "raw": pos.raw,
    }
    return d
