#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX 事件合约 BTC 5分钟布林线自动交易（T+0 限价 ≤0.66，3分钟不成交撤单）
UTC + 北京时间双显示版
—— 单链 + 同时仅允许 1 笔未结算订单版

策略：
1. 已收盘5分钟K线 T
2. 实体完全在布林中轨上方 -> UP -> YES
3. 实体完全在布林中轨下方 -> DOWN -> NO
4. 实体穿越或触碰中轨 -> 不交易

T+0 限价轮询模式：
- 信号来自 T，下单目标是 expTime > T+5min 的**第一个**合约（到期 T+5min）
- 限价单，价格 ≤ --limit-price（默认 0.66）
- 下单后进入 pending_orders，脚本每 --check-interval 秒（默认10）查询一次成交状态
- 成交则保留，继续等待结算
- 超过 --timeout（默认180秒）仍未成交，主动撤单，本笔作废
- 撤单后链状态不变，下一根信号可重新尝试

单链 + 单订单：
- 只有一条 StakeChain，记录 stake / losses / wins
- 同一时间最多只允许 1 笔未结算订单
- 上一笔未结算时，后续信号一律跳过
- 上一笔结算完成后，才允许下一笔下单
- 单链连亏达到 --max-losses 时，链重置为 base

结算（T+0）：
- T+1 close vs T close（近似）
- UP: T+1 close > T close -> 赢
- DOWN: T+1 close < T close -> 赢
"""

import argparse
import hashlib
import hmac
import base64
import json
import os
import sys
import time

from datetime import datetime, timezone, timedelta
from pathlib import Path

import ccxt
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://www.okx.com"
STATE_FILE = Path(__file__).resolve().parent / "5m_boll_t0_limit_poll_state.json"

API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

BEIJING_TZ = timezone(timedelta(hours=8))


# ============================================================
# 工具
# ============================================================

def dual_str(ms):
    utc_part = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    bj_part = datetime.fromtimestamp(ms / 1000, tz=BEIJING_TZ).strftime("%H:%M:%S 北京")
    return f"{utc_part} / {bj_part}"


def now_ms():
    return int(time.time() * 1000)


def new_chain(base_stake):
    return {"stake": float(base_stake), "losses": 0, "wins": 0}


def chain_on_win(chain, base_stake):
    chain["losses"] = 0
    chain["wins"] += 1
    chain["stake"] = float(base_stake)


def chain_on_loss(chain, base_stake, max_losses):
    chain["losses"] += 1
    chain["wins"] = 0
    peak = chain["losses"]
    if chain["losses"] >= max_losses:
        chain["losses"] = 0
        chain["stake"] = float(base_stake)
    else:
        chain["stake"] = chain["stake"] * 2 + float(base_stake)
    return peak


def print_status(tag, state, extra=None):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    c = state["chain"]
    chain_str = f"stake={c['stake']:.2f},L={c['losses']},W={c['wins']}"
    parts = [
        f"[状态] {ts}",
        f"事件={tag}",
        f"链={{{chain_str}}}",
        f"待结算={len(state['pending_orders'])}",
    ]
    if extra:
        for k, v in extra.items():
            parts.append(f"{k}={v}")
    print("  ".join(parts))


# ============================================================
# OKX API
# ============================================================

def _timestamp():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _sign(ts, method, path, body=""):
    mac = hmac.new(SECRET_KEY.encode(), (ts + method.upper() + path + body).encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def _headers(method, path, body=""):
    ts = _timestamp()
    return {
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": _sign(ts, method, path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
    }


def _request(method, path, payload=None):
    method = method.upper()
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if payload is not None else ""
    headers = _headers(method, path, body)
    url = BASE_URL + path

    if method == "GET":
        resp = requests.get(url, headers=headers, timeout=15)
    else:
        resp = requests.post(url, headers=headers, data=body.encode("utf-8"), timeout=15)

    resp.raise_for_status()
    data = resp.json()

    if data.get("code") != "0":
        detail = ""
        if data.get("data"):
            item = data["data"][0]
            detail = f" | sCode={item.get('sCode')} | sMsg={item.get('sMsg')}"
        raise RuntimeError(f"API错误 [{data.get('code')}]: {data.get('msg')}{detail}")
    return data


def get_live_markets(series_id):
    path = f"/api/v5/public/event-contract/markets?seriesId={series_id}&state=live"
    return _request("GET", path).get("data", [])


def match_contract_by_signal_time(markets, signal_ts_ms, skip=0):
    candidates = []
    for m in markets:
        exp = m.get("expTime")
        if not exp:
            continue
        try:
            exp_ms = int(exp)
        except (ValueError, TypeError):
            continue
        if exp_ms > signal_ts_ms:
            candidates.append((exp_ms, m))
    candidates.sort(key=lambda x: x[0])
    if len(candidates) <= skip:
        return None, None
    return candidates[skip][1], candidates[skip][0]


def place_event_order_limit(inst_id, outcome, contracts, limit_price):
    """普通限价单，挂单等待成交"""
    if outcome not in ("yes", "no"):
        raise ValueError(f"非法 outcome: {outcome}")
    if contracts <= 0:
        raise ValueError("下单数量必须大于0")

    path = "/api/v5/trade/order"
    cl_ord_id = hashlib.md5(f"{inst_id}_{outcome}_{contracts}_{limit_price}_{now_ms()}".encode()).hexdigest()[:32]

    payload = {
        "instId": inst_id,
        "tdMode": "isolated",
        "clOrdId": cl_ord_id,
        "side": "buy",
        "ordType": "limit",
        "px": str(limit_price),
        "sz": str(contracts),
        "outcome": outcome,
        "speedBump": "1",
    }

    print("\n========== 限价挂单 ==========")
    data = _request("POST", path, payload)
    result = data["data"][0]
    if result.get("sCode") != "0":
        raise RuntimeError(f"下单失败: sCode={result.get('sCode')}, sMsg={result.get('sMsg')}")
    return result


def cancel_event_order(inst_id, ord_id):
    """撤销事件合约订单"""
    path = "/api/v5/trade/cancel-order"
    payload = {"instId": inst_id, "ordId": ord_id}
    try:
        data = _request("POST", path, payload)
        result = data["data"][0]
        if result.get("sCode") != "0":
            print(f"  [撤单返回] sCode={result.get('sCode')}, sMsg={result.get('sMsg')}")
            return False
        return True
    except Exception as e:
        print(f"  [撤单异常] {e}")
        return False


# ============================================================
# 布林信号
# ============================================================

def get_boll_signal(exchange, symbol, period, std_mult):
    limit = max(period + 30, 100)
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe="5m", limit=limit)
    if len(ohlcv) < period + 2:
        return None, (int(ohlcv[-1][0]) if ohlcv else 0), None

    df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
    df["middle"] = df["c"].rolling(period).mean()
    idx = len(df) - 2
    row = df.iloc[idx]
    closed_ts = int(row["ts"])
    closed_close = float(row["c"])
    if pd.isna(row["middle"]):
        return None, closed_ts, closed_close

    body_low = min(float(row["o"]), float(row["c"]))
    body_high = max(float(row["o"]), float(row["c"]))
    middle = float(row["middle"])
    if body_low > middle:
        return "UP", closed_ts, closed_close
    elif body_high < middle:
        return "DOWN", closed_ts, closed_close
    return None, closed_ts, closed_close


def get_klines(exchange, symbol, limit=20):
    return exchange.fetch_ohlcv(symbol, timeframe="5m", limit=limit)


# ============================================================
# 状态
# ============================================================

def load_state(base_stake):
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                s = json.load(f)
            pending = s.get("pending_orders", [])
            if len(pending) > 1:
                pending = pending[:1]
            if "chain" in s and isinstance(s["chain"], dict):
                chain = s["chain"]
            else:
                chain = new_chain(float(s.get("stake", base_stake)))
            return {"chain": chain, "pending_orders": pending, "last_processed_ts": s.get("last_processed_ts")}
        except Exception as e:
            print(f"读取状态失败: {e}")
    return {"chain": new_chain(base_stake), "pending_orders": [], "last_processed_ts": None}


def save_state(state):
    out = dict(state)
    out["updated_at"] = datetime.now(timezone.utc).isoformat()
    temp = STATE_FILE.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    temp.replace(STATE_FILE)


# ============================================================
# 轮询成交状态（核心新逻辑）
# ============================================================

def poll_order_fill(state, exchange, symbol):
    """
    对 pending 中的限价单每 10 秒查一次成交状态。
    - 已成交：标记 filled，保留 pending 等待 T+1 收盘结算
    - 超时未成交：撤单，从 pending 移除，链不变
    - 部分成交：视为已成交（简化处理），按实际成交张数记录
    返回 True 表示本轮做了操作（调用方应 continue 跳过后续下单）
    """
    if not state["pending_orders"]:
        return False

    order = state["pending_orders"][0]
    ord_id = order.get("ord_id")
    if not ord_id or ord_id == "DRY_RUN":
        # dry run 直接标记为已成交，进入等待结算阶段
        if not order.get("_dry_filled"):
            order["_dry_filled"] = True
            order["filled_contracts"] = order["contracts"]
            print(f"  [DRY RUN] 模拟成交 {order['filled_contracts']} 张")
        return False

    elapsed = now_ms() - order.get("created_at_ms", now_ms())
    timeout_ms = order.get("timeout_ms", 180_000)

    try:
        fetched = exchange.fetch_order(ord_id, symbol)
    except Exception as e:
        print(f"  [轮询异常] {e}")
        return False

    status = fetched.get("status")
    filled = fetched.get("filled") or fetched.get("info", {}).get("fillSz")

    if status == "closed" and filled and float(filled) > 0:
        order["filled_contracts"] = int(float(filled))
        order["_filled"] = True
        print(f"  [成交] ordId={ord_id}, 成交 {order['filled_contracts']} 张，等待 T+1 收盘结算")
        save_state(state)
        return True

    if status == "canceled":
        print(f"  [已取消] ordId={ord_id}，移出 pending")
        state["pending_orders"] = []
        save_state(state)
        return True

    if elapsed >= timeout_ms:
        print(f"  [超时 {elapsed//1000}s] 撤单 ordId={ord_id}")
        cancel_event_order(order["inst_id"], ord_id)
        state["pending_orders"] = []
        save_state(state)
        return True

    # 还在挂单中，未到超时
    remain = (timeout_ms - elapsed) // 1000
    print(f"  [挂单中] ordId={ord_id}, 剩余 {remain}s 超时")
    return False


# ============================================================
# 结算：T+0 -> T+1 close vs T close
# ============================================================

def _find_idx(klines, target_ts):
    for i, k in enumerate(klines):
        if int(k[0]) == int(target_ts):
            return i
    return None


def settle_pending_orders(state, exchange, symbol, base_stake, max_losses):
    """
    只结算已成交（_filled 或 _dry_filled）的订单。
    未成交的订单由 poll_order_fill 处理。
    """
    if not state["pending_orders"]:
        return

    order = state["pending_orders"][0]
    if not (order.get("_filled") or order.get("_dry_filled")):
        return  # 还没成交，等轮询

    klines = get_klines(exchange, symbol, limit=40)
    if len(klines) < 2:
        return
    closed_count = len(klines) - 1

    signal_ts = order.get("signal_ts")
    idx = _find_idx(klines, signal_ts)
    if idx is None:
        age_ms = now_ms() - int(order.get("created_at_ms", now_ms()))
        if age_ms > 30 * 60 * 1000:
            print(f"[结算丢弃] 信号K线不在窗口: {order.get('inst_id')}")
            state["pending_orders"] = []
            save_state(state)
        return

    if idx + 1 >= closed_count:
        return  # T+1 还没收盘

    t_close = float(klines[idx][4])
    t1_close = float(klines[idx + 1][4])
    signal = order["signal"]

    if signal == "UP":
        win = t1_close > t_close
    elif signal == "DOWN":
        win = t1_close < t_close
    else:
        state["pending_orders"] = []
        save_state(state)
        return

    chain = state["chain"]
    last_stake = float(order["stake"])

    print(f"\n[结算] 信号={signal} inst={order.get('inst_id')}")
    print(f"  T close={t_close}, T+1 close={t1_close} -> {'盈利' if win else '亏损'}")

    if win:
        chain_on_win(chain, base_stake)
        result_str = "WIN"
    else:
        peak = chain_on_loss(chain, base_stake, max_losses)
        result_str = "LOSS"
        if peak >= max_losses:
            print(f"  连亏 {peak} 次触顶，链重置为 base={base_stake}")
        else:
            print(f"  连亏={chain['losses']}, 下一笔 stake={chain['stake']:.2f}")

    print_status("已结算", state, {
        "信号": signal, "T价": t_close, "T+1价": t1_close,
        "结果": result_str, "上笔stake": last_stake, "新stake": chain["stake"],
    })

    state["pending_orders"] = []
    save_state(state)


# ============================================================
# 下单：限价挂单，进入 pending 后由轮询处理
# ============================================================

def try_place_order(state, exchange, args, signal, closed_ts):
    if state["pending_orders"]:
        print(f"  [占用] 已有未结算订单，跳过")
        return False

    chain = state["chain"]
    stake = float(chain["stake"])
    if stake <= 0 or stake > args.max_stake:
        print(f"  stake={stake} 不可下单")
        return False

    outcome = args.up_outcome if signal == "UP" else args.down_outcome
    print(f"  stake={stake}, outcome={outcome}, 限价={args.limit_price}")

    markets = get_live_markets(args.series_id)
    if not markets:
        print("  无 live 市场")
        return False

    matched, exp_ms = match_contract_by_signal_time(markets, closed_ts, skip=0)
    if matched is None:
        print("  无 T 轮合约")
        return False

    inst_id = matched["instId"]
    print(f"  合约(T轮): {inst_id}, 到期: {dual_str(exp_ms)}")

    if exp_ms <= now_ms() or matched.get("state") != "live":
        print("  合约非live/已到期")
        return False

    contracts = int(stake)
    if contracts <= 0:
        print("  张数为0")
        return False

    ord_id = None
    if not args.live:
        print(f"  [DRY RUN] 限价挂单 {outcome}, {contracts}张 @ {args.limit_price}")
        ord_id = "DRY_RUN"
    else:
        print(f"  [LIVE] 限价挂单 {outcome}, {contracts}张 @ {args.limit_price}")
        result = place_event_order_limit(inst_id, outcome, contracts, args.limit_price)
        ord_id = result.get("ordId")
        print(f"  <挂单成功> ordId={ord_id}")

    order = {
        "inst_id": inst_id,
        "ord_id": ord_id,
        "outcome": outcome,
        "signal": signal,
        "signal_ts": int(closed_ts),
        "stake": stake,
        "contracts": contracts,
        "limit_price": args.limit_price,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_at_ms": now_ms(),
        "exp_time_ms": int(exp_ms),
        "timeout_ms": args.timeout * 1000,
        "_filled": False,
        "_dry_filled": False,
    }
    state["pending_orders"].append(order)

    print_status("已挂单", state, {
        "合约": inst_id, "方向": outcome, "张数": contracts,
        "限价": args.limit_price, "订单号": ord_id,
        "轮询间隔": f"{args.check_interval}s", "超时": f"{args.timeout}s",
    })
    save_state(state)
    return True


# ============================================================
# 主程序
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="OKX事件合约5分钟布林线T+0限价轮询（3分钟不成交撤单）")
    parser.add_argument("--series-id", default="BTC-UPDOWN-5MIN")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--period", type=int, default=20)
    parser.add_argument("--std", type=float, default=2.0)
    parser.add_argument("--base", type=float, default=10.0)
    parser.add_argument("--max-losses", type=int, default=10)
    parser.add_argument("--max-stake", type=float, default=1000.0)
    parser.add_argument("--limit-price", type=float, default=0.66)
    parser.add_argument("--check-interval", type=int, default=10, help="轮询间隔秒数，默认10")
    parser.add_argument("--timeout", type=int, default=180, help="限价单超时秒数，默认180（3分钟）")
    parser.add_argument("--up-outcome", choices=["yes", "no"], default="yes")
    parser.add_argument("--down-outcome", choices=["yes", "no"], default="no")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--poll-interval", type=int, default=15, help="主循环轮询间隔")
    args = parser.parse_args()

    if args.period < 2: sys.exit("周期>=2")
    if args.base <= 0: sys.exit("base>0")
    if not (0.01 <= args.limit_price <= 0.99): sys.exit("limit-price 0.01~0.99")
    if args.check_interval < 1: sys.exit("check-interval>=1")
    if args.timeout < 10: sys.exit("timeout>=10")
    if args.live and not all([API_KEY, SECRET_KEY, PASSPHRASE]):
        sys.exit("LIVE需要API密钥")

    exchange = ccxt.okx({"enableRateLimit": True})
    state = load_state(args.base)

    print("=" * 65)
    print(f"模式: {'LIVE' if args.live else 'DRY RUN'}")
    print(f"系列: {args.series_id}, 品种: {args.symbol}")
    print(f"布林: 周期={args.period}, 标准差={args.std}")
    print(f"基础投注: {args.base}, 限价: {args.limit_price}")
    print(f"单链: stake={state['chain']['stake']:.2f}, L={state['chain']['losses']}, W={state['chain']['wins']}")
    print(f"待结算: {len(state['pending_orders'])}（最多 1 笔）")
    print(f"连亏重置: {args.max_losses}, 单笔上限: {args.max_stake}")
    print(f"限价轮询: 每 {args.check_interval}s 查一次，{args.timeout}s 未成交撤单")
    print("=" * 65)

    while True:
        try:
            # 1) 轮询 pending 限价单的成交状态
            poll_order_fill(state, exchange, args.symbol)

            # 2) 结算已成交且 T+1 已收盘的订单
            settle_pending_orders(state, exchange, args.symbol, args.base, args.max_losses)

            # 3) 仍有 pending（挂单中或等待结算）-> 不下新单
            if state["pending_orders"]:
                time.sleep(args.check_interval)
                continue

            # 4) 链可下注检查
            stake_now = float(state["chain"]["stake"])
            if stake_now <= 0 or stake_now > args.max_stake:
                print(f"当前 stake={stake_now} 不可下单")
                time.sleep(args.poll_interval)
                continue

            # 5) 信号
            signal, closed_ts, closed_close = get_boll_signal(exchange, args.symbol, args.period, args.std)
            if not closed_ts:
                time.sleep(args.poll_interval)
                continue
            if closed_ts == state["last_processed_ts"]:
                time.sleep(args.poll_interval)
                continue

            state["last_processed_ts"] = closed_ts
            save_state(state)

            if signal is None:
                print(f"[{dual_str(closed_ts)}] 无信号")
                time.sleep(args.poll_interval)
                continue

            print(f"\n[{dual_str(closed_ts)}] 信号: {signal}")
            try_place_order(state, exchange, args, signal, closed_ts)

        except KeyboardInterrupt:
            print("\n停止")
            break
        except Exception as e:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 异常: {e}")

        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()