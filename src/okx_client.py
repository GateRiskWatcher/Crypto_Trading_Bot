#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
okx_client.py — OKX V5 USDT 永续只读适配器（BaseExchange 实现）

职责：
  - 从 .env 读取 OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE
  - 实现 BaseExchange 规定的只读接口（account / positions / tickers）
  - 返回模型对象（AccountInfo / Position），与 GateAdapter 接口一致
  - 只读白名单来自 settings.yaml 的 OKX.readonly_methods
  - 网络/签名失败返回 None / 空列表，不抛走顶层循环

安全契约（与项目铁律一致）：
  - 不存储/打印 key 真值；打码仅用于启动日志
  - 仅调用 readonly_methods 里的端点；不在本模块新增任何写/下单接口
  - 若 OKX 不可用（网络/认证），上层看到的是 "该账户缺失" 状态

OKX V5 签名规则：
  signature = Base64(HMAC-SHA256(secret, verb + endpoint + timestamp + body))
  Headers: OK-ACCESS-KEY / OK-ACCESS-SIGN / OK-ACCESS-TIMESTAMP / OK-ACCESS-PASSPHRASE
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from base_exchange import BaseExchange
from exchange_config import ExchangeConfig
from keystore import AccountKey as _AccountKey
from models import AccountInfo, Position, _num


