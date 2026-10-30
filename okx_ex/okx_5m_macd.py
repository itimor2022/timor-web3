
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX 事件合约 MACD 自动交易脚本
UTC + 北京时间双显示版

策略：
MACD (6,13,5)
5分钟已收盘K线

柱体 > 0 : UP
柱体 < 0 : DOWN
柱体 = 0 : 无信号

事件合约下单：
tdMode = isolated
outcome = yes / no
speedBump = 1
side = buy

安全：
默认 DRY RUN
使用 --live 才会实际下单

安装：
pip install ccxt pandas requests python-dotenv

.env：
OKX_API_KEY=你的API_KEY
OKX_SECRET_KEY=你的SECRET_KEY
OKX_PASSPHRASE=你的PASSPHRASE

注意：
--base 是合约下单数量 sz，不是 USDT 金额。
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

# MACD单独使用状态文件，避免与布林策略冲突
STATE_FILE = Path("event_trade_state_macd.json")

API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

BEIJING_TZ = timezone(timedelta(hours=8))


# ============================================================
# 时间工具
# ============================================================

def utc_str(ms):
    return datetime.fromtimestamp(
        ms / 1000,
        tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")


def bj_str(ms):
    return datetime.fromtimestamp(
        ms / 1000,
        tz=BEIJING_TZ
    ).strftime("%Y-%m-%d %H:%M:%S 北京")


def dual_str(ms):

    utc_part = utc_str(ms)

    bj_part = datetime.fromtimestamp(
        ms / 1000,
        tz=BEIJING_TZ
    ).strftime("%H:%M:%S 北京")

    return f"{utc_part} / {bj_part}"


def now_ms():
    return int(time.time() * 1000)


# ============================================================
# OKX签名与请求
# ============================================================

def _timestamp():

    now = datetime.now(timezone.utc)

    return (
        now.strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{now.microsecond // 1000:03d}Z"
    )


def _sign(ts, method, path, body=""):

    message = (
        ts
        + method.upper()
        + path
        + body
    )

    mac = hmac.new(
        SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256
    )

    return base64.b64encode(
        mac.digest()
    ).decode()


def _headers(method, path, body=""):

    ts = _timestamp()

    return {
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": _sign(
            ts,
            method,
            path,
            body
        ),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
    }


def _request(method, path, payload=None):

    method = method.upper()

    body = (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":")
        )
        if payload is not None else ""
    )

    headers = _headers(
        method,
        path,
        body
    )

    url = BASE_URL + path

    if method == "GET":

        resp = requests.get(
            url,
            headers=headers,
            timeout=15
        )

    else:

        resp = requests.post(
            url,
            headers=headers,
            data=body.encode("utf-8"),
            timeout=15
        )

    resp.raise_for_status()

    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(
            f"OKX返回非JSON数据: {resp.text[:500]}"
        )

    if data.get("code") != "0":

        detail = ""

        if data.get("data"):

            item = data["data"][0]

            detail = (
                f" | sCode={item.get('sCode')}"
                f" | sMsg={item.get('sMsg')}"
            )

        raise RuntimeError(
            f"API错误 [{data.get('code')}]: "
            f"{data.get('msg')}{detail}"
        )

    return data


# ============================================================
# 查询事件合约
# ============================================================

def get_live_markets(series_id):

    path = (
        "/api/v5/public/event-contract/markets"
        f"?seriesId={series_id}&state=live"
    )

    data = _request("GET", path)

    return data.get("data", [])


def match_contract_by_signal_time(
    markets,
    signal_ts_ms
):

    candidates = []

    for market in markets:

        exp = market.get("expTime")

        if exp is None:
            continue

        try:

            exp_ms = int(exp)

        except (ValueError, TypeError):

            continue

        if exp_ms > signal_ts_ms:

            candidates.append(
                (exp_ms, market)
            )

    if not candidates:

        return None, None

    candidates.sort(
        key=lambda x: x[0]
    )

    best_exp, best_market = candidates[0]

    return best_market, best_exp


# ============================================================
# 策略信号转换
# ============================================================

def convert_outcome(
    signal,
    up_outcome,
    down_outcome
):

    if signal == "UP":
        return up_outcome

    if signal == "DOWN":
        return down_outcome

    return None


# ============================================================
# 事件合约下单
# ============================================================

