#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_okx_minimal.py — OKX V5 只读连通性测试（不下单、不写任何东西）

职责：
  - 从 .env 读取 OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE
  - 调用 V5 只读端点：
      /api/v5/account/balance        → 账户余额/权益摘要
      /api/v5/account/positions      → 持仓
  - 不调用任何写/下单/撤单接口，仅作连通性与认证校验。
  - 输出摘要（打码），不在日志/控制台泄露真值。

用法：
  .venv\\Scripts\\python.exe src\\test_okx_minimal.py
"""

import os
import sys
import time
import hashlib
import hmac
import base64
import json
import urllib.request
import urllib.error
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

# ---------- 从 .env 读（不打印真值） ----------

ENV_PATH = os.path.join(ROOT, "secrets", ".env")


def _read_env(name: str) -> str:
    if not os.path.exists(ENV_PATH):
        return ""
    data: dict[str, str] = {}
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data.get(name, "")


API_KEY = _read_env("OKX_API_KEY")
API_SECRET = _read_env("OKX_API_SECRET")
PASSPHRASE = _read_env("OKX_PASSPHRASE")

if not API_KEY or not API_SECRET:
    print("ERROR: OKX_API_KEY / OKX_API_SECRET 缺失，请先运行 enter_keys.bat 录入。")
    sys.exit(1)

print(f"OKX_API_KEY  {'已设置' if API_KEY else '缺失'}")
print(f"OKX_API_SECRET  {'已设置' if API_SECRET else '缺失'}")
print(f"OKX_PASSPHRASE  {'已设置' if PASSPHRASE else '缺失(可能不需要)'}")


# ---------- 签名辅助 ----------

def _sign(verb: str, endpoint: str, timestamp: str, body: str = "") -> str:
    """
    OKX V5 签名规则（HMAC-SHA256）：
      signature = Base64(HMAC-SHA256(secret, verb + endpoint + timestamp + body))
    """
    message = f"{verb}{endpoint}{timestamp}{body}"
    dig = hmac.new(
        API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(dig).decode("utf-8")


def _headers(verb: str, endpoint: str, body: str = "") -> dict[str, str]:
    ts = str(int(time.time() * 1000))
    return {
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": _sign(verb, endpoint, ts, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE or "",
        "Content-Type": "application/json",
    }


def _request(verb: str, path: str, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    url = f"https://www.okx.com{path}"
    payload = json.dumps(body or {}).encode("utf-8") if body else b""
    req = urllib.request.Request(url, data=payload, method=verb)
    for k, v in _headers(verb, path, json.dumps(body or {})).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        body_text = body_bytes.decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} on {path}: {body_text[:300]}") from e
    except Exception as e:
        raise RuntimeError(f"请求 {path} 失败: {e}") from e


# ---------- 端点 ----------

def get_balance() -> dict[str, Any]:
    return _request("GET", "/api/v5/account/balance")


def get_positions() -> dict[str, Any]:
    return _request("GET", "/api/v5/account/positions")


# ---------- 主 ----------

def main() -> None:
    print("\n=== OKX V5 只读连通性测试 ===")
    print("1) 调用 /api/v5/account/balance ...")
    try:
        bal = get_balance()
    except Exception as e:
        print(f"  FAIL: {e}")
        sys.exit(1)

    code = bal.get("code")
    msg = bal.get("msg")
    if code != "0":
        print(f"  API 返回非 0: code={code}, msg={msg}")
        sys.exit(1)

    data = bal.get("data", [])
    if not data:
        print("  返回 data 为空（可能无账户/权限）")
    else:
        acct = data[0]
        # 摘要：不打印真值，仅观察结构
        print(f"  账户结构示例: currency={acct.get('currency')}, "
              f"total={acct.get('total')}, available={acct.get('available')}, "
              f"balance={acct.get('balance')}, frozen={acct.get('frozen')}")
        # 打印出关键字段以确认连通性，但仅展示数值类型/空值，不泄露金额真值
        for f in ("balance", "available", "total", "enable_rate"):
            val = acct.get(f)
            print(f"    {f}: {val}")

    print("\n2) 调用 /api/v5/account/positions ...")
    try:
        pos = get_positions()
    except Exception as e:
        print(f"  FAIL: {e}")
        sys.exit(1)

    pcode = pos.get("code")
    pmsg = pos.get("msg")
    if pcode != "0":
        print(f"  API 返回非 0: code={pcode}, msg={pmsg}")
        sys.exit(1)

    pdata = pos.get("data", [])
    if not pdata:
        print("  返回持仓 data 为空（可能无持仓）")
    else:
        print(f"  持仓数量: {len(pdata)}")
        for p in pdata[:3]:
            print(f"    示例持仓: instId={p.get('instId')}, pos={p.get('pos')}, "
                  f"availPos={p.get('availPos')}, avgPx={p.get('avgPx')}, "
                  f"last={p.get('last')}, markPx={p.get('markPx')}")
        if len(pdata) > 3:
            print(f"    ... 共 {len(pdata)} 条")

    print("\n✅ OKX V5 只读认证 + 网络连通性测试成功（未调用任何写接口）。")


if __name__ == "__main__":
    main()