class OKXAdapter(BaseExchange):
    """
    OKX USDT 永续只读适配器。

    每个实例对应 OKX 的一个账户（一个 AccountKey）。
    网络不通时 account() 返回 None，positions() 返回空列表。
    """

    def __init__(self, account: _AccountKey, config: ExchangeConfig):
        if not account.api_key or not account.api_secret:
            raise ValueError("OKXAdapter 需要有效的 api_key / api_secret")
        # OKX 特有：passphrase 为必需认证参数（即使为空也允许启动，签名时带上）
        self._api_key = account.api_key
        self._api_secret = account.api_secret
        self._passphrase = account.passphrase or ""
        self._host = config.host or "https://www.okx.com"
        self._account_key = account
        self._config = config

        # readonly_methods 从 settings.yaml 读取，转为 set 方便校验
        methods = set(config.readonly_methods) if config.readonly_methods else set()
        self._readonly_methods = frozenset(methods)

    # ------------------------------------------------------------------
    # BaseExchange 身份
    # ------------------------------------------------------------------

    @property
    def exchange_name(self) -> str:
        return "OKX"

    @property
    def readonly_methods(self) -> frozenset[str]:
        return self._readonly_methods

    # ------------------------------------------------------------------
    # OKX V5 签名 + 请求
    # ------------------------------------------------------------------

    def _sign(self, verb: str, endpoint: str, timestamp: str, body: str = "") -> str:
        """OKX V5 HMAC-SHA256 签名 → Base64。"""
        message = f"{verb}{endpoint}{timestamp}{body}"
        dig = hmac.new(
            self._api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(dig).decode("utf-8")

    def _headers(self, verb: str, endpoint: str, body: str = "") -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        return {
            "OK-ACCESS-KEY": self._api_key,
            "OK-ACCESS-SIGN": self._sign(verb, endpoint, ts, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self._passphrase,
            "Content-Type": "application/json",
        }

    def _request(self, verb: str, path: str, body: Optional[dict] = None) -> dict[str, Any]:
        """
        发起 OKX V5 只读请求。失败时抛异常（由调用方捕获并返回 None/[]）。
        """
        self._check_readonly(path)
        url = f"{self._host}{path}"
        body_str = json.dumps(body or {})
        payload = body_str.encode("utf-8") if body else b""
        req = urllib.request.Request(url, data=payload, method=verb)
        for k, v in self._headers(verb, path, body_str).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)

    # ------------------------------------------------------------------
    # BaseExchange 标准只读接口（模型对象）
    # ------------------------------------------------------------------

    def account(self) -> Optional[AccountInfo]:
        """
        获取 OKX 账户余额 → AccountInfo。
        OKX /api/v5/account/balance 返回 {code, data: [{details, ...}]}>
        主账户汇总在 data[0].totalEq（总权益 USDT），details 里有各币种。
        """
        try:
            resp = self._request("GET", "/api/v5/account/balance")
        except Exception:
            return None

        code = resp.get("code")
        if code != "0":
            return None

        data = resp.get("data") or []
        if not data:
            return None

        acct = data[0]
        # totalEq = 总权益（USDT），availBal = 可用余额（从 details 里 USDT 条目取）
        total_eq = _num(acct.get("totalEq"))
        # USDT 可用余额：从 details 找 USDT 条目
        available = None
        unrealised_pnl = None
        details = acct.get("details") or []
        for d in details:
            if (d.get("ccy") or "").upper() == "USDT":
                available = _num(d.get("availBal"))
                # OKX 的 availEq（权益余额）可能更有用，但 availBal 是实际可用
                break

        # 未实现盈亏：OKX 没有直接的账户级未实现盈亏字段
        # 需要从 positions 汇总或用 account/detail 接口；这里先留 None
        # （上层 watch.py 如果需要会从 positions 汇总）
        # 注：OKX /api/v5/account/positions 里每个持仓有 upl，但 account 接口不提供汇总

        # 保证金模式：OKX 单独接口 /api/v5/account/account-position-risk
        # 这里简单标记；实际用的时候上层可按需查询
        margin_mode = "unknown"

        return AccountInfo(
            exchange="OKX",
            account_id=str(acct.get("uid") or acct.get("marginBal") or ""),
            display_name=self._account_key.name,
            total_equity=total_eq,
            available=available,
            unrealised_pnl=unrealised_pnl,
            margin_mode=margin_mode,
            raw=acct,
        )

    def positions(self) -> list[Position]:
        """
        获取 OKX 持仓列表 → Position 列表。
        OKX /api/v5/account/positions 返回 {code, data: [{instId, pos, ...}]}>
        """
        try:
            resp = self._request("GET", "/api/v5/account/positions")
        except Exception:
            return []

        code = resp.get("code")
        if code != "0":
            return []

        data = resp.get("data") or []
        result: list[Position] = []
        for p in data:
            result.append(self._position_from_okx(p))
        return result

    def tickers(self, contracts: Optional[list[str]] = None) -> list[dict[str, Any]]:
        """
        获取 OKX 行情快照。
        OKX /api/v5/public/tickers 返回 {code, data: [{instId, last, ...}]}>
        返回内部标准格式 dict 列表（与 Gate tickers 对齐）。
        """
        try:
            params: dict[str, str] = {}
            if contracts:
                # OKX tickers 接口支持 instType=SWAP + instId 过滤
                # 但单次只支持一个 instId；多个需分别调用
                # 简化：一次请求拿 SWAP 全量，再本地过滤
                params["instType"] = "SWAP"
            else:
                params["instType"] = "SWAP"

            # 构造带查询参数的 GET
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            path = f"/api/v5/public/tickers?{qs}"
            resp = self._request("GET", path)
        except Exception:
            return []

        code = resp.get("code")
        if code != "0":
            return []

        data = resp.get("data") or []
        # 本地过滤：若指定了 contracts，筛选匹配的 instId
        if contracts:
            wanted = {c.upper() for c in contracts}
            # OKX instId 格式：BTC-USDT-SWAP，需要转换到内部格式 BTC_USDT
            # exchange_config.normalize_symbol 可以做，但这里简单处理
            data = [
                t for t in data
                if self._okx_inst_to_internal(t.get("instId", "")).upper() in wanted
                or t.get("instId", "").upper() in wanted
            ]

        result: list[dict[str, Any]] = []
        for t in data:
            result.append(self._ticker_to_internal(t))
        return result

    # ------------------------------------------------------------------
    # OKX 持仓解析
    # ------------------------------------------------------------------

    @staticmethod
    def _position_from_okx(p: dict) -> Position:
        """
        OKX /api/v5/account/positions 的一条持仓 → Position。
        OKX 持仓关键字段：
          instId   : 合约标识，如 "BTC-USDT-SWAP"
          pos      : 持仓数量（张数，正=多，负=空）
          availPos : 可用持仓
          avgPx    : 开仓均价
          markPx   : 标记价
          liqPx    : 强平价（全仓时有值，逐仓可能为 ""）
          upl      : 未实现盈亏（USDT）
          lever    : 杠杆
          margin   : 保证金
          mgnMode  : 保证金模式（cross/isolated）
          cVal     : 每张合约面值（USDT），如 BTC-USDT-SWAP = 0.01 BTC
          posSide  : 持仓方向（long/short/net），双向持仓模式时为 long/short
        """
        inst_id = p.get("instId") or ""
        # 转内部格式：BTC-USDT-SWAP → BTC_USDT
        contract = OKXAdapter._okx_inst_to_internal(inst_id)
        symbol = contract.split("_")[0] if contract else ""

        pos_side = (p.get("posSide") or "").lower()
        pos_val = _num(p.get("pos")) or 0.0

        # 双向持仓：posSide 决定方向；单向持仓（net）：pos 正负决定方向
        if pos_side == "long":
            side = "long"
        elif pos_side == "short":
            side = "short"
        else:
            side = "long" if pos_val > 0 else "short" if pos_val < 0 else "none"

        size = abs(pos_val) if pos_val else None
        mark = _num(p.get("markPx"))
        entry = _num(p.get("avgPx"))
        liq = _num(p.get("liqPx"))
        c_val = _num(p.get("cVal"))  # 每张面值
        # 币数 = 张数 × 每张面值 / 标记价（若 cVal 存在）
        # OKX cVal 是每张的 USDT 面值；币数 = 张数 × cVal / markPx
        if size and c_val and mark and mark > 0:
            amount = size * c_val / mark
        else:
            amount = size  # fallback：无法换算时用张数代替

        lever = _num(p.get("lever"))
        margin = _num(p.get("margin"))
        upl = _num(p.get("upl"))

        # ROE = 未实现盈亏 / 保证金 × 100%（简化算法）
        roe = None
        if margin and margin > 0 and upl is not None:
            roe = upl / margin * 100.0

        # 强平距离
        liq_dist = None
        if mark and liq is not None:
            if side == "long" and liq < mark:
                liq_dist = (mark - liq) / mark * 100.0
            elif side == "short" and liq > mark:
                liq_dist = (liq - mark) / mark * 100.0

        mgn_mode_raw = (p.get("mgnMode") or "").lower()
        if mgn_mode_raw == "cross":
            margin_mode = "全仓"
        elif mgn_mode_raw == "isolated":
            margin_mode = "逐仓"
        else:
            margin_mode = "未知"

        # OKX 无 ADL ranking 字段
        return Position(
            contract=contract,
            symbol=symbol,
            side=side,
            size=size,
            amount=amount,
            leverage=lever,
            entry_price=entry,
            mark_price=mark,
            liq_price=liq,
            margin=margin,
            margin_mode=margin_mode,
            unrealised_pnl=upl,
            adl_ranking=None,
            liq_distance_pct=liq_dist,
            roe_pct=roe,
            initial_margin=margin,  # OKX 的 margin 即占用保证金
            raw=dict(p) if p else {},
        )

    # ------------------------------------------------------------------
    # OKX ticker → 内部格式
    # ------------------------------------------------------------------

    @staticmethod
    def _ticker_to_internal(t: dict) -> dict[str, Any]:
        """
        OKX /api/v5/public/tickers 一条 → 内部标准 dict。
        OKX 字段：instId, last, markPx, open24h, high24h, low24h, vol24h, ...
        """
        inst_id = t.get("instId") or ""
        contract = OKXAdapter._okx_inst_to_internal(inst_id)
        mark = _num(t.get("markPx"))
        last = _num(t.get("last"))

        # OKX ticker 没有直接的 fundingRate 和 openInterest
        # 这些需要单独接口 /api/v5/public/funding-rate 和 /api/v5/public/open-interest
        # 但 BaseExchange.tickers 要求包含 funding_rate / oi
        # 这里先填 0，上层 watch.py 如需要可单独调接口补充
        return {
            "contract": contract,
            "mark_price": mark or last or 0.0,
            "index_price": 0.0,  # OKX 不直接提供 index price 在 ticker 里
            "funding_rate": 0.0,
            "oi": 0.0,
            "last_price": last or 0.0,
        }

    @staticmethod
    def _okx_inst_to_internal(inst_id: str) -> str:
        """
        OKX instId 格式 "BTC-USDT-SWAP" → 内部 "BTC_USDT"。
        """
        if not inst_id:
            return ""
        # 去掉 -SWAP 后缀
        s = inst_id
        if s.upper().endswith("-SWAP"):
            s = s[:-5]  # 去 "-SWAP"
        # BTC-USDT → BTC_USDT（把 - 换成 _）
        s = s.replace("-", "_")
        return s

    # ------------------------------------------------------------------
    # OKX 公共接口：OI / Funding Rate（扩展接口，供 watch.py 额外调用）
    # ------------------------------------------------------------------

    def get_funding_rate(self, inst_id: str) -> Optional[float]:
        """
        获取指定合约的资金费率。
        OKX /api/v5/public/funding-rate?instId=BTC-USDT-SWAP
        返回 fundingRate（比例，如 0.0001 = 0.01%）。
        """
        try:
            resp = self._request("GET", f"/api/v5/public/funding-rate?instId={inst_id}")
            code = resp.get("code")
            if code != "0":
                return None
            data = resp.get("data") or []
            if data:
                return _num(data[0].get("fundingRate"))
        except Exception:
            pass
        return None

    def get_open_interest(self, inst_id: str) -> Optional[float]:
        """
        获取指定合约的未平仓量（OI）。
        OKX /api/v5/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP
        返回 oi（张数）。
        """
        try:
            resp = self._request(
                "GET",
                f"/api/v5/public/open-interest?instType=SWAP&instId={inst_id}",
            )
            code = resp.get("code")
            if code != "0":
                return None
            data = resp.get("data") or []
            if data:
                return _num(data[0].get("oi"))
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # 调试/可观测
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """轻量连通性检测：请求一个公共接口。"""
        try:
            resp = self._request("GET", "/api/v5/public/time")
            return resp.get("code") == "0"
        except Exception:
            return False