def place_event_order(
    inst_id,
    outcome,
    sz,
    signal_ts
):

    """
    OKX事件合约下单。

    tdMode = isolated
    outcome = yes / no
    speedBump = 1

    sz = 合约数量
    """

    if outcome not in ("yes", "no"):

        raise ValueError(
            f"非法outcome参数: {outcome}"
        )

    if sz <= 0:

        raise ValueError(
            "下单数量必须大于0"
        )

    path = "/api/v5/trade/order"

    # 每个信号使用独立客户端订单ID
    cl_ord_id = hashlib.md5(
        f"MACD_{inst_id}_{signal_ts}_{outcome}".encode()
    ).hexdigest()[:32]

    payload = {

        "instId": inst_id,

        "tdMode": "isolated",

        "clOrdId": cl_ord_id,

        "side": "buy",

        "ordType": "market",

        "sz": str(sz),

        "outcome": outcome,

        "speedBump": "1",
    }

    print("\n========== OKX下单参数 ==========")

    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2
        )
    )

    print("=================================\n")

    data = _request(
        "POST",
        path,
        payload
    )

    result = data["data"][0]

    if result.get("sCode") != "0":

        raise RuntimeError(
            f"下单失败: "
            f"sCode={result.get('sCode')}, "
            f"sMsg={result.get('sMsg')}"
        )

    return result


# ============================================================
# MACD信号
# ============================================================

def get_macd_signal(
    exchange,
    symbol,
    fast,
    slow,
    signal
):

    ohlcv = exchange.fetch_ohlcv(
        symbol,
        timeframe="5m",
        limit=300
    )

    if len(ohlcv) < slow + signal + 2:

        return None, (
            int(ohlcv[-1][0])
            if ohlcv else 0
        )

    df = pd.DataFrame(
        ohlcv,
        columns=[
            "ts",
            "o",
            "h",
            "l",
            "c",
            "v"
        ]
    )

    ema_fast = df["c"].ewm(
        span=fast,
        adjust=False
    ).mean()

    ema_slow = df["c"].ewm(
        span=slow,
        adjust=False
    ).mean()

    dif = ema_fast - ema_slow

    dea = dif.ewm(
        span=signal,
        adjust=False
    ).mean()

    hist = dif - dea

    # 最后一根K线可能尚未收盘
    idx = len(df) - 2

    closed_ts = int(
        df.iloc[idx]["ts"]
    )

    h = hist.iloc[idx]

    if pd.isna(h):

        return None, closed_ts

    if h > 0:

        return "UP", closed_ts

    elif h < 0:

        return "DOWN", closed_ts

    return None, closed_ts


# ============================================================
# 状态持久化
# ============================================================

def load_state(base_stake):

    if STATE_FILE.exists():

        try:

            with STATE_FILE.open(
                "r",
                encoding="utf-8"
            ) as f:

                state = json.load(f)

            return (
                state.get("stake", base_stake),
                state.get("consecutive_losses", 0)
            )

        except Exception as e:

            print(
                f"读取状态文件失败: {e}"
            )

    return base_stake, 0


def save_state(
    stake,
    consecutive_losses
):

    state = {

        "stake": stake,

        "consecutive_losses": consecutive_losses,

        "updated_at": datetime.now(
            timezone.utc
        ).isoformat()
    }

    temp_file = STATE_FILE.with_suffix(".tmp")

    with temp_file.open(
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )

    temp_file.replace(STATE_FILE)


