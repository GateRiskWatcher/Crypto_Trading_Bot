#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ledger.py — 第3层 复盘：每日盈亏账 / 回撤 / 强平历史

数据来源（只读）：
  - get_income(contract): 资金费、手续费、已实现盈亏(pnl) 等收支记录
  - get_my_trades(contract): 成交流水（算胜率/持仓时长）
落盘到 data/daily_ledger.json（每日一份）与 data/equity_curve.json（权益曲线）。
"""
import os
import json
import time
from datetime import datetime, timezone
from typing import Any


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "data")


def _num(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def ensure_dirs():
    os.makedirs(os.path.join(DATA_DIR, "ledger"), exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "equity"), exist_ok=True)


def summarize_income(income_list: list) -> dict:
    """
    按 income 记录的 type 汇总：
      pnl(已实现), fund(资金费), fee(手续费), dnw(出入金), refr(返佣)...
    Gate account_book 金额字段名为 `change`（非 amount）。
    """
    out: dict[str, float] = {}
    for r in income_list:
        t = r.get("type", "other")
        out[t] = out.get(t, 0.0) + _num(r.get("change"))
    return out


def daily_ledger(account_name: str, day_key: str, income_list: list, equity: float) -> dict:
    """生成/追加某账户某日账本，并附带当前权益曲线最大回撤。"""
    ensure_dirs()
    path = os.path.join(DATA_DIR, "ledger", f"{account_name}_{day_key}.json")
    summary = summarize_income(income_list)
    rec = {
        "account": account_name,
        "day": day_key,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "equity": equity,
        "income_by_type": summary,
        "realized_pnl": summary.get("pnl", 0.0),
        "funding_paid": summary.get("fund", 0.0),
        "fee_paid": summary.get("fee", 0.0),
        "max_drawdown_pct": max_drawdown(account_name),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2, ensure_ascii=False)
    return rec


def append_equity(account_name: str, equity: float, unrealised: float):
    """追加权益曲线点（用于回撤计算）。"""
    ensure_dirs()
    path = os.path.join(DATA_DIR, "equity", f"{account_name}.json")
    arr: list = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                arr = json.load(f)
        except Exception:
            arr = []
    arr.append({
        "ts": int(time.time() * 1000),
        "equity": equity,
        "unrealised": unrealised,
    })
    # 只保留最近 5000 点，避免无限增长
    arr = arr[-5000:]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(arr, f, ensure_ascii=False)


def max_drawdown(account_name: str) -> float:
    """读权益曲线算最大回撤（百分比）。"""
    path = os.path.join(DATA_DIR, "equity", f"{account_name}.json")
    if not os.path.exists(path):
        return 0.0
    try:
        with open(path, "r", encoding="utf-8") as f:
            arr = json.load(f)
    except Exception:
        return 0.0
    eqs = [r["equity"] for r in arr if r.get("equity")]
    if len(eqs) < 2:
        return 0.0
    peak = eqs[0]
    mdd = 0.0
    for e in eqs:
        if e > peak:
            peak = e
        if peak > 0:
            dd = (peak - e) / peak * 100.0
            mdd = max(mdd, dd)
    return mdd
