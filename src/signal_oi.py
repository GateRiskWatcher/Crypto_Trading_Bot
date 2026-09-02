#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signal_oi.py — OI(未平仓量) × Price(价格) 语义化信号识别（纯函数，不触网络）

工作原理：
    不再孤立地看待数据，而是对比「当前采样」与「上一次采样」的 OI 与 Price
    变化率，识别四种具有实战意义的语义信号：

      · 去杠杆 Deleveraging : OI↓ 且 Price↓  —— 多头平仓离场
      · 空头踩踏 Short Squeeze: OI↓ 且 Price↑ —— 空头被迫止损，推涨
      · 资金流入 Inflow      : OI↑ 且 Price↑  —— 多方主动入场，看涨
      · 资金压制 Pressure    : OI↑ 且 Price↓  —— 空方主动入场，看跌

    当 OI 与/或 Price 的变化低于各自阈值时，判定为「中性」（不发声）。

注意：Gate ticker 的 `change_percentage` 是 24h 涨跌，并非环比；本模块
要求调用方传入「上一帧」与「当前帧」的 total_size / mark_price，自行做环比。
"""

from typing import Any, Optional


def _num(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# 四分类语义表
_SIGNALS = {
    "deleveraging": {
        "zh": "去杠杆",
        "en": "Deleveraging",
        "desc": "多头平仓离场，OI 与价格同步下行",
        "stance": "偏空 / 流动性撤退",
    },
    "short_squeeze": {
        "zh": "空头踩踏",
        "en": "Short Squeeze",
        "desc": "空头被迫止损，OI 降而价格反拉",
        "stance": "短线逼空 / 警惕回补延续",
    },
    "inflow": {
        "zh": "资金流入",
        "en": "Inflow",
        "desc": "多方主动开仓，OI 与价格同步上行",
        "stance": "趋势偏强 / 看涨",
    },
    "pressure": {
        "zh": "资金压制",
        "en": "Pressure",
        "desc": "空方主动开仓，OI 升而价格承压",
        "stance": "趋势偏弱 / 看跌",
    },
    "neutral": {
        "zh": "中性",
        "en": "Neutral",
        "desc": "OI 或价格变化未达阈值，暂无明显语义",
        "stance": "观望",
    },
}


def oi_price_signal(
    prev_oi: float,
    cur_oi: float,
    prev_price: float,
    cur_price: float,
    min_oi_change_pct: float = 5.0,
    min_price_change_pct: float = 0.3,
    cur_price_abs: Optional[float] = None,
    change_24h_pct: Optional[float] = None,
) -> dict:
    """
    对比两帧 OI 与价格，输出语义信号。

    参数：
        prev_oi / cur_oi      : 上一帧 / 当前帧 未平仓量（total_size，合约张数）
        prev_price/cur_price  : 上一帧 / 当前帧 价格（mark_price，用于环比）
        min_oi_change_pct     : OI 变化绝对值低于该值则视为「未变」
        min_price_change_pct  : 价格变化绝对值低于该值则视为「未变」
        cur_price_abs         : 当前绝对价格（展示用，如 0.0895）
        change_24h_pct        : 24h 涨跌幅（ticker.change_percentage，展示用）

    返回 dict（新增 cur_price / change_24h 字段供消息展示）。
    """
    # OI 环比（prev 为 0 时无法计算，归为中性）
    if prev_oi and prev_oi > 0:
        oi_chg = (cur_oi - prev_oi) / prev_oi * 100.0
    else:
        oi_chg = 0.0
    # 价格环比
    if prev_price and prev_price > 0:
        px_chg = (cur_price - prev_price) / prev_price * 100.0
    else:
        px_chg = 0.0

    oi_moved = abs(oi_chg) >= min_oi_change_pct
    px_moved = abs(px_chg) >= min_price_change_pct

    if not oi_moved and not px_moved:
        sig = "neutral"
    elif oi_moved and px_moved:
        if oi_chg > 0 and px_chg > 0:
            sig = "inflow"
        elif oi_chg > 0 and px_chg < 0:
            sig = "pressure"
        elif oi_chg < 0 and px_chg < 0:
            sig = "deleveraging"
        else:  # oi_chg<0, px_chg>0
            sig = "short_squeeze"
    else:
        # 只有一侧达到阈值：信息不足，视为中性（避免误读单一维度）
        sig = "neutral"

    meta = _SIGNALS[sig]
    arrow = lambda v: "↑" if v > 0 else ("↓" if v < 0 else "→")
    text = (
        f"OI{arrow(oi_chg)}{abs(oi_chg):.1f}% 且 价格{arrow(px_chg)}{abs(px_chg):.2f}%"
        f" —— {meta['desc']}"
    )
    # 当前价 / 24h 涨跌展示串（有值才追加）
    extra = ""
    if cur_price_abs is not None:
        extra += f" 当前价 {cur_price_abs}"
    if change_24h_pct is not None:
        extra += f" | 24h涨跌 {change_24h_pct:+.2f}%"
    return {
        "signal": sig,
        "zh": meta["zh"],
        "en": meta["en"],
        "desc": meta["desc"],
        "stance": meta["stance"],
        "oi_change_pct": round(oi_chg, 2),
        "price_change_pct": round(px_chg, 2),
        "arrow_oi": arrow(oi_chg),
        "arrow_price": arrow(px_chg),
        "cur_price": cur_price_abs,
        "change_24h_pct": change_24h_pct,
        "text": text,
        "extra": extra,
    }


def interpret_to_alert(contract: str, sig: dict, name: str) -> Optional[tuple]:
    """
    把语义信号转成告警三元组 (title, msg, key)；中性返回 None。
    供 watch.market_loop 直接 fire 使用。
    """
    if sig["signal"] == "neutral":
        return None
    title = f"[{name}] {sig['zh']} {contract}"  # 去掉原冗余的「 | {en}｜{contract}」，缩短 title 以缓解 toast 截断
    msg_lines = []
    extra = sig.get("extra")
    if extra:
        msg_lines.append(extra.strip())
    msg_lines.append(sig["text"])
    msg_lines.append(f"信号倾向：{sig['stance']}")
    msg = "\n".join(msg_lines)
    key = f"oi-sig:{name}:{contract}:{sig['signal']}"
    return title, msg, key