# ============================================================
# 主程序
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description="OKX事件合约MACD自动交易"
    )

    parser.add_argument(
        "--series-id",
        default="BTC-UPDOWN-5MIN"
    )

    parser.add_argument(
        "--symbol",
        default="BTC/USDT"
    )

    parser.add_argument(
        "--fast",
        type=int,
        default=6
    )

    parser.add_argument(
        "--slow",
        type=int,
        default=13
    )

    parser.add_argument(
        "--signal",
        type=int,
        default=5
    )

    parser.add_argument(
        "--base",
        type=float,
        default=10.0,
        help="基础合约数量，不是USDT金额"
    )

    parser.add_argument(
        "--max-losses",
        type=int,
        default=10
    )

    parser.add_argument(
        "--up-outcome",
        choices=["yes", "no"],
        default="yes"
    )

    parser.add_argument(
        "--down-outcome",
        choices=["yes", "no"],
        default="no"
    )

    parser.add_argument(
        "--live",
        action="store_true"
    )

    parser.add_argument(
        "--poll-interval",
        type=int,
        default=15
    )

    args = parser.parse_args()

    if args.fast <= 0 or args.slow <= 0 or args.signal <= 0:
        print("错误：MACD周期必须大于0")
        sys.exit(1)

    if args.fast >= args.slow:
        print("错误：MACD fast必须小于slow")
        sys.exit(1)

    if args.base <= 0:
        print("错误：基础合约数量必须大于0")
        sys.exit(1)

    if args.poll_interval < 1:
        print("错误：轮询间隔必须大于0")
        sys.exit(1)

    if args.live and not all([
        API_KEY,
        SECRET_KEY,
        PASSPHRASE
    ]):

        print(
            "错误：--live需要配置"
            "OKX_API_KEY / OKX_SECRET_KEY / "
            "OKX_PASSPHRASE"
        )

        sys.exit(1)

    exchange = ccxt.okx({
        "enableRateLimit": True
    })

    stake, consecutive_losses = load_state(
        args.base
    )

    print("=" * 65)

    print(
        f"模式: {'🔴 LIVE实盘交易' if args.live else '🟢 DRY RUN模拟运行'}"
    )

    print(
        f"系列: {args.series_id}"
    )

    print(
        f"交易品种: {args.symbol}"
    )

    print(
        f"MACD: {args.fast}/{args.slow}/{args.signal}"
    )

    print(
        f"当前stake: {stake} 张"
    )

    print(
        f"连续亏损: {consecutive_losses}"
    )

    print(
        f"UP -> outcome={args.up_outcome}"
    )

    print(
        f"DOWN -> outcome={args.down_outcome}"
    )

    print(
        "tdMode: isolated"
    )

    print(
        "speedBump: 1"
    )

    print(
        "时间显示: UTC / 北京时间(UTC+8)"
    )

    print("=" * 65 + "\n")

    last_processed_ts = None

    while True:

        try:

            # 获取MACD信号
            signal, closed_ts = get_macd_signal(
                exchange,
                args.symbol,
                args.fast,
                args.slow,
                args.signal
            )

            if not closed_ts:

                time.sleep(args.poll_interval)
                continue

            # 同一根K线只处理一次
            if closed_ts == last_processed_ts:

                time.sleep(args.poll_interval)
                continue

            last_processed_ts = closed_ts

            if signal is None:

                print(
                    f"[{dual_str(closed_ts)}] "
                    "MACD柱体为0，无明确信号"
                )

                time.sleep(args.poll_interval)
                continue

            print(
                f"\n[{dual_str(closed_ts)}] "
                f"MACD信号: {signal}"
            )

            # 转换成OKX合法的outcome
            outcome = convert_outcome(
                signal,
                args.up_outcome,
                args.down_outcome
            )

            print(
                f"  策略方向: {signal}"
            )

            print(
                f"  OKX outcome: {outcome}"
            )

            # 获取live市场
            markets = get_live_markets(
                args.series_id
            )

            if not markets:

                print(
                    "  当前没有live市场"
                )

                time.sleep(args.poll_interval)
                continue

            # 匹配合约
            matched, exp_ms = match_contract_by_signal_time(
                markets,
                closed_ts
            )

            if matched is None:

                print(
                    f"  未找到匹配合约"
                    f"（信号时间 {dual_str(closed_ts)}）"
                )

                print(
                    "  当前live合约到期时间："
                )

                for market in markets:

                    exp = market.get("expTime")

                    exp_str = (
                        dual_str(int(exp))
                        if exp and str(exp).isdigit()
                        else "?"
                    )

                    print(
                        f"    {market.get('instId')} "
                        f"-> 到期 {exp_str}"
                    )

                time.sleep(args.poll_interval)
                continue

            inst_id = matched["instId"]

            print(
                f"  匹配合约: {inst_id}"
            )

            print(
                f"  到期时间: {dual_str(exp_ms)}"
            )

            if matched.get("state") != "live":

                print(
                    "  合约不是live状态，取消交易"
                )

                time.sleep(args.poll_interval)
                continue

            if exp_ms <= now_ms():

                print(
                    "  合约已经到期，取消交易"
                )

                time.sleep(args.poll_interval)
                continue

            # 下单数量
            sz = stake

            if not args.live:

                print(
                    f"  [DRY RUN] "
                    f"将下单 outcome={outcome}, "
                    f"sz={sz} 张"
                )

            else:

                print(
                    f"  [LIVE] "
                    f"准备下单 outcome={outcome}, "
                    f"sz={sz} 张"
                )

                result = place_event_order(
                    inst_id,
                    outcome,
                    sz,
                    closed_ts
                )

                print(
                    f"  <下单成功> "
                    f"ordId={result.get('ordId')}"
                )

                print(
                    f"  clOrdId={result.get('clOrdId')}"
                )

                print(
                    f"  sCode={result.get('sCode')}"
                )

        except KeyboardInterrupt:

            print(
                "\n用户主动停止程序"
            )

            break

        except Exception as e:

            print(
                f"\n[{datetime.now().strftime('%H:%M:%S')}] "
                f"异常: {e}"
            )

        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
