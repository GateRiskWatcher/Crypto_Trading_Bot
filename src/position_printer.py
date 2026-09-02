#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
position_printer.py — 持仓定时打印（只读，从本地快照提取）

功能：每 N 分钟把各账户持仓按「交易所 App 持仓列表」风格打印到终端，
并环形写入 logs/positions.log（只保留最近 keep 次，避免长期驻守膨胀）。

数据来源：data/snapshot.json（由 watch.py 的 snapshot_loop 每 5s 落盘，
含账户/持仓全字段），本模块**不额外调用任何 API**，完全只读本地文件。

保守路线：ADL 仅展示 Gate API 原始档位 adl_ranking（1-5），不做任何
弹窗/告警——与交易所 App「5 格亮灯」是两套口径，以 App 为准。
"""
import os
import sys
import time
import json
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from risk import position_panel, adl_risk

_NUM = lambda x: float(x) if isinstance(x, (int, float)) else 0.0
_SEP = "\n\n" + "─" * 60 + "\n"


class PositionPrinter:
    def __init__(self, settings: dict):
        self.interval_seconds = int(_NUM(settings.get("position_print_interval_minutes", 5)) * 60)
        if self.interval_seconds <= 0:
            self.interval_seconds = 300
        self.keep = int(_NUM(settings.get("position_print_keep", 20)))
        if self.keep <= 0:
            self.keep = 20
        self.last_print = 0.0

    def tick(self, snapshot_path: str):
        """由 watch.py 的 snapshot_loop 每轮调用；到点才打印一次。"""
        now = time.time()
        if now - self.last_print < self.interval_seconds:
            return
        self.last_print = now
        if not os.path.exists(snapshot_path):
            return
        try:
            with open(snapshot_path, "r", encoding="utf-8") as f:
                snap = json.load(f)
        except Exception:
            return
        text = self._format(snap)
        if text.strip():
            print(text)
            self._write_ring(text)

    def _format(self, snap: dict) -> str:
        """交易所 App 持仓列表风格：每个账户一块，每仓多行。"""
        lines = []
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        accounts = (snap.get("accounts") or {})
        if not accounts:
            return ""
        for name, a in accounts.items():
            if not a.get("ok"):
                continue
            lines.append(f"━━━ {ts}  持仓快照 · 账户【{name}】 ━━━")
            panel = position_panel(a.get("positions") or [])
            if not panel:
                lines.append("  （当前无持仓）")
                lines.append("")
                continue
            for r in panel:
                side = r.get("side", "long")
                mmode = r.get("margin_mode") or ""
                lev = r.get("leverage") or 0
                ld = r.get("liq_distance_pct")
                adl = r.get("adl_ranking")
                coin = (r.get("contract") or "___").split("_")[0]
                amt = r.get("amount")
                amt_s = f"{amt:.4f} {coin}" if amt is not None else f"{r.get('size')}张"
                roe = r.get("roe_pct")
                roe_s = f"{roe:+.2f}%" if roe is not None else "N/A"
                lines.append(f"  {r['contract']}   {side} · {mmode}   杠杆 {lev}x")
                lines.append(f"    数量 {amt_s} (={r.get('size')}张)  开仓 {r['entry_price']}  标记 {r['mark_price']}")
                ld_s = f"{ld:.2f}%" if ld is not None else "N/A"
                lines.append(
                    f"    强平价 {r['liq_price']} (距强平 {ld_s})  "
                    f"未实现 {r['unrealised_pnl']:+.4f}  ROE {roe_s}"
                )
                if adl is not None:
                    lines.append(f"    ADL档位(API原始): {adl}/6  [{adl_risk(adl)}]")
                lines.append("")
        return "\n".join(lines)

    def _write_ring(self, text: str):
        """环形写入 logs/positions.log：只保留最近 keep 个块，防膨胀。"""
        path = os.path.join(ROOT, "logs", "positions.log")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            blocks = []
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                blocks = [b for b in content.split(_SEP) if b.strip()]
            blocks.append(text)
            if len(blocks) > self.keep:
                blocks = blocks[-self.keep:]
            with open(path, "w", encoding="utf-8") as f:
                f.write(_SEP.join(blocks) + _SEP)
        except Exception:
            pass
