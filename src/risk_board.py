#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
risk_board.py — 中文风险看板（一次性打印）

用法: .venv\\Scripts\\python.exe src\\risk_board.py
读取真实账户 + 本地历史账本，打印人话版风险快照：
  - 合约账户总览（实时读 API）
  - 周期盈亏（今日 / 7 / 30 / 90 / 180 天，读本地 data/ledger/，不额外调 API）
  - 每个仓位强平还差几点、资金费倒计时、保证金率、ADL 档位
不监控、不报警，只打印当前状态。适合你录完 key 后直观确认系统工作正常。

数据来源：
  - 实时合约总览 / 持仓：走 gate_client 只读 GET（与常驻 watch.py 同一套）。
  - 周期盈亏：从 data/ledger/<账户>_YYYY-MM-DD.json 本地聚合，零额外 API。
"""

import os
import sys
import json
import glob
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import yaml
from keystore import load_accounts, describe
from gate_client import MultiAccountWatcher
from risk import (liq_distance_pct, funding_countdown,
                  position_panel, mark_index_deviation, oi_change_pct,
                  account_safety, adl_risk)
from alert import AlertManager
import ledger as ledger_mod

def _NUM(x):
    # Gate 返回的数值字段多为字符串，必须真正解析（原 lambda 只对 int/float 有效，
    # 导致总权益/可用/未实现盈亏全部显示 0.0000 的历史 bug）
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

# 周期盈亏统计窗口（天）
PERIOD_DAYS = [1, 7, 30, 90, 180]


def _period_pnl(account_name: str, days: int) -> dict:
    """读本地 data/ledger/ 聚合最近 N 天累计盈亏（零 API）。
    返回 {n_days, pnl, fee, fund, dnw, net, realized, counted_days}。
    net = 已实现盈亏 + 资金费 + 手续费（不含出入金 dnw）。
    """
    today = datetime.now().date()
    cutoff = today - timedelta(days=days - 1)
    ledger_dir = os.path.join(ROOT, "data", "ledger")
    total_pnl = total_fee = total_fund = total_dnw = 0.0
    counted = 0
    if os.path.isdir(ledger_dir):
        pat = os.path.join(ledger_dir, f"{account_name}_*.json")
        for fp in glob.glob(pat):
            bn = os.path.basename(fp)
            day_str = bn[len(f"{account_name}_"):-5]  # YYYY-MM-DD
            try:
                d = datetime.strptime(day_str, "%Y-%m-%d").date()
            except Exception:
                continue
            if cutoff <= d <= today:
                try:
                    rec = json.load(open(fp, encoding="utf-8"))
                except Exception:
                    continue
                total_pnl += _NUM(rec.get("realized_pnl"))
                total_fee += _NUM(rec.get("fee_paid"))
                total_fund += _NUM(rec.get("funding_paid"))
                total_dnw += _NUM(rec.get("income_by_type", {}).get("dnw", 0.0))
                counted += 1
    net = total_pnl + total_fund + total_fee  # 手续费为负，直接相加
    return {
        "n_days": days,
        "pnl": total_pnl,
        "fee": total_fee,
        "fund": total_fund,
        "dnw": total_dnw,
        "net": net,
        "counted_days": counted,
    }


def _print_period_block(account_name: str):
    print("\n  ▸ 周期盈亏（读本地账本，零 API）")
    for d in PERIOD_DAYS:
        r = _period_pnl(account_name, d)
        if r["counted_days"] == 0:
            print(f"    {('今日' if d == 1 else str(d) + '天'):>4}：暂无账本数据")
            continue
        net = r["net"]
        sign = "+" if net >= 0 else ""
        label = "今日" if d == 1 else f"{d}天"
        print(f"    {label:>4}：累计净额 {sign}{net:.4f} USDT "
              f"（已实现 {r['pnl']:+.4f} / 资金费 {r['fund']:+.4f} / 手续费 {r['fee']:+.4f}）"
              f"  样本 {r['counted_days']} 天")


def main():
    settings = yaml.safe_load(open(os.path.join(ROOT, "config", "settings.yaml"), encoding="utf-8"))
    accounts = load_accounts(settings)
    if not accounts:
        print("未找到 key，先运行 enter_keys.bat 录入。"); return
    watcher = MultiAccountWatcher(accounts)
    alert = AlertManager(settings)

    # 并发：后台线程跑账本补齐（带红灯进度动画），主线程同时拉 snapshot 出看板
    import threading
    backfill_state = {"running": True, "done": False, "name": None,
                     "day": None, "pulled": 0, "planned": 0, "error": None}
    backfill_progress = {}  # name -> progress report (from maybe_daily_ledger)

    def _on_day(name, day, pulled, per_run, planned):
        backfill_state.update(name=name, day=day.isoformat(), pulled=pulled, planned=planned)

    def _run_backfill():
        try:
            import watch
            nonlocal backfill_progress
            backfill_progress = watch.maybe_daily_ledger(watcher, settings, snap, on_day=_on_day)
        except Exception as e:
            backfill_state["error"] = str(e)
        finally:
            backfill_state["running"] = False
            backfill_state["done"] = True

    # 先拉 snapshot（看板实时数据），与补齐并行
    snap = watcher.snapshot_all()
    bt = threading.Thread(target=_run_backfill, daemon=True)
    bt.start()

    print("=" * 64)
    print("GateRiskWatcher 风险看板  (只读)")
    print("=" * 64)

    # 红灯进度动画（补齐进行中）
    import time as _time
    while backfill_state["running"]:
        st = backfill_state
        if st["planned"]:
            filled = int(st["pulled"] / st["planned"] * 10)
            bar = "█" * filled + "░" * (10 - filled)
            dayinfo = f" {st['day']}" if st["day"] else ""
            print(f"\r  [🔴 账本补齐中] {bar} {st['pulled']}/{st['planned']} 天{dayinfo}", end="", flush=True)
        else:
            print(f"\r  [🔴 账本补齐中] 初始化…", end="", flush=True)
        _time.sleep(0.3)
    print()  # 换行结束动画行

    # 绿灯：补齐结果
    if backfill_state.get("error"):
        print(f"  [🟢 账本补齐异常(不影响看板)] {backfill_state['error']}")
    else:
        for nm, p in (backfill_progress or {}).items():
            if p.get("done") and p.get("pulled", 0) == 0:
                print(f"  [🟢 账本补齐完成] [{nm}] 已追平到昨天，无需补齐 ✓")
            else:
                rem = p.get("remaining", 0)
                tail = "已追平 ✓" if p.get("done") else f"剩 {rem} 天（下次开机/运行续补）"
                print(f"  [🟢 账本补齐完成] [{nm}] 本次 +{p.get('pulled',0)} 天 "
                      f"({p.get('from')}→{p.get('to')})，目标 {p.get('target')}：{tail}")

    # 实时账户总览 + 持仓
    for name, a in snap["accounts"].items():
        print(f"\n【账户】{name}")
        if not a.get("ok"):
            print("  ✗ 拉取失败:", a.get("error")); continue
        u = a["account"]
        saf = account_safety(u)
        print(f"  账户模式: {saf['margin_mode']} | 总权益: {_NUM(u.get('total')):.4f} USDT")
        print(f"  可用保证金: {_NUM(u.get('available')):.4f} | 未实现盈亏: {_NUM(u.get('unrealised_pnl')):+.4f}")
        print(f"  保证金率: {saf['available_margin_rate_pct']:.2f}% (维持 {saf['maintenance_margin']:.4f})")
        mr_warn = _NUM(settings.get("risk", {}).get("margin_ratio_warning_pct", 30))
        if saf["available_margin_rate_pct"] is not None:
            flag = "⚠ 偏低" if saf["available_margin_rate_pct"] < mr_warn else "✓"
            print(f"  账户安全垫: {flag}")
        try:
            mdd = ledger_mod.max_drawdown(name)
            print(f"  权益曲线最大回撤: {mdd:.2f}%")
        except Exception:
            pass

        # A2: 周期盈亏（读本地账本，补齐线程已完成）
        _print_period_block(name)

        panel = position_panel(a["positions"])
        tickers = a["tickers"]
        if not panel:
            print("  当前无持仓")
        for r in panel:
            ld = r["liq_distance_pct"]
            coin = (r['contract']).split("_")[0]
            amt = r.get("amount")
            amt_s = f"{amt:.4f} {coin}" if amt is not None else f"{r['size']}张"
            roe = r.get("roe_pct")
            roe_s = f"{roe:+.2f}%" if roe is not None else "N/A"
            print(f"\n  ▸ {r['contract']}  {r['side']} · {r.get('margin_mode','')}  杠杆 {r['leverage']}x")
            print(f"    仓位: {amt_s} (={r['size']}张)  开仓价 {r['entry_price']}  标记价 {r['mark_price']}")
            print(f"    强平价 {r['liq_price']}  未实现 {r['unrealised_pnl']:+.4f}  ROE {roe_s}")
            if ld is not None:
                bar = "█" * max(1, int(ld)) if ld < 50 else "█" * 50
                danger = "🔴 临界" if ld < _NUM(settings.get("risk", {}).get("liq_distance_critical_pct", 2)) \
                    else ("🟠 接近" if ld < _NUM(settings.get("risk", {}).get("liq_distance_warning_pct", 5)) else "🟢 安全")
                print(f"    强平距离: {ld:.2f}%  {bar}  {danger}")
            else:
                print(f"    强平距离: 无 (保证金充足)  ██████████  🟢 安全")
            tk = tickers.get(r["contract"])
            if tk:
                dev = mark_index_deviation(r["mark_price"], _NUM(tk.get("index_price")))
                if dev is not None:
                    print(f"    标记-指数偏离: {dev:+.3f}%")
                fc = funding_countdown(tk)
                print(f"    资金费: {fc['rate']:.6f}  下次结算 {fc['next_settle_utc']} ({fc['payer']})")
            adl = r.get("adl_ranking")
            if adl is not None:
                # ADL指示灯：根据 rank_division (1-6) 换算亮灯数
                # 公式：亮灯数 = 6 − rank_division (rank_division 1～5)
                # rank_division 越小 越危险，亮灯越多越危险（与原刻度相反）
                # 交易所App 5格色序：前3个绿，第4个黄，第五个红
                rank_div = adl if adl <= 5 else 5
                if rank_div < 1:
                    rank_div = 1
                lights_count = 6 - rank_div
                green = "🟢" * min(lights_count, 3)
                yellow = "🟡" if lights_count >= 4 else ""
                red = "🔴" if lights_count >= 5 else ""
                lights = green + yellow + red
                print(f"    ADL 自动减仓名次: {rank_div}")
                print(f"    {lights}")
    print("\n" + "=" * 64)
    print("只读模式：本程序不会下单。报警/铃音请在 run.bat 常驻时生效。")
    print("=" * 64)


if __name__ == "__main__":
    main()
