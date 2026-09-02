#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watch.py — GateRiskWatcher 主循环（只读哨兵）

把 4 层串起来：
  L1 保命: 强平距离 / 保证金率 / ADL 排名           -> account_loop
  L2 态势: 资金费倒计时 / 标记-指数背离 / OI 突增    -> market_loop
  L3 复盘: 每日盈亏账 / 回撤（落 data/，由 snapshot_loop 日终触发）
  L4 决策: 情景模拟（data/snapshot.json 落盘，供 Hermes Bot 读）

三个线程（各自独立限流退避）：
  - account_loop (poll_account_seconds): 资金/持仓/强平/保证金率/ADL + 权益曲线
  - market_loop  (poll_market_seconds): ticker 资金费/背离/OI
  - snapshot_loop (5s): 汇总落盘 data/snapshot.json + 日终生成账本

安全：只用 gate_client 的 GET；任何写操作不存在于代码路径。

模型集成（Roadmap 第6条）：gate_client 新增 get_account_model / get_positions_models，
risk.py 的 account_safety / position_panel 已兼容 AccountInfo / Position 对象。
默认关闭模型热路径（use_models=False），启用前需确认下游打印/看板无副作用。
"""
import os
import sys
import time
import json
import glob
import signal
import threading
import argparse
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import yaml
from keystore import load_accounts, describe
from gate_client import MultiAccountWatcher, RateLimitError
from risk import (liq_distance_pct, funding_countdown,
                  mark_index_deviation, position_panel, oi_change_pct,
                  account_safety, adl_risk, funding_rate_abnormal, scenario_full,
                  oi_trend_pct)
from signal_oi import oi_price_signal, interpret_to_alert
from alert import AlertManager
import ledger
from position_printer import PositionPrinter

STOP = threading.Event()

# ============ 限流退避（账户线程/行情线程共用）============
BACKOFF_BASE = 2.0        # 退避翻倍基数（秒）
BACKOFF_MAX = 120.0       # 单个账户轮询间隔封顶（秒）
BACKOFF_COOLDOWN = 60.0   # 连续多少秒无错误，把间隔降回基础值


def load_settings() -> dict:
    p = os.path.join(ROOT, "config", "settings.yaml")
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def dump_snapshot(snap, settings, use_models=False):
    """落盘快照；tickers 仅保留 持仓合约 + 白名单币种（瘦身，避免全市场几百个合约反复刷盘）。

    若 use_models=True 且 snap 中混入了 AccountInfo/Position 对象，会在写入前
    把它们转成兼容 dict，避免 json.dump 报 TypeError。默认路径（snap 里全是
    原始 dict）则不做额外转换，保持向下兼容。
    """
    from models import AccountInfo as _AI, Position as _Pos
    from risk import position_panel

    account_filter = None
    position_filter = None

    if use_models:
        account_filter = lambda a: a.to_snapshot_dict() if isinstance(a, _AI) else a
        position_filter = lambda p: p.to_snapshot_dict() if isinstance(p, _Pos) else p

    fund_cfg = settings.get("funding") or {}
    whitelist = set(fund_cfg.get("monitoring_whitelist", []))
    for name, a in (snap.get("accounts") or {}).items():
        if not a.get("ok"):
            continue
        tk = a.get("tickers") or {}
        panel = position_panel(a.get("positions") or [], compute_liq_distance=False)
        active = {p["contract"] for p in panel if _num(p.get("size", 0)) > 0}
        allowed = active | whitelist
        a["tickers"] = {c: t for c, t in tk.items() if c in allowed}
        if account_filter and isinstance(a.get("account"), _AI):
            a["account"] = account_filter(a["account"])
        if position_filter:
            a["positions"] = [position_filter(p) if isinstance(p, _Pos) else p
                              for p in a.get("positions", [])]
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    path = os.path.join(ROOT, "data", "snapshot.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=2, ensure_ascii=False)


# ============ 本地 OI 基线（趋势层用，纯本地落盘，不触交易所）============
# 结构: {"ts": epoch, "accounts": {name: {contract: oi}}}
_OI_BASELINE_PATH = os.path.join(ROOT, "data", "oi_baseline.json")


def load_oi_baseline() -> dict:
    try:
        if os.path.exists(_OI_BASELINE_PATH):
            with open(_OI_BASELINE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"ts": 0, "accounts": {}}


def save_oi_baseline(accounts_oi: dict):
    """accounts_oi: {name: {contract: oi}}。覆盖写（趋势层只看「窗口起点」快照）。"""
    try:
        os.makedirs(os.path.dirname(_OI_BASELINE_PATH), exist_ok=True)
        with open(_OI_BASELINE_PATH, "w", encoding="utf-8") as f:
            json.dump({"ts": int(time.time()), "accounts": accounts_oi},
                      f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# ============ 限流/鉴权 处理 helper（两线程共用，避免重复）============
def _on_rate_limit(backoff, last_err, name, alert, e, base_interval):
    last_err[name] = time.time()
    cur = backoff[name]
    nxt = min(cur * BACKOFF_BASE, BACKOFF_MAX)
    backoff[name] = nxt
    alert.fire("warning", f"[{name}] API 限流，已自动降速",
               f"Gate 返回限流（拉取过快）。已自动把该账户轮询间隔从 {cur:.0f}s 升到 {nxt:.0f}s，"
               f"限流解除后会逐步恢复。\n原始回馈: {e}",
               key="rate-limit")


def _on_recover(backoff, last_err, name, alert, base_interval):
    if backoff[name] > base_interval and (time.time() - last_err[name]) >= BACKOFF_COOLDOWN:
        backoff[name] = max(base_interval, backoff[name] / BACKOFF_BASE)
        if backoff[name] == base_interval:
            alert.fire("info", f"[{name}] 轮询已恢复正常",
                       f"限流解除，该账户轮询间隔已恢复为 {base_interval:.0f}s。",
                       key="rate-recover")


def _on_error(alert, name, e):
    emsg = str(e)
    # 鉴权失败 = key 过期/失效（Gate 返回 401 INVALID_KEY / Unauthorized）
    if "INVALID_KEY" in emsg or "401" in emsg or "Unauthorized" in emsg or "API_KEY" in emsg:
        alert.fire("critical", f"[{name}] API key 已过期/失效",
                   f"Gate 返回鉴权失败，该账户只读 key 需重新录入。\n"
                   f"请双击 enter_keys.bat 重填 {name} 的 key 与 secret，然后重启监控。",
                   key=f"err-auth:{name}")
    else:
        alert.fire("warning", f"[{name}] 拉取失败", f"账户数据拉取异常: {e}", key=f"err:{name}")


# ============ L1 账户线程 ============
def account_loop(watcher: MultiAccountWatcher, settings: dict, alert: AlertManager):
    risk_cfg = settings.get("risk") or {}
    liq_crit = _num(risk_cfg.get("liq_distance_critical_pct", 2.0))
    liq_warn = _num(risk_cfg.get("liq_distance_warning_pct", 5.0))
    margin_warn = _num(risk_cfg.get("margin_ratio_warning_pct", 30.0))
    base_interval = _num(settings.get("poll_account_seconds", 10))

    # 模型热路径开关（Roadmap 第6条）：默认关闭，启用前确认下游兼容。
    # 启用后两个线程会用 get_*_model 系列，并经由兼容后的风险函数处理模型对象。
    use_models = False

    backoff = {name: base_interval for name in watcher.clients}
    last_err = {name: 0.0 for name in watcher.clients}

    while not STOP.is_set():
        try:
            for name, cli in watcher.clients.items():
                try:
                    acct = cli.get_account()
                    positions = cli.get_positions()
                    acct_model = None
                    positions_models = None

                    if use_models:
                        acct_model = cli.get_account_model(display_name=name)
                        positions_models = cli.get_positions_models()
                except RateLimitError as e:
                    _on_rate_limit(backoff, last_err, name, alert, e, base_interval)
                    continue
                except Exception as e:
                    _on_error(alert, name, e)
                    continue
                else:
                    _on_recover(backoff, last_err, name, alert, base_interval)

                # Gate 全仓账户 get_account() 返回单个 dict（全仓+逐仓字段都在）
                unified = acct if isinstance(acct, dict) else (acct[0] if isinstance(acct, list) and acct else {})

                # --- 账户级保证金率（逐仓/全仓通用）---
                try:
                    if use_models and acct_model is not None:
                        saf = account_safety(acct_model)
                    else:
                        saf = account_safety(unified)
                    mr = saf["available_margin_rate_pct"]
                    if mr is not None and mr < margin_warn:
                        alert.fire("warning", f"[{name}] 保证金率偏低",
                                   f"可用保证金率 {mr:.1f}% < {margin_warn:.1f}% (总保证金 {saf['total']:.2f}, 维持 {saf['maintenance_margin']:.4f}, 模式 {saf['margin_mode']})",
                                   key=f"mr:{name}")
                except Exception:
                    pass

                # --- 逐仓持仓：强平距离 + ADL 排名 ---
                try:
                    if use_models:
                        panel = position_panel(positions_models)
                    else:
                        panel = position_panel(positions)
                except Exception:
                    panel = []

                for row in panel:
                    ld = row.get("liq_distance_pct")
                    if ld is not None:
                        c = row["contract"]
                        if ld < liq_crit:
                            alert.fire("critical", f"[{name}] 强平临界 {c}",
                                       f"{row['side']} 仓位强平距离仅 {ld:.2f}%（标记价 {row['mark_price']}，强平价 {row['liq_price']}，杠杆 {row['leverage']}x）",
                                       key=f"liq:{name}:{c}")
                        elif ld < liq_warn:
                            alert.fire("warning", f"[{name}] 强平接近 {c}",
                                       f"{row['side']} 仓位强平距离 {ld:.2f}%", key=f"liq:{name}:{c}")
                    # ADL 自动减仓排队名次（Gate 1-5 档）：保守处理——不再弹窗告警，
                    # 仅在持仓定时打印中展示原始档位，以交易所 App 指示灯判断实际危险度。
                    _ = row.get("adl_ranking")

                # --- 权益曲线（L3 回撤数据源）---
                # 用账户总权益 total（含未实现盈亏），而非逐仓保证金占用，否则序列平直、回撤恒为 0
                try:
                    if use_models and acct_model is not None:
                        acct_total = acct_model.total_equity
                        upnl_sum = _num(acct_model.unrealised_pnl) + sum(_num(p.get("unrealised_pnl")) for p in panel)
                    else:
                        acct_total = _num(unified.get("total"))
                        upnl_sum = _num(unified.get("unrealised_pnl")) + sum(_num(p.get("unrealised_pnl")) for p in panel)
                    ledger.append_equity(name, acct_total, upnl_sum)
                except Exception:
                    pass
        except Exception as e:
            print(f"[account_loop] 异常: {e}", file=sys.stderr)
        sleep_for = max(backoff.values()) if backoff else base_interval
        STOP.wait(sleep_for)


# ============ L2 行情线程 ============
def market_loop(watcher: MultiAccountWatcher, settings: dict, alert: AlertManager, prev_oi: dict, prev_price: dict, prev_oi_price: dict):
    fund_cfg = settings.get("funding") or {}
    settle_warn_sec = _num(fund_cfg.get("settle_warning_seconds", 1800))
    dev_warn = _num(settings.get("risk", {}).get("mark_index_deviation_pct", 0.5))
    oi_thresh = _num(settings.get("anomaly", {}).get("oi_change_pct", 8.0))
    # 新增：费率异常阈值 / OI×Price 信号阈值 / 插针阈值（白名单覆盖无持仓币）
    fund_abn = _num(settings.get("funding", {}).get("abnormal_rate_pct", 0.05))
    oisig_oi = _num(settings.get("anomaly", {}).get("oi_signal_oi_pct", 5.0))
    oisig_px = _num(settings.get("anomaly", {}).get("oi_signal_price_pct", 0.3))
    spike_pct = _num(settings.get("anomaly", {}).get("price_spike_pct", 1.5))
    # 大市值判定：OI 绝对值（张数，1张=1美元面值）超过该值视为大市值资产
    # 大币 20s 环比达 8% 需数千万美元瞬时变动，属巨鲸/极端行情级别，提示更醒目
    big_oi_threshold = _num(settings.get("anomaly", {}).get("big_oi_threshold", 100_000_000))
    # 大币 20s 瞬时环比阈值（降为 1.5%，让大币瞬时异动也能弹；小币仍用 oi_thresh=8%）
    big_oi_warn_pct = _num(settings.get("anomaly", {}).get("big_oi_warn_pct", 1.5))
    # 趋势层：本地 OI 基线窗口（分钟）+ 累计变化阈值（大币小币同阈值）
    oi_trend_window_min = _num(settings.get("anomaly", {}).get("oi_trend_window_minutes", 60))
    oi_trend_pct_th = _num(settings.get("anomaly", {}).get("oi_trend_pct", 30.0))
    base_interval = _num(settings.get("poll_market_seconds", 20))

    backoff = {name: base_interval for name in watcher.clients}
    last_err = {name: 0.0 for name in watcher.clients}

    while not STOP.is_set():
        try:
            for name, cli in watcher.clients.items():
                try:
                    tickers_list = cli.get_tickers()
                    positions = cli.get_positions()
                except RateLimitError as e:
                    _on_rate_limit(backoff, last_err, name, alert, e, base_interval)
                    continue
                except Exception as e:
                    _on_error(alert, name, e)
                    continue
                else:
                    _on_recover(backoff, last_err, name, alert, base_interval)

                tickers = {t.get("contract"): t for t in (tickers_list or [])}
                try:
                    if use_models:
                        panel = position_panel(positions_models)
                    else:
                        panel = position_panel(positions)
                except Exception:
                    panel = []
                active = {p["contract"] for p in panel if _num(p.get("size", 0)) > 0}
                whitelist = set(fund_cfg.get("monitoring_whitelist", []))
                allowed = active | whitelist

                # --- 资金费倒计时 (白名单过滤模式) ---
                for c, tk in tickers.items():
                    if c not in allowed:
                        continue
                    try:
                        fc = funding_countdown(tk)
                        if fc["settle_in_seconds"] < settle_warn_sec and "你付" in fc["payer"]:
                            # 费率固定 6 位小数格式，避免 5e-05 这类科学计数法显示
                            rate_txt = f"{fc['rate']:.6f}"
                            px = _num(tk.get("mark_price"))
                            chg24 = _num(tk.get("change_percentage"))
                            px_txt = f" | 当前价 {px:.4f}" if px else ""
                            chg24_txt = f" | 24h涨跌 {chg24:+.2f}%" if tk.get("change_percentage") is not None else ""
                            alert.fire("warning", f"[{name}] 资金费结算 {c}",
                                       f"{int(fc['settle_in_minutes'])} 分钟后结算，你是{fc['payer']}，费率 {rate_txt}{px_txt}{chg24_txt}",
                                       key=f"fund:{name}:{c}")
                    except Exception:
                        pass

                # --- 标记-指数背离 ---
                for row in panel:
                    tk = tickers.get(row["contract"])
                    if not tk:
                        continue
                    dev = mark_index_deviation(row["mark_price"], _num(tk.get("index_price")))
                    if dev is not None and abs(dev) > dev_warn:
                        px = _num(tk.get("mark_price"))
                        chg24 = _num(tk.get("change_percentage"))
                        px_txt = f" | 当前价 {px:.4f}" if px else ""
                        chg24_txt = f" | 24h涨跌 {chg24:+.2f}%" if tk.get("change_percentage") is not None else ""
                        alert.fire("warning", f"[{name}] 标记-指数背离 {row['contract']}",
                                   f"偏离 {dev:.2f}% > {dev_warn:.2f}%{px_txt}{chg24_txt}", key=f"dev:{name}:{row['contract']}")

                # --- OI 突增（L2 异动）---
                # 趋势层基线（纯本地落盘 data/oi_baseline.json，跨重启有效）：
                #   每个周期从磁盘读「窗口起点」全市场 OI，与当前对比算累计变化；
                #   窗口到点则把当前全市场 OI 写盘作为新基线起点。
                baseline = load_oi_baseline()
                trend_due = (baseline.get("ts", 0) == 0) or \
                    (time.time() - baseline.get("ts", 0) >= oi_trend_window_min * 60)
                # 本周期采集到的全市场 OI（用于窗口到点时写盘为新基线）
                cur_account_oi = {}

                for c, tk in tickers.items():
                    cur_oi = _num(tk.get("total_size"))
                    key_oi = f"{name}:{c}"
                    is_big = cur_oi > big_oi_threshold
                    # 大币用低瞬时阈值（1.5%），小币用原阈值（8%）
                    inst_thresh = big_oi_warn_pct if is_big else oi_thresh

                    # --- 瞬时层：20s 环比 ---
                    if key_oi in prev_oi and prev_oi[key_oi]:
                        ch = oi_change_pct(prev_oi[key_oi], cur_oi)
                        if ch is not None and abs(ch) > inst_thresh:
                            cur_px = _num(tk.get("mark_price"))
                            chg24 = _num(tk.get("change_percentage"))
                            px_txt = f"当前价 {cur_px:.4f}" if cur_px else ""
                            chg24_txt = f" | 24h涨跌 {chg24:+.2f}%" if tk.get("change_percentage") is not None else ""
                            warn_mark = "⚠ " if is_big else ""
                            big_note = ""
                            if is_big:
                                net_usd = abs(cur_oi - prev_oi[key_oi])
                                big_note = f"\n【大市值资产】20s 内 OI 净变约 ${net_usd/1e4:.0f}万，属巨鲸/极端行情级别"
                            alert.fire("warning", f"[{name}] {warn_mark}OI 异动 {c}",
                                       f"未平仓量变化 {ch:.1f}%（{px_txt}{chg24_txt}）{big_note}",
                                       key=f"oi:{key_oi}", sound="big_oi" if is_big else None)

                    # --- 趋势层：当前 vs 本地基线 累计变化（急涨急跌，抓趋势性建仓）---
                    base_acc = baseline.get("accounts", {}).get(name, {})
                    base_oi = base_acc.get(c)
                    if base_oi:
                        tr = oi_trend_pct(base_oi, cur_oi)
                        if tr is not None and abs(tr) > oi_trend_pct_th:
                            cur_px = _num(tk.get("mark_price"))
                            chg24 = _num(tk.get("change_percentage"))
                            chg24_txt = f" | 24h涨跌 {chg24:+.2f}%" if tk.get("change_percentage") is not None else ""
                            direction = "激增" if tr > 0 else "骤减"
                            alert.fire("warning", f"[{name}] OI 趋势异动 {c}",
                                       f"{oi_trend_window_min:.0f}分钟内未平仓量{direction} {abs(tr):.1f}%（当前价 {cur_px:.4f}{chg24_txt}）",
                                       key=f"oi-trend:{name}:{c}", sound="big_oi_trend" if is_big else None)
                    # 采集当前 OI 供窗口到期写盘
                    cur_account_oi.setdefault(name, {})[c] = cur_oi
                    prev_oi[key_oi] = cur_oi

                # 窗口到点：把本周期采集的全市场 OI 写盘为新基线起点
                if trend_due and cur_account_oi:
                    save_oi_baseline(cur_account_oi)

                # --- 资金费率绝对值异常（多空极端拥挤前兆）---
                for c, tk in tickers.items():
                    if c not in allowed:
                        continue
                    try:
                        fa = funding_rate_abnormal(
                            _num(tk.get("funding_rate")), fund_abn,
                            price=tk.get("mark_price"),          # 原样传，函数内判 None
                            change24h=tk.get("change_percentage"),
                        )
                        if fa:
                            alert.fire("warning", f"[{name}] 资金费率异常 {c}",
                                       fa["text"], key=f"fund-abn:{name}:{c}")
                    except Exception:
                        pass

                # --- 插针 / 瞬时爆拉（白名单无持仓币也监控）---
                for c, tk in tickers.items():
                    if c not in allowed:
                        continue
                    cur_px = _num(tk.get("mark_price"))
                    key_px = f"{name}:{c}"
                    prev = prev_price.get(key_px)
                    if prev:
                        chg = (cur_px - prev) / prev * 100.0 if prev else 0.0
                        if abs(chg) >= spike_pct:
                            direction = "爆拉" if chg > 0 else "插针"
                            chg24 = tk.get("change_percentage")
                            chg24_txt = f" | 24h涨跌 {_num(chg24):+.2f}%" if chg24 is not None else ""
                            alert.fire("warning", f"[{name}] 瞬时{direction} {c}",
                                       f"{c} 标记价 {direction} {abs(chg):.2f}%（{prev:.4f} → {cur_px:.4f}）{chg24_txt}，"
                                       f"疑似插针/瞬时异动", key=f"spike:{name}:{c}")
                    prev_price[key_px] = cur_px

                # --- OI × Price 语义信号（四分类：去杠杆/空头踩踏/资金流入/资金压制）---
                for c, tk in tickers.items():
                    if c not in allowed:
                        continue
                    cur_oi = _num(tk.get("total_size"))
                    cur_px = _num(tk.get("mark_price"))
                    key_oi = f"{name}:{c}"
                    prev = prev_oi_price.get(key_oi)
                    if prev and prev.get("oi") and prev.get("px"):
                        sig = oi_price_signal(prev["oi"], cur_oi, prev["px"], cur_px,
                                              oisig_oi, oisig_px,
                                              cur_price_abs=cur_px,
                                              change_24h_pct=_num(tk.get("change_percentage")))
                        res = interpret_to_alert(c, sig, name)
                        if res:
                            title, msg, key = res
                            # 四分类语义信号改用 info 级 -> 播放 info.mp3（配置 alert_sounds.info）
                            # neutral 在 interpret_to_alert 返回 None，根本不 fire，故 warning.mp3 仅保留配置不被触发
                            alert.fire("info", title, msg, key=key)
                    prev_oi_price[key_oi] = {"oi": cur_oi, "px": cur_px}
        except Exception as e:
            print(f"[market_loop] 异常: {e}", file=sys.stderr)
        sleep_for = max(backoff.values()) if backoff else base_interval
        STOP.wait(sleep_for)


# ============ 主：汇总快照 + L3 日终账本 ============

def _utc_day_bounds(day: datetime.date) -> tuple:
    """返回某 UTC 日的 [start_ts, end_ts) 秒级时间戳（含当日 00:00:00 UTC，不含次日）。"""
    start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def _existing_ledger_days(account_name: str) -> set:
    """读 data/ledger/ 已存在的账本日期集合（UTC 自然日，文件名 YYYY-MM-DD）。"""
    out = set()
    d = os.path.join(ROOT, "data", "ledger")
    if not os.path.isdir(d):
        return out
    for fp in glob.glob(os.path.join(d, f"{account_name}_*.json")):
        bn = os.path.basename(fp)
        ds = bn[len(f"{account_name}_"):-5]
        try:
            out.add(datetime.strptime(ds, "%Y-%m-%d").date())
        except Exception:
            continue
    return out


def maybe_daily_ledger(watcher: MultiAccountWatcher, settings: dict, snap: dict = None,
                       on_day: callable = None):
    """按 UTC 日精确生成/补齐每日账本（L3 复盘），与交易所结算口径一致。

    设计（非 24/7 友好、永不超时）：
    - 每次调用最多补齐 per_run_days 天（默认 7），避免一次性拉 180 天过载/超时。
    - 多日缺口（关机数天）跨多次运行自动续补：本次拉 7 天，下次再拉 7 天，直至追平到昨天。
    - 已追平到昨天则跳过（零成本）；若从未有账本，从 backfill_days(默认180)天前开始。
    - 多账户各自独立补齐（遍历 watcher.clients）。
    - 每日用 get_income(_from,to=UTC日界) 精确拉该日流水，零越界、与交易所总览对齐。
    - snap 为已拉快照，取 account.total 作为真实权益，零额外 API。
    """
    backfill_days = int(_num(settings.get("daily_ledger_backfill_days", 180)))
    per_run = int(_num(settings.get("daily_ledger_backfill_per_run_days", 7)))
    today_utc = datetime.now(timezone.utc).date()
    yest_utc = today_utc - timedelta(days=1)
    progress = {}  # name -> 补齐进度报告
    for name, cli in watcher.clients.items():
        try:
            existing = _existing_ledger_days(name)
            if existing:
                start_day = max(existing) + timedelta(days=1)
            else:
                start_day = today_utc - timedelta(days=backfill_days)
            if start_day > yest_utc:
                progress[name] = {"pulled": 0, "from": None, "to": None,
                                  "target": yest_utc.isoformat(), "done": True}
                continue  # 已追平到昨天，无需补齐
            # 本次最多补齐 per_run 天（避免单次过载），跨次运行续补
            end_day = min(yest_utc, start_day + timedelta(days=per_run - 1))
            pulled = 0
            day = start_day
            while day <= end_day:
                t0, t1 = _utc_day_bounds(day)
                income = cli.get_income(limit=1000, _from=t0, to=t1) or []
                equity = 0.0
                try:
                    u = (snap.get("accounts", {}).get(name, {}) or {}).get("account", {})
                    equity = _num(u.get("total"))
                except Exception:
                    pass
                ledger.daily_ledger(name, day.strftime("%Y-%m-%d"), income, equity)
                day += timedelta(days=1)
                pulled += 1
                if on_day is not None:
                    try:
                        on_day(name, day - timedelta(days=1), pulled, per_run,
                               (end_day - start_day).days + 1)
                    except Exception:
                        pass
            remaining = (yest_utc - end_day).days
            progress[name] = {"pulled": pulled, "from": start_day.isoformat(),
                              "to": end_day.isoformat(), "target": yest_utc.isoformat(),
                              "done": remaining <= 0, "remaining": remaining}
        except Exception as e:
            print(f"[daily_ledger][{name}] 异常: {e}", file=sys.stderr)
            progress[name] = {"pulled": 0, "from": None, "to": None,
                              "target": yest_utc.isoformat(), "done": False, "error": str(e)}
    return progress

def snapshot_loop(watcher: MultiAccountWatcher, settings: dict, printer: PositionPrinter):
    while not STOP.is_set():
        try:
            snap = watcher.snapshot_all()
            if settings.get("dump_full_snapshot", True):
                dump_snapshot(snap, settings)
            # 注：每日账本补齐不在此处做（避免后台常驻静默抢补、用户看不到进度）。
            # 补齐专属 risk_board.py 启动流程（带进度动画 + 并发），常驻只管实时数据。
            # 每 N 分钟打印一次持仓（从本地 snapshot.json 提取，不额外调 API）
            printer.tick(os.path.join(ROOT, "data", "snapshot.json"))
            # 周期把 warning/info 合并成摘要邮件发出（critical 已在 fire 时即时发）
            try:
                alert.flush_digest()
            except Exception:
                pass
        except Exception as e:
            print(f"[snapshot_loop] 异常: {e}", file=sys.stderr)
        STOP.wait(5)


def main():
    ap = argparse.ArgumentParser(description="GateRiskWatcher 只读风险哨兵")
    ap.add_argument("--once", action="store_true", help="只跑一次快照并退出（自检用）")
    args = ap.parse_args()

    settings = load_settings()
    accounts = load_accounts(settings)
    if not accounts:
        print("未找到任何 key。请先运行: python src/setup_keys.py")
        sys.exit(1)
    print(f"已加载账户 ({len(accounts)}): {describe(accounts)}")
    print("【只读模式】本程序只调用 GET，绝不下单。")

    watcher = MultiAccountWatcher(accounts)
    alert = AlertManager(settings)
    printer = PositionPrinter(settings)
    prev_oi: dict = {}
    prev_price: dict = {}       # 插针检测：上一帧标记价
    prev_oi_price: dict = {}    # OI×Price 信号：上一帧 {oi, px}

    if args.once:
        snap = watcher.snapshot_all()
        dump_snapshot(snap, settings)
        print(json.dumps(snap, indent=2, ensure_ascii=False)[:1500])
        # 连通性
        for n, c in watcher.clients.items():
            print(f"  {n} ping={'OK' if c.ping() else 'FAIL'}")
        return

    # 启动线程
    t_acc = threading.Thread(target=account_loop, args=(watcher, settings, alert), daemon=True)
    t_mkt = threading.Thread(target=market_loop, args=(watcher, settings, alert, prev_oi, prev_price, prev_oi_price), daemon=True)
    t_snap = threading.Thread(target=snapshot_loop, args=(watcher, settings, printer), daemon=True)
    t_acc.start()
    t_mkt.start()
    t_snap.start()

    def _sig(*_):
        STOP.set()
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    print("监控已启动。Ctrl+C 退出。")
    try:
        while not STOP.is_set():
            STOP.wait(1)
    finally:
        STOP.set()
        print("正在退出…")


if __name__ == "__main__":
    main()
