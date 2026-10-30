#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OKX 事件合约 布林线 自动化交易脚本（UTC + 北京时间双显示版）
- 信号：布林线（默认 20 周期、2 倍标准差），基于已收盘 5m K 线
    * 实体完全在中轨上方 -> UP (LONG)
    * 实体完全在中轨下方 -> DOWN (SHORT)
    * 实体跨过/贴住中轨（穿线那根）-> 无信号，不投注
- 合约匹配：根据 expTime 筛选到期时间最接近且晚于信号的 live 合约
- 时间显示：同时显示 UTC 和北京时间（UTC+8），内部比较用毫秒时间戳
- 安全：默认 dry-run，需显式 --live 才实际下单

前置：
1. pip install ccxt pandas requests python-dotenv
2. .env 设置 OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE
3. API Key 仅开启读取+交易，不要开提币
4. 账户需 KYC Level 2
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
STATE_FILE = Path("event_trade_state.json")

API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

BEIJING_TZ = timezone(timedelta(hours=8))


# ============================================================
# 时间工具（UTC + 北京时间双显示）
# ============================================================
def utc_str(ms):
    """毫秒时间戳 -> 'YYYY-MM-DD HH:MM:SS UTC'"""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def bj_str(ms):
    """毫秒时间戳 -> 'YYYY-MM-DD HH:MM:SS 北京'"""
    return datetime.fromtimestamp(ms / 1000, tz=BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S 北京")


def dual_str(ms):
    """毫秒时间戳 -> 'YYYY-MM-DD HH:MM:SS UTC / HH:MM:SS 北京'"""
    utc_part = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    bj_part = datetime.fromtimestamp(ms / 1000, tz=BEIJING_TZ).strftime("%H:%M:%S 北京")
    return f"{utc_part} / {bj_part}"


# ============================================================
# 签名与请求
# ============================================================
def _timestamp():
    now = datetime.now(timezone.utc)
    return now.strftime('%Y-%m-%dT%H:%M:%S.') + f'{now.microsecond // 1000:03d}Z'


def _sign(ts, method, path, body=""):
    message = ts + method.upper() + path + body
    mac = hmac.new(SECRET_KEY.encode(), message.encode(), hashlib.sha256)
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
    body = json.dumps(payload) if payload else ""
    headers = _headers(method, path, body)
    url = BASE_URL + path
    if method.upper() == "GET":
        resp = requests.get(url, headers=headers, timeout=15)
    else:
        resp = requests.post(url, headers=headers, data=body, timeout=15)
    data = resp.json()
    if data.get("code") != "0":
        raise RuntimeError(f"API 错误 [{data.get('code')}]: {data.get('msg')}")
    return data


# ============================================================
# 事件合约查询与匹配
# ============================================================
def get_live_markets(series_id):
    path = f"/api/v5/public/event-contract/markets?seriesId={series_id}&state=live"
    data = _request("GET", path)
    return data.get("data", [])


def match_contract_by_signal_time(markets, signal_ts_ms):
    """
    选 expTime 晚于 signal_ts_ms 且最接近的合约。
    返回 (market_dict, expTime_ms) 或 (None, None)。
    """
    candidates = []
    for m in markets:
        exp = m.get("expTime")
        if exp is None:
            continue
        try:
            exp_ms = int(exp)
        except (ValueError, TypeError):
            continue
        if exp_ms > signal_ts_ms:
            candidates.append((exp_ms, m))

    if not candidates:
        return None, None

    candidates.sort(key=lambda x: x[0])
    best_exp, best_market = candidates[0]
    return best_market, best_exp


def place_event_order(inst_id, outcome, sz_usdt):
    path = "/api/v5/trade/order"
    payload = {
        "instId": inst_id,
        "tdMode": "cash",
        "side": "buy",
        "ordType": "market",
        "sz": str(sz_usdt),
        "outcome": outcome,
        "speedBump": "1",
    }
    data = _request("POST", path, payload)
    return data["data"][0]


# ============================================================
# 布林线信号
# ============================================================
def get_boll_signal(exchange, symbol, period, std_mult):
    """
    基于已收盘 5m K 线计算布林线，判断方向：
    - 实体完全在中轨上方 -> "UP"
    - 实体完全在中轨下方 -> "DOWN"
    - 实体跨过/贴住中轨（穿线那根）-> None（无信号）
    返回 (signal, closed_ts)。
    """
    # 多取一些数据用于预热，period + 30 够用
    limit = max(period + 30, 100)
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe="5m", limit=limit)
    if len(ohlcv) < period + 2:
        return None, int(ohlcv[-1][0]) if ohlcv else 0

    df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])

    df["middle"] = df["c"].rolling(period).mean()
    df["std"] = df["c"].rolling(period).std(ddof=0)
    df["upper"] = df["middle"] + std_mult * df["std"]
    df["lower"] = df["middle"] - std_mult * df["std"]

    # 使用倒数第 2 根（最后一根是尚未收盘的当前 K 线）
    idx = len(df) - 2
    closed_ts = int(df.iloc[idx]["ts"])
    row = df.iloc[idx]

    if pd.isna(row["middle"]):
        return None, closed_ts

    body_low = min(float(row["o"]), float(row["c"]))
    body_high = max(float(row["o"]), float(row["c"]))
    middle = float(row["middle"])

    if body_low > middle:
        return "UP", closed_ts
    elif body_high < middle:
        return "DOWN", closed_ts
    else:
        # 穿线那根：实体跨过/贴住中轨 -> 无信号
        return None, closed_ts


