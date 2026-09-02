#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
risk.py — 风险指标计算（纯函数，不触网络）

覆盖你要求的 4 层布局：
  第1层(保命): 强平距离 / 保证金率 / 资金费倒计时+方向 / 全仓抗跌 / ADL排名
  第2层(态势): 统一持仓面板 / 标记价-指数价背离 / OI 突增
  第3层(复盘): 每日盈亏账 / 回撤（数据在 ledger.py）
  第4层(决策): 情景模拟（给定跌幅算强平价/亏损/保证金率）

所有输入来自 gate_client 的快照字段（Gate 直接给 liq_price，几乎零公式风险）。
账户模型（已按真实账户校准）：逐仓(isolated)+dual 模式为主，全仓字段多为 0，
资金在 isolated_position_margin；账户级安全垫用 total / maintenance_margin。
"""
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from models import position_to_dict


def _num(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ============ 第1层 ============

def liq_distance_pct(mark_price: float, liq_price: float, side: str) -> Optional[float]:
    """
    强平距离（百分比）。多仓: (mark-liq)/mark；空仓: (liq-mark)/mark。
    side: 'long' / 'short'（由 mode 字段判断，见 position_panel）。
    返回 None 表示无强平价。
    """
    if not mark_price or not liq_price:
        return None
    if side == "long":
        if liq_price >= mark_price:
            return 999999.0
        return (mark_price - liq_price) / mark_price * 100.0
    else:
        if liq_price <= mark_price:
            return 999999.0
        return (liq_price - mark_price) / mark_price * 100.0


def account_safety(acct: Any) -> dict:
    """
    账户级安全垫（逐仓/全仓通用）。

    输入可为：
      - Gate 原始 dict（list_futures_accounts 返回的单账户 dict，含 total /
        maintenance_margin / isolated_position_margin / cross_margin_balance 等）。
      - AccountInfo 模型对象（from_gate_account / from_okx_account / from_binance_account
        构建而来）。若传入模型对象，函数会优先从其字段读取，并在字段缺失时回退到
        raw dict（若有）中的同名键。

    Gate 返回单 dict：total=总保证金(含逐仓+全仓)，maintenance_margin=总维持保证金。
    逐仓模式下 cross_* 多为 0，资金在 isolated_position_margin；此时用 total/maintenance_margin。
    """
    from models import AccountInfo as _AI

    if isinstance(acct, _AI):
        # 优先从模型字段取值（数值字段已统一为 float/None），缺失则回退 raw
        raw = acct.raw if isinstance(acct.raw, dict) else {}
        total = acct.total_equity if acct.total_equity is not None else _num(raw.get("total"))
        mm = acct.maintenance_margin if acct.maintenance_margin is not None else _num(raw.get("maintenance_margin"))
        iso_margin = _num(raw.get("isolated_position_margin"))  # 模型层未显式保留此字段，回退 raw
        cross_mb = _num(raw.get("cross_margin_balance"))
    else:
        total = _num(acct.get("total"))
        mm = _num(acct.get("maintenance_margin"))
        iso_margin = _num(acct.get("isolated_position_margin"))
        cross_mb = _num(acct.get("cross_margin_balance"))
    avail_rate = (total - mm) / total * 100.0 if total else None
    return {
        "total": total,
        "maintenance_margin": mm,
        "isolated_position_margin": iso_margin,
        "cross_margin_balance": cross_mb,
        "available_margin_rate_pct": avail_rate,
        # 判别：逐仓保证金明显大于 cross 余额则视为 isolated；否则 cross/none
        "margin_mode": "isolated" if iso_margin > cross_mb else ("cross" if cross_mb else "none"),
    }


def funding_countdown(contract_ticker: dict) -> dict:
    """
    资金费倒计时 + 方向。
    Gate USDT 永续每 8 小时结算一次，固定为 UTC 00:00 / 08:00 / 16:00。
    ticker 不含 funding_time 字段，按当前 UTC 时间推算下次结算点。
    方向：funding_rate > 0 多方付空方（多仓是付方）；<0 反之。
    """
    rate = _num(contract_ticker.get("funding_rate"))
    now = datetime.now(timezone.utc)
    candidates = []
    for h in (0, 8, 16):
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if t <= now:
            t = t + (timedelta(days=1) if h == 16 else timedelta(hours=8))
        candidates.append(t)
    future = [c for c in candidates if c > now]
    settle = min(future) if future else candidates[0]
    remain = (settle - now).total_seconds()
    payer = "多仓(你付)" if rate > 0 else ("空仓(你付)" if rate < 0 else "中性")
    return {
        "rate": rate,
        "settle_in_seconds": remain,
        "settle_in_minutes": remain / 60.0,
        "next_settle_utc": settle.strftime("%Y-%m-%d %H:%M UTC"),
        "payer": payer,
    }


def adl_risk(rank: int) -> str:
    """
    ADL 自动减仓排队名次：Gate API 返回 1-5 整数档位。
    ⚠️ 口径：该档位与 App 的「5 格亮灯」是两套口径，数值越小越靠前排、极端行情越优先被
    自动减仓，（实测 API=4 时 App 仅亮 2 格）。本函数仅做信息展示，不触发告警。
    """
    rank = int(rank) if rank is not None else 0
    if rank >= 5:
        return "档位5(最后排; 极端行情自动减仓概率极低)"
    if rank == 4:
        return "档位4(后排; 极端行情自动减仓概率低)"
    if rank == 3:
        return "档位3(中排，极端行情自动减仓概率中等)"
    if rank == 2:
        return "档位2(前排靠后，极端行情自动减仓概率高)"
    if rank == 1:
        return "档位1(最前排，极端行情自动减仓概率极高)"
    return "档位0(最靠后)"


# ============ 第2层 ============

def mark_index_deviation(mark_price: float, index_price: float) -> Optional[float]:
    """标记价相对指数价偏离百分比。大偏离=插针/操纵前兆。"""
    if not index_price:
        return None
    return (mark_price - index_price) / index_price * 100.0


def position_panel(positions: list, tickers: Optional[dict] = None,
                   compute_liq_distance: bool = True) -> list:
    """
    统一持仓面板：每个仓位净敞口/未实现盈亏/占用保证金/强平距离，一次看全。

    输入：
      - positions: 可为 Gate 原始 dict 列表（get_positions() 返回），或 Position
        模型对象列表。混用也支持；函数内部会自动将 Position 对象转为兼容 dict。
      - tickers: 可选的 {contract: ticker_dict}，主要用于外部代码复用；本函数内部
        目前未直接用它（保持签名兼容）。若传入非 dict/None 则忽略。
      - compute_liq_distance: 是否计算 liq_distance_pct（默认 True）。设 False 时
        对应字段留 None，适合不关心强平距离的快速面板。

    Gate 永续支持 dual 模式：side 由 mode 字段决定（dual_long/dual_short/single），
    不能只看 size 正负（dual 下 size 恒为正）。
    """
    from models import Position as _Position

    rows = []
    for p in positions:
        # Position 模型对象 → 兼容 dict（保留向下兼容，避免重复写展开逻辑）
        if isinstance(p, _Position):
            p = position_to_dict(p)
        if not isinstance(p, dict):
            continue
        if _num(p.get("size")) == 0:
            continue
        size = _num(p.get("size"))
        mark = _num(p.get("mark_price"))
        liq = _num(p.get("liq_price"))
        mode = (p.get("mode") or "").lower()
        if "long" in mode:
            side = "long"
        elif "short" in mode:
            side = "short"
        else:
            side = "long" if size > 0 else "short"
        mmode_raw = (p.get("pos_margin_mode") or mode or "").lower()
        margin_mode = "全仓" if mmode_raw.startswith("cross") else (
            "逐仓" if "isolated" in mmode_raw else (p.get("pos_margin_mode") or mode))
        # 数量换算成「币数」：Gate size 单位是合约张数，币数 = 名义价值 value / 标记价
        # （通用算法，避免硬编码各币种面值表）
        amount = (_num(p.get("value")) / mark) if (mark and _num(p.get("value"))) else size
        im = _num(p.get("initial_margin"))
        upnl = _num(p.get("unrealised_pnl"))
        roe = (upnl / im * 100.0) if im else None  # 收益率 = 未实现盈亏 / 初始保证金
        ld = None
        if compute_liq_distance:
            ld = liq_distance_pct(mark, liq, side)
        rows.append({
            "contract": p.get("contract"),
            "side": side,
            "size": size,                      # 合约张数
            "amount": amount,                 # 币数量（size×面值）
            "leverage": _num(p.get("lever")) or _num(p.get("leverage")),  # 优先 lever（dual 下 leverage 常为0）
            "entry_price": _num(p.get("entry_price")),
            "mark_price": mark,
            "liq_price": liq,
            "margin": _num(p.get("margin")),
            "margin_mode": margin_mode,       # 全仓 / 逐仓（中文）
            "unrealised_pnl": upnl,
            "adl_ranking": p.get("adl_ranking"),
            "liq_distance_pct": ld,
            "roe_pct": roe,                   # 收益率（ROE）
        })
    return rows


# ============ 第3层 ============

def oi_change_pct(prev_oi: float, cur_oi: float) -> Optional[float]:
    if not prev_oi:
        return None
    return (cur_oi - prev_oi) / prev_oi * 100.0


def oi_trend_pct(baseline_oi: float, cur_oi: float) -> Optional[float]:
    """
    趋势层 OI 变化：当前 OI 相对「基线 OI」（如 N 分钟前落盘的本地快照）的累计变化百分比。
    用于捕捉趋势性建仓/平仓（急涨急跌），与 oi_change_pct（20s 瞬时环比）互补。
    纯函数，不触网络；baseline 由 watch 从本地 oi_baseline.json 读取。
    """
    if not baseline_oi:
        return None
    return (cur_oi - baseline_oi) / baseline_oi * 100.0


# ============ 第4层：情景模拟 ============

def scenario(mark_price: float, size: float, entry_price: float,
             new_drop_pct: float) -> dict:
    """
    给定标的价格下跌 new_drop_pct% 后的情景推演（数学，不改任何东西）。
    size>0 视为多仓。
    """
    new_price = mark_price * (1 - new_drop_pct / 100.0)
    if size > 0:
        new_pnl = (new_price - entry_price) * size
        base_pnl = (mark_price - entry_price) * size
    else:
        new_pnl = (entry_price - new_price) * abs(size)
        base_pnl = (entry_price - mark_price) * abs(size)
    return {
        "drop_pct": new_drop_pct,
        "new_mark_price": new_price,
        "new_unrealised_pnl": new_pnl,
        "pnl_change": new_pnl - base_pnl,
    }


def scenario_full(mark_price: float, size: float, entry_price: float,
                  leverage: float, initial_margin: float,
                  maintenance_rate: float, side: str,
                  new_drop_pct: float) -> dict:
    """
    情景模拟（完整版）：给定标的价格下跌 new_drop_pct% 后，推演
    新标记价 / 新未实现盈亏 / 新强平价 / 新 ROE / 新保证金率。
    纯数学，不改任何东西；支持多空（side='long'/'short'）。

    强平价模型（Gate USDT 永续，逐仓近似）：
      多仓: liq = entry * (1 - 1/leverage + mmr)
      空仓: liq = entry * (1 + 1/leverage - mmr)
    其中 mmr = maintenance_rate（维持保证金率，如 0.008333）。
    该近似与 Gate 实际 liq_price 通常误差 <1%，足够哨兵推演。

    注：强平价不随价格移动而移动（由开仓价/杠杆/维持率决定），
    但「强平距离」和「保证金率」会随价格变动而变动，这里一并算出。
    """
    mmr = maintenance_rate if maintenance_rate else 0.0
    lev = leverage if leverage else 1.0
    new_price = mark_price * (1 - new_drop_pct / 100.0)

    # 开仓价方向的强平价（不随现价移动）
    if side == "short":
        liq_price = entry_price * (1 + 1.0 / lev - mmr)
    else:
        liq_price = entry_price * (1 - 1.0 / lev + mmr)

    # 名义价值与盈亏
    notion = abs(size) * (new_price if new_price else mark_price)
    if size > 0 or side == "long":
        new_pnl = (new_price - entry_price) * abs(size)
        base_pnl = (mark_price - entry_price) * abs(size)
    else:
        new_pnl = (entry_price - new_price) * abs(size)
        base_pnl = (entry_price - mark_price) * abs(size)

    im = initial_margin if initial_margin else 0.0
    roe = (new_pnl / im * 100.0) if im else None

    # 新保证金率（持仓保证金 / 名义价值，逐仓近似）
    new_margin_rate = (im / notion * 100.0) if notion else None
    margin_call = (new_margin_rate is not None and new_margin_rate <= mmr * 100.0 + 0.01)

    # 当前（未跌）强平距离
    cur_liq_dist = liq_distance_pct(mark_price, liq_price, side if side else ("long" if size > 0 else "short"))
    new_liq_dist = liq_distance_pct(new_price, liq_price, side if side else ("long" if size > 0 else "short"))

    return {
        "drop_pct": new_drop_pct,
        "side": side,
        "new_mark_price": round(new_price, 8),
        "liq_price": round(liq_price, 8),
        "liq_distance_now_pct": round(cur_liq_dist, 3) if cur_liq_dist is not None else None,
        "liq_distance_after_pct": round(new_liq_dist, 3) if new_liq_dist is not None else None,
        "new_unrealised_pnl": round(new_pnl, 6),
        "pnl_change": round(new_pnl - base_pnl, 6),
        "new_roe_pct": round(roe, 2) if roe is not None else None,
        "new_margin_rate_pct": round(new_margin_rate, 2) if new_margin_rate is not None else None,
        "maintenance_rate_pct": round(mmr * 100.0, 4),
        "margin_call": margin_call,
    }


def funding_rate_abnormal(rate: float, threshold_pct: float = 0.05,
                          price: Optional[float] = None,
                          change24h: Optional[float] = None) -> Optional[dict]:
    """
    资金费率绝对值异常检测。
    Gate funding_rate 量纲为比例（0.0001 = 0.01%），需先 ×100 得百分比。
    绝对费率过高（多空一方极端拥挤）往往是插针/反转前兆。

    price / change24h 可选：传入则在提示文本里带上当前价与 24h 涨跌幅，
    值为 None 时不追加（保持向后兼容）。
    返回 None 表示未超阈值；否则返回 {abs_pct, side, text, price, change24h}。
    """
    rate_pct = abs(rate) * 100.0
    if rate_pct < threshold_pct:
        return None
    side = "正费率(多付空)" if rate > 0 else "负费率(空付多)"
    text = f"资金费率 {rate_pct:.3f}% 异常偏高（{side}），多空一方极端拥挤，警惕反转/插针"
    if price is not None:
        try:
            text += f"，当前价 {float(price):.4f}"
        except (TypeError, ValueError):
            pass
    if change24h is not None:
        try:
            text += f"，24h涨跌 {float(change24h):+.2f}%"
        except (TypeError, ValueError):
            pass
    return {"abs_pct": round(rate_pct, 3), "side": side, "text": text,
            "price": price, "change24h": change24h}
