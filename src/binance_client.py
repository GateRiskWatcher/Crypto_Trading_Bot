#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
binance_client.py — Binance USDT-M 永续只读适配器（BaseExchange 实现）

职责：
  - 从 .env 读取 BINANCE_API_KEY / BINANCE_API_SECRET
  - 实现 BaseExchange 规定的只读接口（account / positions / tickers）
  - 返回模型对象（AccountInfo / Position），与 GateAdapter 接口一致
  - 只读白名单来自 settings.yaml 的 BINANCE.readonly_methods
  - 网络/签名失败返回 None / 空列表，不抛走顶层循环

安全契约（与项目铁律一致）：
  - 不存储/打印 key 真值；打码仅用于启动日志
  - 仅调用 readonly_methods 里的端点；不在本模块新增任何写/下单接口
  - 若 Binance 不可用（网络/墙），上层看到的是 "该账户缺失" 状态

Binance 签名规则：
  query_string = sorted(key=value)&...&timestamp=...&recvWindow=...
  signature = HMAC-SHA256(secret, query_string)
  Headers: X-MBX-API-Key
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from base_exchange import BaseExchange
from exchange_config import ExchangeConfig
from keystore import AccountKey as _AccountKey
from models import AccountInfo, Position, _num


class BinanceAdapter(BaseExchange):
    """
    Binance USDT-M 永续只读适配器。

    每个实例对应 Binance 的一个账户（一个 AccountKey）。
    网络不通时 account() 返回 None，positions() 返回空列表。
    """

    def __init__(self, account: _AccountKey, config: ExchangeConfig):
        if not account.api_key or not account.api_secret:
            raise ValueError("BinanceAdapter 需要有效的 api_key / api_secret")
        self._api_key = account.api_key
        self._api_secret = account.api_secret
        self._host = config.host or "https://fapi.binance.com"
        self._recv_window = int(config.extra.get("recv_window", 5000))
        self._account_key = account
        self._config = config

        methods = set(config.readonly_methods) if config.readonly_methods else set()
        self._readonly_methods = frozenset(methods)

    # ------------------------------------------------------------------
    # BaseExchange 身份
    # ------------------------------------------------------------------

    @property
    def exchange_name(self) -> str:
        return "BINANCE"

    @property
    def readonly_methods(self) -> frozenset[str]:
        return self._readonly_methods

    # ------------------------------------------------------------------
    # Binance 签名 + 请求
    # ------------------------------------------------------------------

    def _signature(self, query_string: str) -> str:
        """Binance HMAC-SHA256 签名。"""
        return hmac.new(
            self._api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _request(self, path: str, params: Optional[dict] = None) -> dict[str, Any]:
        """
        发起 Binance fapi 只读请求。失败时抛异常（由调用方捕获）。
        """
        self._check_readonly(path)
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        params.setdefault("recvWindow", self._recv_window)

        # 拼接排序后的 query string
        query_parts = []
        for k in sorted(params.keys()):
            v = params[k]
            if v is None:
                continue
            query_parts.append(f"{k}={urllib.parse.quote(str(v), safe='')}")
        query_string = "&".join(query_parts)
        sig = self._signature(query_string)
        query_string += f"&signature={sig}"

        url = f"{self._host}{path}?{query_string}"
        req = urllib.request.Request(url, method="GET")
        req.add_header("X-MBX-API-Key", self._api_key)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)

    # ------------------------------------------------------------------
    # BaseExchange 标准只读接口（模型对象）
    # ------------------------------------------------------------------

    def account(self) -> Optional[AccountInfo]:
        """
        获取 Binance USDT-M 账户 → AccountInfo。
        Binance /fapi/v2/account 返回：
          totalWalletBalance, totalUnrealizedProfit, availableBalance,
          assets: [{asset, walletBalance, ...}], positions: [...]
        """
        try:
            resp = self._request("/fapi/v2/account")
        except Exception:
            return None

        # Binance 错误格式：{code: -2015, msg: "..."} 或正常返回
        code = resp.get("code")
        if code is not None and code != 0:
            return None

        # 账户级汇总字段
        total_wallet = _num(resp.get("totalWalletBalance"))
        available = _num(resp.get("availableBalance"))
        unrealised = _num(resp.get("totalUnrealizedProfit"))

        # 账户 ID：Binance 无明确字段，用 ownerName 或 operator
        account_id = str(resp.get("ownerName") or resp.get("operator") or "")

        # 保证金模式：Binance 账户级不直接提供；positions 里有 positionSide
        # 这里简单标记；实际用的时候上层可按需判断
        margin_mode = "unknown"

        return AccountInfo(
            exchange="BINANCE",
            account_id=account_id,
            display_name=self._account_key.name,
            total_equity=total_wallet,
            available=available,
            unrealised_pnl=unrealised,
            margin_mode=margin_mode,
            raw=resp,
        )

    def positions(self) -> list[Position]:
        """
        获取 Binance USDT-M 持仓 → Position 列表。
        Binance /fapi/v2/positionRisk 返回：
          positions: [{symbol, positionAmt, entryPrice, markPrice, ...}]
        过滤：仅返回 positionAmt != 0 的有效持仓。
        """
        try:
            resp = self._request("/fapi/v2/positionRisk")
        except Exception:
            return []

        code = resp.get("code")
        if code is not None and code != 0:
            return []

        data = resp.get("positions") or []
        result: list[Position] = []
        for p in data:
            # 只返回有仓位的（positionAmt != 0）
            amt = _num(p.get("positionAmt"))
            if amt is None or amt == 0:
                continue
            result.append(self._position_from_binance(p))
        return result

    def tickers(self, contracts: Optional[list[str]] = None) -> list[dict[str, Any]]:
        """
        获取 Binance USDT-M 24hr ticker。
        Binance /fapi/v2/ticker/24hr 返回 list of:
          {symbol, lastPrice, markPrice, ...}
        返回内部标准格式 dict 列表。
        """
        try:
            if contracts and len(contracts) == 1:
                # 单合约：直接请求指定 symbol
                path = f"/fapi/v2/ticker/24hr?symbol={contracts[0].replace('_', '')}"
                resp = self._request(path)
                # 单合约返回单个 dict，不是 list
                data = [resp] if isinstance(resp, dict) and "symbol" in resp else []
            else:
                # 全量请求
                resp = self._request("/fapi/v2/ticker/24hr")
                data = resp if isinstance(resp, list) else []
                if contracts:
                    # 过滤
                    wanted = {c.replace("_", "").upper() for c in contracts}
                    data = [t for t in data if t.get("symbol", "").upper() in wanted]
        except Exception:
            return []

        result: list[dict[str, Any]] = []
        for t in data:
            result.append(self._ticker_to_internal(t))
        return result

    # ------------------------------------------------------------------
    # Binance 持仓解析
    # ------------------------------------------------------------------

    @staticmethod
    def _position_from_binance(p: dict) -> Position:
        """
        Binance /fapi/v2/positionRisk 的一条持仓 → Position。
        Binance 持仓字段：
          symbol        : 合约符号，如 "BTCUSDT"
          positionAmt   : 持仓数量（张/币），正=多，负=空
          entryPrice    : 开仓均价
          markPrice     : 标记价
          liquidationPrice : 强平价
          unRealizedProfit : 未实现盈亏
          leverage      : 杠杆
          marginType    : 保证金类型（ISOLATED/CROSSED）
          positionSide  : LONG/SHORT/BOTH（双向/单向持仓模式）
          notional      : 名义价值（USDT），abs(notional) = 杠杆后的头寸大小
        """
        symbol_raw = p.get("symbol") or ""
        # BTCUSDT → BTC_USDT（内部格式）
        contract = BinanceAdapter._binance_symbol_to_internal(symbol_raw)
        symbol = contract.split("_")[0] if contract else ""

        pos_side = (p.get("positionSide") or "").upper()
        amt = _num(p.get("positionAmt")) or 0.0
        size = abs(amt) if amt else None
        mark = _num(p.get("markPrice"))
        entry = _num(p.get("entryPrice"))
        liq = _num(p.get("liquidationPrice"))
        upl = _num(p.get("unRealizedProfit"))
        lever = _num(p.get("leverage"))

        # 方向：positionSide 为 LONG/SHORT/BOTH 时不同处理
        if pos_side == "LONG":
            side = "long"
        elif pos_side == "SHORT":
            side = "short"
        elif pos_side == "BOTH" or pos_side == "":
            side = "long" if amt > 0 else "short" if amt < 0 else "none"
        else:
            side = "long" if amt > 0 else "short" if amt < 0 else "none"

        # Binance 没有单独的每张面值字段；amount 直接是币数（amt 本身就是币数）
        # notional 是名义价值（USDT），可做交叉校验
        amount = abs(amt) if amt else None

        # 保证金：Binance positionRisk 不直接返回单仓保证金
        # 用 notional / leverage 估算
        notional = _num(p.get("notional"))
        margin = None
        if notional is not None and lever and lever > 0:
            margin = abs(notional) / lever

        # ROE
        roe = None
        if margin and margin > 0 and upl is not None:
            roe = upl / margin * 100.0

        # 强平距离
        liq_dist = None
        if mark and liq is not None:
            if side == "long" and liq < mark and mark > 0:
                liq_dist = (mark - liq) / mark * 100.0
            elif side == "short" and liq > mark and mark > 0:
                liq_dist = (liq - mark) / mark * 100.0

        margin_type = (p.get("marginType") or "").upper()
        if margin_type == "CROSSED":
            margin_mode = "全仓"
        elif margin_type == "ISOLATED":
            margin_mode = "逐仓"
        else:
            margin_mode = "未知"

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
            adl_ranking=None,  # Binance 无 ADL 概念
            liq_distance_pct=liq_dist,
            roe_pct=roe,
            initial_margin=margin,
            raw=dict(p) if p else {},
        )

    # ------------------------------------------------------------------
    # Binance ticker → 内部格式
    # ------------------------------------------------------------------

    @staticmethod
    def _ticker_to_internal(t: dict) -> dict[str, Any]:
        """
        Binance /fapi/v2/ticker/24hr 一条 → 内部标准 dict。
        Binance 字段：symbol, lastPrice, markPrice, ...
        """
        symbol_raw = t.get("symbol") or ""
        contract = BinanceAdapter._binance_symbol_to_internal(symbol_raw)
        mark = _num(t.get("markPrice"))
        last = _num(t.get("lastPrice"))

        # Binance 24hr ticker 无 fundingRate / openInterest
        # 需要单独接口；这里先填 0
        return {
            "contract": contract,
            "mark_price": mark or last or 0.0,
            "index_price": 0.0,
            "funding_rate": 0.0,
            "oi": 0.0,
            "last_price": last or 0.0,
        }

    @staticmethod
    def _binance_symbol_to_internal(symbol: str) -> str:
        """
        Binance symbol 格式 "BTCUSDT" → 内部 "BTC_USDT"。
        """
        if not symbol:
            return ""
        s = symbol.upper()
        # 常见 USDT 结尾
        if s.endswith("USDT"):
            base = s[:-4]
            return f"{base}_USDT"
        return s

    # ------------------------------------------------------------------
    # Binance 公共接口扩展（供 watch.py 额外调用）
    # ------------------------------------------------------------------

    def get_funding_rate(self, symbol: str) -> Optional[float]:
        """
        获取指定合约的资金费率。
        Binance /fapi/v2/ticker/fundingRate?symbol=BTCUSDT
        返回 fundingRate（比例，如 0.0001 = 0.01%）。
        """
        try:
            path = f"/fapi/v2/ticker/fundingRate?symbol={symbol}"
            resp = self._request(path)
            if isinstance(resp, list) and resp:
                return _num(resp[0].get("fundingRate"))
            elif isinstance(resp, dict):
                return _num(resp.get("fundingRate"))
        except Exception:
            pass
        return None

    def get_open_interest(self, symbol: str) -> Optional[float]:
        """
        获取指定合约的未平仓量（OI）。
        Binance /fapi/v2/openInterest?symbol=BTCUSDT
        返回 openInterest（张数/币数）。
        """
        try:
            path = f"/fapi/v2/openInterest?symbol={symbol}"
            resp = self._request(path)
            return _num(resp.get("openInterest"))
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # 调试/可观测
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """轻量连通性检测：请求公共接口（无需签名）。"""
        try:
            url = f"{self._host}/fapi/v1/time"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return "serverTime" in data
        except Exception:
            return False