# ============================================================
# 状态持久化
# ============================================================
def load_state(base_stake):
    if STATE_FILE.exists():
        with STATE_FILE.open("r", encoding="utf-8") as f:
            s = json.load(f)
        return s.get("stake", base_stake), s.get("consecutive_losses", 0)
    return base_stake, 0


def save_state(stake, consecutive_losses):
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump({
            "stake": stake,
            "consecutive_losses": consecutive_losses,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, f, ensure_ascii=False, indent=2)


# ============================================================
# 主循环
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="OKX 事件合约 布林线 自动交易（UTC + 北京双显示）")
    parser.add_argument("--series-id", default="BTC-UPDOWN-5MIN")
    parser.add_argument("--symbol", default="BTC/USDT")
    # === 布林线参数（替换原 MACD 参数） ===
    parser.add_argument("--period", type=int, default=20, help="布林线周期，默认 20")
    parser.add_argument("--std", type=float, default=2.0, help="标准差倍数，默认 2.0")
    # === 投注参数 ===
    parser.add_argument("--base", type=float, default=10.0)
    parser.add_argument("--max-losses", type=int, default=10)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--poll-interval", type=int, default=15)
    args = parser.parse_args()

    if args.live and not all([API_KEY, SECRET_KEY, PASSPHRASE]):
        print("错误：--live 需要 .env 配置 OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE")
        sys.exit(1)

    exchange = ccxt.okx({"enableRateLimit": True})
    stake, consecutive_losses = load_state(args.base)

    print("=" * 60)
    print(f"模式: {'🔴 LIVE' if args.live else '🟢 DRY RUN'}")
    print(f"系列: {args.series_id}")
    print(f"布林线: 周期={args.period}, 标准差={args.std}")
    print(f"当前 stake: {stake} USDT | 连亏: {consecutive_losses}")
    print(f"时间显示: UTC / 北京时间(UTC+8)")
    print("=" * 60 + "\n")

    last_processed_ts = None

    while True:
        try:
            signal, closed_ts = get_boll_signal(exchange, args.symbol, args.period, args.std)

            if closed_ts == last_processed_ts:
                time.sleep(args.poll_interval)
                continue
            last_processed_ts = closed_ts

            if signal is None:
                print(f"[{dual_str(closed_ts)}] 无明确信号（穿线或指标未预热）")
                time.sleep(args.poll_interval)
                continue

            print(f"[{dual_str(closed_ts)}] 布林线信号: {signal}")

            markets = get_live_markets(args.series_id)
            if not markets:
                print(f"  无 live 市场")
                time.sleep(args.poll_interval)
                continue

            matched, exp_ms = match_contract_by_signal_time(markets, closed_ts)
            if matched is None:
                print(f"  未找到匹配合约（信号时间 {dual_str(closed_ts)}）")
                print(f"  当前 live 合约到期时间：")
                for m in markets:
                    e = m.get("expTime")
                    e_str = dual_str(int(e)) if e and str(e).isdigit() else "?"
                    print(f"    {m.get('instId')} -> 到期 {e_str}")
                time.sleep(args.poll_interval)
                continue

            inst_id = matched["instId"]
            print(f"  匹配合约: {inst_id}")
            print(f"  到期时间: {dual_str(exp_ms)}")

            if not args.live:
                print(f"  [DRY RUN] 将下单 outcome={signal}, sz={stake} USDT")
            else:
                print(f"  下单: outcome={signal}, sz={stake} USDT")
                result = place_event_order(inst_id, signal, stake)
                print(f"  下单成功 ordId={result.get('ordId')}")

        except Exception as e:
            print(f"  异常: {e}")

        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()