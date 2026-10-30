#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX 事件合约 BTC 5分钟 MACD 自动交易（买 T+1 轮）
UTC + 北京时间双显示版

策略：
1. 已收盘5分钟K线 T
2. MACD 多头（DIF>DEA 且 HIST>0）且该K线收阳 -> UP -> YES
3. MACD 空头（DIF<DEA 且 HIST<0）且该K线收阴 -> DOWN -> NO
4. 其他（交叉、柱体接近0、K线方向与MACD不一致）-> 不交易

MACD 参数：
fast=6, slow=13, signal=5（可通过命令行调整）

买 T+1 轮：
- 信号来自 T，但下单目标是 expTime > T+5min 的第二个合约（到期 T+10min）
- 结算用 T+2 close vs T+1 close（对应 T+1 合约的到期区间）
- 方向仍用 T 的 MACD 信号

输赢判定：
- UP 信号：T+2 close > T+1 close -> 赢
- DOWN 信号：T+2 close < T+1 close -> 赢
- 相等视为输

资金管理：
- 盈利：连亏清零，投注 = 基础投注额
- 亏损：连亏+1，投注 = 上一笔投注 * 2 + 基础投注额
- 连亏达到 --max-losses（默认10）停止下单
- 投注张数超过 --max-stake（默认1000）停止下单

状态日志：
- 每笔订单下单成功输出 [状态] 已下单
- 每笔订单结算完成输出 [状态] 已结算

事件合约参数：
tdMode = isolated
outcome = yes / no
speedBump = 1
side = buy

注意：
--base 表示合约张数，不代表 USDT 金额。
DRY RUN 默认开启，--live 才实际交易。

依赖：
pip install ccxt pandas requests python-dotenv
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
from decimal import Decimal, ROUND_DOWN

import ccxt
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://www.okx.com"
STATE_FILE = Path(__file__).resolve().parent / "event_trade_state.json"

API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

BEIJING_TZ = timezone(timedelta(hours=8))


# ============================================================
# 时间工具
# ============================================================

def dual_str(ms):
    utc_part = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    bj_part = datetime.fromtimestamp(ms / 1000, tz=BEIJING_TZ).strftime("%H:%M:%S 北京")
    return f"{utc_part} / {bj_part}"


def now_ms():
    return int(time.time() * 1000)


# ============================================================
# 统一状态日志
# ============================================================

