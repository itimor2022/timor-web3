
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX 事件合约 BTC 5分钟布林线自动交易
UTC + 北京时间双显示版

策略：
1. 已收盘5分钟K线
2. 实体完全在布林中轨上方 -> UP -> YES
3. 实体完全在布林中轨下方 -> DOWN -> NO
4. 实体穿越或触碰中轨 -> 不交易

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
STATE_FILE = Path("event_trade_state.json")

API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

BEIJING_TZ = timezone(timedelta(hours=8))


# ============================================================
# 时间工具
# ============================================================

def dual_str(ms):
    utc_part = datetime.fromtimestamp(
        ms / 1000,
        tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")

    bj_part = datetime.fromtimestamp(
        ms / 1000,
        tz=BEIJING_TZ
    ).strftime("%H:%M:%S 北京")

    return f"{utc_part} / {bj_part}"


def now_ms():
    return int(time.time() * 1000)


# ============================================================
# OKX API 签名
# ============================================================

def _timestamp():
    now = datetime.now(timezone.utc)

    return (
        now.strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{now.microsecond // 1000:03d}Z"
    )


def _sign(ts, method, path, body=""):
    message = ts + method.upper() + path + body

    mac = hmac.new(
        SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256
    )

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

    body = (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":")
        )
        if payload is not None else ""
    )

    headers = _headers(method, path, body)

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


def match_contract_by_signal_time(markets, signal_ts_ms):

    candidates = []

    for market in markets:

        exp = market.get("expTime")

        if not exp:
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

    candidates.sort(key=lambda x: x[0])

    best_exp, best_market = candidates[0]

    return best_market, best_exp


# ============================================================
# 事件合约下单
# ============================================================

def place_event_order(
    inst_id,
    outcome,
    contracts
):
    """
    OKX事件合约下单

    tdMode: isolated
    outcome: yes / no
    speedBump: 1
    side: buy

    contracts: 合约张数
    """

    if outcome not in ("yes", "no"):
        raise ValueError(
            f"非法 outcome: {outcome}"
        )

    if contracts <= 0:
        raise ValueError("下单数量必须大于0")

    path = "/api/v5/trade/order"

    # 使用固定信号标识，辅助防止重复委托
    cl_ord_id = hashlib.md5(
        f"{inst_id}_{outcome}_{contracts}".encode()
    ).hexdigest()[:32]

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

    # print("\n========== 下单参数 ==========")

    # print(json.dumps(
    #     payload,
    #     ensure_ascii=False,
    #     indent=2
    # ))
    
    print("\n========== 下单结果 ==========")
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
# 布林线信号
# ============================================================

def get_boll_signal(
    exchange,
    symbol,
    period,
    std_mult
):

    limit = max(period + 30, 100)

    ohlcv = exchange.fetch_ohlcv(
        symbol,
        timeframe="5m",
        limit=limit
    )

    if len(ohlcv) < period + 2:

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

    df["middle"] = df["c"].rolling(
        period
    ).mean()

    df["std"] = df["c"].rolling(
        period
    ).std(ddof=0)

    df["upper"] = (
        df["middle"]
        + std_mult * df["std"]
    )

    df["lower"] = (
        df["middle"]
        - std_mult * df["std"]
    )

    # 最后一根K线可能尚未收盘
    idx = len(df) - 2

    row = df.iloc[idx]

    closed_ts = int(row["ts"])

    if pd.isna(row["middle"]):
        return None, closed_ts

    body_low = min(
        float(row["o"]),
        float(row["c"])
    )

    body_high = max(
        float(row["o"]),
        float(row["c"])
    )

    middle = float(row["middle"])

    if body_low > middle:

        return "UP", closed_ts

    elif body_high < middle:

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
                state.get("consecutive_losses", 0),
                state.get("last_processed_ts")
            )

        except Exception as e:

            print(f"读取状态文件失败: {e}")

    return base_stake, 0, None


