#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_binance_minimal.py — Binance fapi/v2 只读连通性测试（不下单）

职责：
  - 从 .env 读取 BINANCE_API_KEY / BINANCE_API_SECRET
  - 调用 fapi/v2 只读端点：
      /fapi/v2/account     → 账户余额/保证金摘要
      /fapi/v2/positionRisk → 持仓
  - 采用 HMAC-SHA256 签名（Binance 标准），串行请求。
  - 仅作连通性与认证校验，不调用任何写接口。

用法：
  .venv\\Scripts\\python.exe src\\test_binance_minimal.py
"""

import os
import sys
import time
import hmac
import hashlib
import base64
import json
import urllib.request
import urllib.error
from typing import Optional, Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

ENV_PATH = os.path.join(ROOT, "secrets", ".env")


def _read_env(name: str) -> str:
    if not os.path.exists(ENV_PATH):
        return ""
    data = {}
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data.get(name, "")


API_KEY = _read_env("BINANCE_API_KEY")
API_SECRET = _read_env("BINANCE_API_SECRET")

if not API_KEY or not API_SECRET:
    print("ERROR: BINANCE_API_KEY / BINANCE_API_SECRET 缺失，请先运行 enter_keys.bat 录入。")
    sys.exit(1)

print(f"BINANCE_API_KEY  {'已设置' if API_KEY else '缺失'}")
print(f"BINANCE_API_SECRET  {'已设置' if API_SECRET else '缺失'}")


# ---------- Binance 签名 ----------

def _signature(query_string: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()


def _request(path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    host = "https://fapi.binance.com"
    url = host + path
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    # recvWindow 可选；默认 5000 ms
    params.setdefault("recvWindow", 5000)
    query_parts = []
    for k in sorted(params.keys()):
        v = params[k]
        if v is None:
            continue
        query_parts.append(f"{k}={urllib.parse.quote(str(v), safe='')}")
    query_string = "&".join(query_parts)
    sig = _signature(query_string, API_SECRET)
    query_string += f"&signature={sig}"

    url_full = f"{url}?{query_string}"
    req = urllib.request.Request(url_full, method="GET")
    req.add_header("X-MBX-API-Key", API_KEY)
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

def get_account() -> dict[str, Any]:
    return _request("/fapi/v2/account")


def get_position_risk() -> dict[str, Any]:
    return _request("/fapi/v2/positionRisk")


# ---------- 主 ----------

def main() -> None:
    print("\n=== Binance fapi/v2 只读连通性测试 ===")
    print("1) 调用 /fapi/v2/account ...")
    try:
        acct = get_account()
    except Exception as e:
        print(f"  FAIL: {e}")
        sys.exit(1)

    # Binance 返回：若 API Key 无效，返回 {"code":-2015,"msg":"Invalid API-key..."}
    code = acct.get("code")
    msg = acct.get("msg")
    if code is not None and code != 0:
        print(f"  API 返回错误: code={code}, msg={msg}")
        sys.exit(1)

    assets = acct.get("assets", [])
    print(f"  资产数量: {len(assets)}")
    for a in assets[:3]:
        print(f"    示例资产: asset={a.get('asset')}, balance={a.get('balance')}, "
              f"available={a.get('available')}, withheld={a.get('withheld')}")
        for f in ("balance", "available", "walletBalance", "marginBalance"):
            val = a.get(f)
            if val is not None:
                print(f"      {f}: {val}")
    if len(assets) > 3:
        print(f"    ... 共 {len(assets)} 资产")

    print("\n2) 调用 /fapi/v2/positionRisk ...")
    try:
        pos = get_position_risk()
    except Exception as e:
        print(f"  FAIL: {e}")
        sys.exit(1)

    pcode = pos.get("code")
    pmsg = pos.get("msg")
    if pcode is not None and pcode != 0:
        print(f"  API 返回错误: code={pcode}, msg={pmsg}")
        sys.exit(1)

    positions = pos.get("positions", [])
    if not positions:
        print("  返回持仓为空（可能无持仓）")
    else:
        print(f"  持仓数量: {len(positions)}")
        for p in positions[:3]:
            print(f"    示例持仓: symbol={p.get('symbol')}, positionSide={p.get('positionSide')}, "
                  f"positionAmt={p.get('positionAmt')}, entryPrice={p.get('entryPrice')}, "
                  f"markPrice={p.get('markPrice')}, liquidationPrice={p.get('liquidationPrice')}")
            for f in ("positionAmt", "entryPrice", "markPrice", "liquidationPrice"):
                val = p.get(f)
                if val is not None:
                    print(f"      {f}: {val}")
        if len(positions) > 3:
            print(f"    ... 共 {len(positions)} 条")

    print("\n✅ Binance fapi/v2 只读认证 + 网络连通性测试成功（未调用任何写接口）。")


if __name__ == "__main__":
    import urllib.parse
    main()
