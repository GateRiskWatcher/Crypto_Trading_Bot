#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notify.py — 离机告警推送（邮件 / Telegram 预留）

【安全契约】
  本模块只「发消息」，绝不触达任何交易所接口；与 gate_client 完全隔离。
  推送凭据来自 secrets/push.env（与交易 key 的 secrets/.env 同目录但不同文件，
  互不读取）。即便推送被配置错误，也绝不影响只读哨兵的核心逻辑。

【防垃圾邮件设计】
  邮件是离机保命通道，但发太勤会变成垃圾邮件、甚至被 SMTP 服务商限流。
  本模块采用两层节流：
    1) 全局冷却：任意一条邮件发送后，cooldown_seconds 内不再发（默认 60s）。
       这能挡住「强平临界」在 20s 轮询下被反复触发时的洪泛。
    2) critical 每接收者冷却：critical 虽保命不抑制，但同一接收者 5 分钟内
       只真正投递一次（其余落入 digest）。
    3) warning/info 落 digest：每 digest 周期（默认 15 分钟）合并成一封摘要邮件，
       避免每条 warning 都发一封（脉冲 OI 异动、费率提醒等天然适合合并）。
   digest 由 watch 主循环调用 flush_digest() 周期触发；critical 即时发但受冷却约束。
"""

import os
import time
import smtplib
import threading
import email.utils
from email.mime.text import MIMEText
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load_push_cfg() -> dict:
    """
    从 secrets/push.env 读取推送配置。
    不读取 secrets/.env（交易 key 文件），仅读 push.env。
    缺失时返回空 dict（调用方据此禁用对应通道）。
    """
    cfg = {}
    p = os.path.join(ROOT, "secrets", "push.env")
    if not os.path.exists(p):
        return cfg
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    except Exception:
        pass
    return cfg


# ---------------- 邮件发送（标准库 smtplib，无需新增依赖） ----------------

class EmailNotifier:
    def __init__(self, cfg: dict, cooldown_seconds: float = 60.0,
                 critical_cooldown_seconds: float = 300.0):
        self.enabled = bool(cfg.get("EMAIL_FROM") and cfg.get("EMAIL_SMTP_HOST"))
        self.cfg = cfg
        self.cooldown = cooldown_seconds
        self.critical_cooldown = critical_cooldown_guard(critical_cooldown_seconds)
        self._last_send = 0.0          # 全局冷却
        self._last_critical_recv = {}  # 每接收者 critical 冷却
        self._lock = threading.Lock()

    def _can_send(self, level: str, recipient: str) -> bool:
        now = time.time()
        if now - self._last_send < self.cooldown:
            return False
        if level == "critical":
            last = self._last_critical_recv.get(recipient, 0.0)
            if now - last < self.critical_cooldown:
                return False
        return True

    def send(self, level: str, title: str, body: str,
             recipients: Optional[list] = None) -> bool:
        """
        发送一封邮件。受冷却约束；约束内返回 False（不发送）。
        失败静默返回 False（不影响主循环）。
        """
        if not self.enabled:
            return False
        recips = recipients or [r.strip() for r in self.cfg.get("EMAIL_TO", "").split(",") if r.strip()]
        if not recips:
            return False
        with self._lock:
            # 仅对第一个接收者做冷却判断（群发一封即可覆盖所有人）
            if not self._can_send(level, recips[0]):
                return False
            self._last_send = time.time()
            if level == "critical":
                for r in recips:
                    self._last_critical_recv[r] = time.time()
        try:
            frm = self.cfg.get("EMAIL_FROM")
            host = self.cfg.get("EMAIL_SMTP_HOST")
            port = int(self.cfg.get("EMAIL_SMTP_PORT", "465"))
            user = self.cfg.get("EMAIL_SMTP_USER", frm)
            pwd = self.cfg.get("EMAIL_SMTP_PASS", "")
            use_tls = self.cfg.get("EMAIL_USE_TLS", "1") in ("1", "true", "True")

            subject = f"[{level.upper()}] Gate哨兵 {title}"
            msg = MIMEText(body, "plain", "utf-8")
            msg["From"] = frm
            msg["To"] = ", ".join(recips)
            msg["Subject"] = email.utils.formataddr(("GateRiskWatcher", subject))
            msg["Date"] = email.utils.formatdate(localtime=True)

            with smtplib.SMTP_SSL(host, port, timeout=10) if (port == 465 or use_tls) \
                    else smtplib.SMTP(host, port, timeout=10) as s:
                if use_tls and port != 465:
                    s.starttls()
                if pwd:
                    s.login(user, pwd)
                s.sendmail(frm, recips, msg.as_string())
            return True
        except Exception:
            return False


def critical_cooldown_guard(v):
    # 小防护：避免把 critical_cooldown 误传成 None
    return v or 300.0


# ---------------- Telegram 预留（接口就绪，凭据留空即禁用） ----------------

class TelegramNotifier:
    """
    预留接口：未来启用时，在 secrets/push.env 填 TG_BOT_TOKEN / TG_CHAT_ID 即可。
    发送走 api.telegram.org 的 sendMessage（纯 GET/POST，与交易所无关）。
    当前不强制依赖 requests（标准库 urllib 即可），保持零新增依赖原则。
    """
    def __init__(self, cfg: dict):
        self.token = cfg.get("TG_BOT_TOKEN")
        self.chat_id = cfg.get("TG_CHAT_ID")
        self.enabled = bool(self.token and self.chat_id)

    def send(self, level: str, title: str, body: str) -> bool:
        if not self.enabled:
            return False
        try:
            import urllib.request
            import urllib.parse
            import json
            text = f"[{level.upper()}] {title}\n{body}"
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": "true",
            }).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception:
            return False


# ---------------- 聚合通知器（digest 合并 warning/info） ----------------

class NotifierHub:
    """
    汇总邮件 + Telegram；提供：
      - send_immediate(level, title, body)：critical 即时发（受冷却），
        warning/info 进入 digest 队列。
      - flush_digest()：把 digest 队列合并成一封邮件发出（由 watch 周期调用）。
    """
    def __init__(self, cfg: dict, email_cooldown: float = 60.0,
                 critical_cooldown: float = 300.0,
                 digest_recipients: Optional[list] = None):
        self.email = EmailNotifier(cfg, email_cooldown, critical_cooldown)
        self.tg = TelegramNotifier(cfg)
        self.enabled = self.email.enabled or self.tg.enabled
        self._digest = []
        self._digest_recipients = digest_recipients
        self._lock = threading.Lock()

    def send_immediate(self, level: str, title: str, body: str):
        if not self.enabled:
            return False
        sent = False
        if level == "critical":
            # 即时发（邮件+TG），受冷却约束
            if self.email.send("critical", title, body):
                sent = True
            if self.tg.send("critical", title, body):
                sent = True
        else:
            # warning/info 入 digest
            with self._lock:
                self._digest.append((level, title, body))
                sent = True
        return sent

    def flush_digest(self) -> bool:
        """合并 digest 发一封摘要邮件；为空时返回 False。"""
        with self._lock:
            if not self._digest:
                return False
            items = self._digest
            self._digest = []
        if not self.email.enabled:
            return False
        lines = [f"【Gate 哨兵 告警摘要 · {time.strftime('%Y-%m-%d %H:%M:%S')}】", ""]
        for i, (lv, ti, bd) in enumerate(items, 1):
            lines.append(f"{i}. [{lv.upper()}] {ti}")
            lines.append(f"   {bd.replace(chr(10), chr(10)+'   ')}")
            lines.append("")
        body = "\n".join(lines)
        return self.email.send("info", f"告警摘要（{len(items)} 条）", body,
                               recipients=self._digest_recipients)