def save_state(
    stake,
    consecutive_losses,
    last_processed_ts
):

    state = {
        "stake": stake,
        "consecutive_losses": consecutive_losses,
        "last_processed_ts": last_processed_ts,
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
        description="OKX事件合约5分钟布林线自动交易"
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
        "--period",
        type=int,
        default=20
    )

    parser.add_argument(
        "--std",
        type=float,
        default=2.0
    )

    parser.add_argument(
        "--base",
        type=float,
        default=10.0,
        help="基础投注数量（合约张数）"
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

    if args.period < 2:
        print("错误：布林周期必须大于等于2")
        sys.exit(1)

    if args.base <= 0:
        print("错误：基础投注数量必须大于0")
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
            "错误：LIVE需要配置OKX_API_KEY、"
            "OKX_SECRET_KEY、OKX_PASSPHRASE"
        )

        sys.exit(1)

    exchange = ccxt.okx({
        "enableRateLimit": True
    })

    stake, consecutive_losses, last_processed_ts = (
        load_state(args.base)
    )

    print("=" * 65)

    print(
        f"模式: {'LIVE 实盘交易' if args.live else 'DRY RUN 模拟运行'}"
    )

    print(f"系列: {args.series_id}")

    print(f"交易品种: {args.symbol}")

    print(
        f"布林线: 周期={args.period}, "
        f"标准差={args.std}"
    )

    print(f"基础投注数量: {stake} 张")

    print(f"连亏次数: {consecutive_losses}")

    print(
        f"UP 对应 outcome: {args.up_outcome}"
    )

    print(
        f"DOWN 对应 outcome: {args.down_outcome}"
    )

    print("交易模式: isolated")

    print("时间: UTC / 北京时间")

    print("=" * 65)

    while True:

        try:

            signal, closed_ts = get_boll_signal(
                exchange,
                args.symbol,
                args.period,
                args.std
            )

            if not closed_ts:
                time.sleep(args.poll_interval)
                continue

            # 同一根K线只处理一次
            if closed_ts == last_processed_ts:

                time.sleep(args.poll_interval)
                continue

            # 先记录处理时间，避免异常后重复提交
            last_processed_ts = closed_ts

            save_state(
                stake,
                consecutive_losses,
                last_processed_ts
            )

            if signal is None:

                print(
                    f"[{dual_str(closed_ts)}] "
                    "无明确信号（穿越中轨或指标未预热）"
                )

                time.sleep(args.poll_interval)
                continue

            print(
                f"\n[{dual_str(closed_ts)}] "
                f"布林线信号: {signal}"
            )

            # 策略方向映射
            outcome = (
                args.up_outcome
                if signal == "UP"
                else args.down_outcome
            )

            print(
                f"  策略信号: {signal}"
            )

            print(
                f"  OKX outcome: {outcome}"
            )

            # 查询市场
            markets = get_live_markets(
                args.series_id
            )

            if not markets:

                print("  当前没有 live 市场")

                time.sleep(args.poll_interval)
                continue

            # 匹配到期合约
            matched, exp_ms = match_contract_by_signal_time(
                markets,
                closed_ts
            )

            if matched is None:

                print("  没有找到匹配的事件合约")

                time.sleep(args.poll_interval)
                continue

            inst_id = matched["instId"]

            print(
                f"  匹配合约: {inst_id}"
            )

            print(
                f"  到期时间: {dual_str(exp_ms)}"
            )

            print(
                f"  当前状态: {matched.get('state')}"
            )

            # 到期时间必须晚于当前时间
            if exp_ms <= now_ms():

                print("  合约已经到期，取消交易")

                time.sleep(args.poll_interval)
                continue

            # 再次检查市场状态
            if matched.get("state") != "live":

                print("  合约不是 live 状态，取消交易")

                time.sleep(args.poll_interval)
                continue

            contracts = stake

            if not args.live:

                print(
                    f"  [DRY RUN] "
                    f"将买入 outcome={outcome}, "
                    f"sz={contracts} 张"
                )

            else:

                print(
                    f"  [LIVE] "
                    f"准备买入 outcome={outcome}, "
                    f"sz={contracts} 张"
                )

                result = place_event_order(
                    inst_id,
                    outcome,
                    contracts
                )

                print(
                    f"  <下单成功> ordId={result.get('ordId')}"
                )

                print(
                    f"  clOrdId={result.get('clOrdId')}"
                )

                print(
                    f"  sCode={result.get('sCode')}"
                )

        except KeyboardInterrupt:

            print("\n用户主动停止程序")
            break

        except Exception as e:

            print(
                f"\n[{datetime.now().strftime('%H:%M:%S')}] "
                f"异常: {e}"
            )

        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