def print_status(tag, state, extra=None):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    parts = [
        f"[状态] {ts}",
        f"事件={tag}",
        f"投注={state.get('stake')}",
        f"连亏={state.get('consecutive_losses')}",
        f"待结算={state.get('pending')}",
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


# ============================================================
# 事件合约查询
# ============================================================

def get_live_markets(series_id):
    path = f"/api/v5/public/event-contract/markets?seriesId={series_id}&state=live"
    return _request("GET", path).get("data", [])


def match_contract_by_signal_time(markets, signal_ts_ms, skip=1):
    """
    skip=0: 取最近一个（买本轮）
    skip=1: 跳过最近一个，取第二个（买 T+1 轮）
    """
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


# ============================================================
# 事件合约下单
# ============================================================

def place_event_order(inst_id, outcome, contracts):
    if outcome not in ("yes", "no"):
        raise ValueError(f"非法 outcome: {outcome}")
    if contracts <= 0:
        raise ValueError("下单数量必须大于0")

    path = "/api/v5/trade/order"
    cl_ord_id = hashlib.md5(f"{inst_id}_{outcome}_{contracts}".encode()).hexdigest()[:32]

    payload = {
        "instId": inst_id,
        "tdMode": "isolated",
        "clOrdId": cl_ord_id,
        "side": "buy",
        "ordType": "market",
        "sz": str(contracts),
        "outcome": outcome,
        "speedBump": "1",
    }

    print("\n========== 下单结果 ==========")
    data = _request("POST", path, payload)
    result = data["data"][0]

    if result.get("sCode") != "0":
        raise RuntimeError(f"下单失败: sCode={result.get('sCode')}, sMsg={result.get('sMsg')}")
    return result


# ============================================================
# MACD 信号
# ============================================================

def get_macd_signal(exchange, symbol, fast, slow, signal_period):
    """
    返回 (signal, closed_ts, closed_close)
    signal: "UP" / "DOWN" / None

    规则（基于已收盘K线）：
    - DIF > DEA 且 HIST > 0 且 close >= open -> UP
    - DIF < DEA 且 HIST < 0 且 close <= open -> DOWN
    - 其他 -> None
    """

    limit = max(slow + signal_period + 30, 100)
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe="5m", limit=limit)

    if len(ohlcv) < slow + signal_period + 2:
        return None, (int(ohlcv[-1][0]) if ohlcv else 0), None

    df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])

    ema_fast = df["c"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["c"].ewm(span=slow, adjust=False).mean()

    df["dif"] = ema_fast - ema_slow
    df["dea"] = df["dif"].ewm(span=signal_period, adjust=False).mean()
    df["hist"] = df["dif"] - df["dea"]

    idx = len(df) - 2
    row = df.iloc[idx]

    closed_ts = int(row["ts"])
    closed_close = float(row["c"])

    if pd.isna(row["dif"]) or pd.isna(row["dea"]) or pd.isna(row["hist"]):
        return None, closed_ts, closed_close

    dif = float(row["dif"])
    dea = float(row["dea"])
    hist = float(row["hist"])
    o = float(row["o"])
    c = float(row["c"])

    if dif > dea and hist > 0 and c >= o:
        return "UP", closed_ts, closed_close
    elif dif < dea and hist < 0 and c <= o:
        return "DOWN", closed_ts, closed_close

    return None, closed_ts, closed_close


def get_klines(exchange, symbol, limit=10):
    return exchange.fetch_ohlcv(symbol, timeframe="5m", limit=limit)


# ============================================================
# 状态持久化
# ============================================================

def load_state(base_stake):
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                s = json.load(f)
            return {
                "stake": s.get("stake", base_stake),
                "consecutive_losses": s.get("consecutive_losses", 0),
                "last_processed_ts": s.get("last_processed_ts"),
                "pending": s.get("pending", False),
                "last_signal": s.get("last_signal"),
                "last_signal_ts": s.get("last_signal_ts"),
                "last_stake": s.get("last_stake"),
            }
        except Exception as e:
            print(f"读取状态失败: {e}")
    return {
        "stake": base_stake,
        "consecutive_losses": 0,
        "last_processed_ts": None,
        "pending": False,
        "last_signal": None,
        "last_signal_ts": None,
        "last_stake": None,
    }


def save_state(state):
    out = dict(state)
    out["updated_at"] = datetime.now(timezone.utc).isoformat()
    temp = STATE_FILE.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    temp.replace(STATE_FILE)


# ============================================================
# 结算（买 T+1 轮：T+2 close vs T+1 close）
# ============================================================

def settle_pending(state, exchange, symbol, base_stake, max_losses, max_stake):
    if not state.get("pending"):
        return

    klines = get_klines(exchange, symbol, limit=10)
    if len(klines) < 3:
        return

    signal_ts = state.get("last_signal_ts")
    if signal_ts is None:
        state["pending"] = False
        save_state(state)
        return

    idx = None
    for i, k in enumerate(klines):
        if int(k[0]) == signal_ts:
            idx = i
            break

    if idx is None:
        return

    # 需要 T+1 和 T+2 都已收盘（最后一根是当前未收盘）
    if idx + 2 >= len(klines) - 1:
        return

    t1_close = float(klines[idx + 1][4])
    t2_close = float(klines[idx + 2][4])

    signal = state.get("last_signal")
    last_stake = state.get("last_stake") or base_stake

    if signal == "UP":
        win = t2_close > t1_close
    elif signal == "DOWN":
        win = t2_close < t1_close
    else:
        state["pending"] = False
        save_state(state)
        return

    print(f"\n[结算] 信号={signal}, T+1 close={t1_close}, T+2 close={t2_close} -> {'盈利' if win else '亏损'}")

    if win:
        state["consecutive_losses"] = 0
        state["stake"] = base_stake
        print(f"  连亏清零，下一笔 = {base_stake} 张")
    else:
        state["consecutive_losses"] += 1
        new_stake = last_stake * 2 + base_stake
        state["stake"] = new_stake
        print(f"  连亏={state['consecutive_losses']}, 上一笔={last_stake}, 下一笔={new_stake} 张")

    state["pending"] = False
    state["last_signal"] = None
    state["last_signal_ts"] = None
    state["last_stake"] = None

    if state["consecutive_losses"] >= max_losses:
        print(f"\n!!! 连亏 {max_losses} 次，熔断 !!!")
        state["stake"] = 0
    elif state["stake"] > max_stake:
        print(f"\n!!! 下笔投注 {state['stake']} 超上限 {max_stake}，熔断 !!!")
        state["stake"] = 0

    print_status("已结算", state, {
        "信号": signal,
        "T+1价": t1_close,
        "T+2价": t2_close,
        "结果": "盈利" if win else "亏损",
        "上笔投注": last_stake,
        "下笔投注": state["stake"],
        "是否熔断": "是" if state["stake"] <= 0 else "否",
    })
    save_state(state)


# ============================================================
# 主程序
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="OKX事件合约5分钟MACD自动交易（买T+1轮）")
    parser.add_argument("--series-id", default="BTC-UPDOWN-5MIN")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--fast", type=int, default=6)
    parser.add_argument("--slow", type=int, default=13)
    parser.add_argument("--signal-period", type=int, default=5)
    parser.add_argument("--base", type=float, default=10.0)
    parser.add_argument("--max-losses", type=int, default=10)
    parser.add_argument("--max-stake", type=float, default=1000.0)
    parser.add_argument("--up-outcome", choices=["yes", "no"], default="yes")
    parser.add_argument("--down-outcome", choices=["yes", "no"], default="no")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--poll-interval", type=int, default=15)

    args = parser.parse_args()

    if args.fast < 1 or args.slow < 1 or args.signal_period < 1:
        print("错误：MACD 参数必须>=1"); sys.exit(1)
    if args.fast >= args.slow:
        print("错误：--fast 必须小于 --slow"); sys.exit(1)
    if args.base <= 0:
        print("错误：基础投注必须>0"); sys.exit(1)
    if args.poll_interval < 1:
        print("错误：轮询间隔必须>0"); sys.exit(1)
    if args.max_losses < 1:
        print("错误：--max-losses 必须>=1"); sys.exit(1)
    if args.max_stake <= 0:
        print("错误：--max-stake 必须>0"); sys.exit(1)

    if args.live and not all([API_KEY, SECRET_KEY, PASSPHRASE]):
        print("错误：LIVE需要配置API密钥"); sys.exit(1)

    exchange = ccxt.okx({"enableRateLimit": True})
    state = load_state(args.base)

    print("=" * 65)
    print(f"模式: {'LIVE' if args.live else 'DRY RUN'}")
    print(f"系列: {args.series_id}, 品种: {args.symbol}")
    print(f"MACD: fast={args.fast}, slow={args.slow}, signal={args.signal_period}")
    print(f"基础投注: {args.base}, 当前: {state['stake']}")
    print(f"连亏: {state['consecutive_losses']}/{args.max_losses}")
    print(f"投注上限: {args.max_stake}")
    print(f"待结算: {state['pending']}")
    print("策略: 买 T+1 轮（跳过最近合约）")
    print("结算: T+2 close vs T+1 close")
    print("=" * 65)

    while True:
        try:
            settle_pending(state, exchange, args.symbol, args.base, args.max_losses, args.max_stake)

            if state["consecutive_losses"] >= args.max_losses:
                print("熔断中（连亏上限）"); time.sleep(args.poll_interval); continue
            if state["stake"] <= 0:
                print("投注为0（熔断/超限），跳过"); time.sleep(args.poll_interval); continue

            signal, closed_ts, closed_close = get_macd_signal(
                exchange, args.symbol, args.fast, args.slow, args.signal_period
            )

            if not closed_ts:
                time.sleep(args.poll_interval); continue
            if closed_ts == state["last_processed_ts"]:
                time.sleep(args.poll_interval); continue

            state["last_processed_ts"] = closed_ts
            save_state(state)

            if signal is None:
                print(f"[{dual_str(closed_ts)}] 无信号（MACD交叉/柱体为0/K线方向不一致）")
                time.sleep(args.poll_interval); continue

            print(f"\n[{dual_str(closed_ts)}] MACD信号: {signal}")
            outcome = args.up_outcome if signal == "UP" else args.down_outcome
            print(f"  outcome: {outcome}")

            markets = get_live_markets(args.series_id)
            if not markets:
                print("  无 live 市场"); time.sleep(args.poll_interval); continue

            matched, exp_ms = match_contract_by_signal_time(markets, closed_ts, skip=1)
            if matched is None:
                print("  无 T+1 轮合约（可能只有 1 个 live 合约）")
                time.sleep(args.poll_interval); continue

            inst_id = matched["instId"]
            print(f"  合约(T+1轮): {inst_id}, 到期: {dual_str(exp_ms)}")

            if exp_ms <= now_ms() or matched.get("state") != "live":
                print("  合约非live/已到期"); time.sleep(args.poll_interval); continue

            contracts = int(state["stake"])
            if contracts <= 0:
                print("  张数为0"); time.sleep(args.poll_interval); continue

            result = None
            if not args.live:
                print(f"  [DRY RUN] 将买入 {outcome}, {contracts}张")
            else:
                print(f"  [LIVE] 买入 {outcome}, {contracts}张")
                result = place_event_order(inst_id, outcome, contracts)
                print(f"  <下单成功> ordId={result.get('ordId')}")

            state["pending"] = True
            state["last_signal"] = signal
            state["last_signal_ts"] = closed_ts
            state["last_stake"] = contracts
            save_state(state)

            print_status("已下单", state, {
                "合约": inst_id,
                "方向": outcome,
                "张数": contracts,
                "信号": signal,
                "订单号": result.get("ordId") if result else "DRY_RUN",
            })

        except KeyboardInterrupt:
            print("\n停止"); break
        except Exception as e:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 异常: {e}")

        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()