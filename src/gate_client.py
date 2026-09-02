#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gate_client.py — 只读 Gate.io USDT 永续客户端（多账户）

【只读保证 / 安全契约】
  本文件只调用 FuturesApi 的 GET 方法（list_*/get_*），绝不调用任何
  create_*/cancel_*/update_*/amend_* 写方法。下方 _READONLY_METHODS 白名单
  进一步约束：仅这些方法被允许，且强制 settle='usdt'。物理上不可能误下单。

依赖：gate-api（官方 SDK，自带 apiv4 签名）。
"""
import time
from typing import Any, Optional

import gate_api
from gate_api import ApiClient, Configuration
from gate_api.exceptions import GateApiException

from keystore import AccountKey
from models import AccountInfo

SETTLE = "usdt"

# Gate 限流相关标识（label / 消息关键字）。命中即认为被限流，触发退避。
_RATE_LIMIT_LABELS = {"TOO_MANY_REQUEST", "RATE_LIMIT", "API_FREQUENCY_LIMIT"}
_RATE_LIMIT_HINTS = ("frequency", "too many request", "rate limit", "rate_limit", "429")


class RateLimitError(Exception):
    """Gate 返回限流（429 / frequency limit）时抛出，便于上层退避。"""
    pass

# 允许的只读方法集合（只用这些，写方法一律不出现）
_READONLY_METHODS = {
    "list_futures_accounts",
    "list_positions",
    "list_futures_tickers",
    "get_my_trades",
    "list_futures_account_book",
    "list_futures_contracts",
    "list_futures_orders",      # 只读查看当前委托（核对用）
}


def _to_dicts(obj: Any) -> Any:
    """把 SDK 模型对象 / 列表 递归转成 dict / list[dict]。"""
    if obj is None:
        return obj
    if isinstance(obj, list):
        return [_to_dicts(x) for x in obj]
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return obj.to_dict()
    if isinstance(obj, dict):
        return {k: _to_dicts(v) for k, v in obj.items()}
    return obj


class ReadOnlyGateClient:
    def __init__(self, account: AccountKey, host: str = "https://api.gateio.ws/api/v4"):
        self.account = account
        cfg = Configuration(host=host)
        # 注意：gate-api SDK 用 cfg.key / cfg.secret（不是 api_key/api_secret）
        cfg.key = account.api_key
        cfg.secret = account.api_secret
        self._client = ApiClient(cfg)
        self._futures = gate_api.FuturesApi(self._client)

    def _call(self, method: str, **kwargs: Any) -> Any:
        if method not in _READONLY_METHODS:
            raise PermissionError(f"[只读] 拒绝非白名单方法: {method}")
        fn = getattr(self._futures, method)
        try:
            # 所有 futures 只读方法都需要 settle 作为第一位置参数
            raw = fn(SETTLE, **kwargs)
        except GateApiException as e:
            # 提取 label / status / 原始消息，判断是否为限流
            emsg = str(e).lower()
            label = ""
            try:
                # Gate 错误体形如 {'label': '...', 'message': '...', 'code': n}
                import json as _json
                body = _json.loads(str(e))
                label = str(body.get("label", "")).upper()
            except Exception:
                pass
            if label in _RATE_LIMIT_LABELS or any(h in emsg for h in _RATE_LIMIT_HINTS):
                raise RateLimitError(f"Gate 限流: label={label or '?'} | {e}") from e
            raise  # 其他 Gate 错误（如 INVALID_KEY）原样上浮
        # gate-api 返回的是 SDK 模型对象（无 .get）；统一转成 dict 便于解析
        return _to_dicts(raw)

    # ---- 第1层：资金 / 持仓（拉满）----
    def get_account(self) -> Any:
        """全仓+逐仓账户快照：余额、权益、可用、保证金、未实现盈亏、模式。"""
        return self._call("list_futures_accounts")

    def get_account_model(self, display_name: str = "") -> Optional["AccountInfo"]:
        """get_account() 的 AccountInfo 模型版（兼容全仓/逐仓两种原始结构）。

        纯包装：内部复用 get_account() 的 dict 结果，把归一化逻辑收进工厂，
        上层（watch.py/risk_board.py）不用重复写 unified = acct if isinstance(...)。
        失败/空结果返回 None。
        """
        from models import AccountInfo as _AI

        raw = self.get_account()
        if not raw:
            return None
        # Gate 可能直接返回单 dict（全仓账户），也可能返回 list —— 逐仓时常是 list，
        # 且逐仓账户的 fields 分布在 list 元素里。以“最有引用价值”的那个元素构建模型。
        if isinstance(raw, list):
            acct = raw[0] if raw else {}
        else:
            acct = raw
        return _AI.from_gate_account(dict(acct) if acct else {}, exchange="GATE",
                                     display_name=display_name)

    def get_positions(self) -> list:
        """所有合约持仓，含 liq_price/entry_price/size/leverage/mark_price/margin。"""
        return self._call("list_positions") or []

    def get_positions_models(self, mark_prices: Optional[dict[str, float]] = None) -> list["Position"]:
        """get_positions() 的 Position 模型版。

        mark_prices: 可选的 {contract: mark_price} 字典，用来填充标记价（例如来自
        get_tickers 的结果）。若不传则 Position.mark_price 尽量从持仓项自身读取。
        """
        from models import Position as _P

        raw = self.get_positions()
        mk = mark_prices or {}
        return [_P.from_gate_position(dict(p) if p else {}, mark_price=mk.get(p.get("contract")))
                for p in (raw or [])]

    def get_tickers(self, contract: Optional[str] = None) -> list:
        """ticker：标记价/指数价/资金费/funding_time/未平仓量(OI)。"""
        kwargs = {"contract": contract} if contract else {}
        return self._call("list_futures_tickers", **kwargs) or []

    # ---- 第2/3层：历史与异动 ----
    def get_my_trades(self, contract: Optional[str] = None, limit: int = 100) -> list:
        kwargs = {}
        if contract:
            kwargs["contract"] = contract
        kwargs["limit"] = limit
        return self._call("get_my_trades", **kwargs) or []

    def get_income(self, contract: Optional[str] = None, limit: int = 100,
                   i_type: Optional[str] = None, _from: Optional[int] = None,
                   to: Optional[int] = None) -> list:
        """资金费/手续费/已实现盈亏流水（account_book, type 可过滤）。

        已知缺陷规避: gate-api SDK 给 list_futures_account_book 的 type 参数写了
        非法的默认值 'pv_dnw'，只要不显式覆盖就会被 Gate 拒绝
        (Invalid value for `type` (pv_dnw))。因此本方法在 i_type=None 时，
        分别拉取账本核心合法类型并合并(绕过 SDK 坏默认)，拿到全部收支流水供
        每日账本按 type 汇总；i_type 指定时则直接使用(合法值见 Gate 文档:
        dnw/pnl/fee/refr/fund/point_dnw/point_fee/point_refr/bonus_offset)。

        时间窗 _from/to 为 Unix 秒(UTC)，用于按 UTC 日精确切分账本，
        与交易所资金费/盈亏结算口径一致。传了即精确过滤；不传则拉最近 limit 条。
        分页：单类型可能超 limit 条，循环 offset 拉全窗口，避免静默截断。
        """
        def _one_type(t: str) -> list:
            out: list = []
            offset = 0
            while True:
                kw = {"limit": limit, "type": t, "offset": offset}
                if contract:
                    kw["contract"] = contract
                if _from is not None:
                    kw["_from"] = _from
                if to is not None:
                    kw["to"] = to
                page = self._call("list_futures_account_book", **kw) or []
                if not page:
                    break
                out.extend(page)
                # 已拉满窗口或不足一页则停止
                if len(page) < limit:
                    break
                offset += limit
                # 安全阀：单类型最多 50 页，防异常死循环
                if offset >= limit * 50:
                    break
            return out

        if i_type is not None:
            return _one_type(i_type)
        # i_type is None: 合并多个合法 type，每层独立容错(单类失败不影响整体)
        out: list = []
        for t in ("pnl", "fee", "fund", "dnw", "refr"):
            try:
                out.extend(_one_type(t))
            except Exception:
                continue
        return out

    def get_open_orders(self, contract: Optional[str] = None) -> list:
        """只读查看是否有挂单（核对用，不改动）。"""
        kwargs = {"status": "open"}
        if contract:
            kwargs["contract"] = contract
        return self._call("list_futures_orders", **kwargs) or []

    def ping(self) -> bool:
        try:
            self._call("list_futures_tickers", contract="BTC_USDT")
            return True
        except Exception:
            return False


class MultiAccountWatcher:
    def __init__(self, accounts: list[AccountKey]):
        self.accounts = accounts
        self.clients = {a.name: ReadOnlyGateClient(a) for a in accounts}

    def __len__(self):
        return len(self.clients)

    def snapshot_all(self) -> dict:
        out = {"ts": int(time.time() * 1000), "accounts": {}}
        for name, cli in self.clients.items():
            try:
                acct = cli.get_account()
                positions = cli.get_positions()
                tickers = {t.get("contract"): t for t in (cli.get_tickers() or [])}
                out["accounts"][name] = {
                    "ok": True,
                    "account": acct,
                    "positions": positions,
                    "tickers": tickers,
                }
            except Exception as e:
                out["accounts"][name] = {"ok": False, "error": str(e)}
        return out
