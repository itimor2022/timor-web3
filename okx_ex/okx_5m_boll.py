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

输赢判定（基于K线）：
- 信号K线的下一根已收盘K线的 close
- UP 信号：下一根 close > 信号K线 close -> 赢
- DOWN 信号：下一根 close < 信号K线 close -> 赢
- 相等视为输（保守处理）

资金管理：
- 盈利：连亏清零，投注 = 基础投注额
- 亏损：连亏+1，投注 = 上一笔投注 * 2 + 基础投注额
- 连亏达到 --max-losses（默认10）停止下单
- 投注张数超过 --max-stake（默认1000）停止下单

状态日志：
- 每笔订单下单成功输出 [STATUS] ORDER_PLACED
- 每笔订单结算完成输出 [STATUS] SETTLED

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
# 统一状态日志
# ============================================================

def print_status(tag, state, extra=None):
    """
    统一状态日志

    tag: 事件标签，如 "ORDER_PLACED" / "SETTLED"
    state: 当前状态 dict
    extra: 额外字段 dict
    """
    ts = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

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
    """
    返回 (signal, closed_ts, closed_close)
    signal: "UP" / "DOWN" / None
    closed_ts: 已收盘K线时间戳(ms)
    closed_close: 已收盘K线收盘价
    """

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
        ), None

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
    closed_close = float(row["c"])

    if pd.isna(row["middle"]):
        return None, closed_ts, closed_close

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

        return "UP", closed_ts, closed_close

    elif body_high < middle:

        return "DOWN", closed_ts, closed_close

    return None, closed_ts, closed_close


# ============================================================
# 获取最新已收盘K线（用于结算）
# ============================================================

def get_latest_closed_candle(exchange, symbol):
    """
    返回 (ts, close)，倒数第二根为已收盘
    """
    ohlcv = exchange.fetch_ohlcv(
        symbol,
        timeframe="5m",
        limit=5
    )

    if len(ohlcv) < 2:
        return None, None

    row = ohlcv[-2]

    return int(row[0]), float(row[4])


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

            return {
                "stake": state.get("stake", base_stake),
                "consecutive_losses": state.get(
                    "consecutive_losses", 0
                ),
                "last_processed_ts": state.get(
                    "last_processed_ts"
                ),
                "pending": state.get("pending", False),
                "last_signal": state.get("last_signal"),
                "last_signal_close_ts": state.get(
                    "last_signal_close_ts"
                ),
                "last_signal_close_price": state.get(
                    "last_signal_close_price"
                ),
                "last_stake": state.get("last_stake"),
            }

        except Exception as e:

            print(f"读取状态文件失败: {e}")

    return {
        "stake": base_stake,
        "consecutive_losses": 0,
        "last_processed_ts": None,
        "pending": False,
        "last_signal": None,
        "last_signal_close_ts": None,
        "last_signal_close_price": None,
        "last_stake": None,
    }


def save_state(state):

    out = dict(state)
    out["updated_at"] = datetime.now(
        timezone.utc
    ).isoformat()

    temp_file = STATE_FILE.with_suffix(".tmp")

    with temp_file.open(
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            out,
            f,
            ensure_ascii=False,
            indent=2
        )

    temp_file.replace(STATE_FILE)


# ============================================================
# 结算上一笔（基于K线）
# ============================================================

def settle_pending(
    state,
    exchange,
    symbol,
    base_stake,
    max_losses,
    max_stake
):
    """
    若有待结算单，用最新已收盘K线判定输赢并更新状态。

    判定：
    - 信号K线的下一根已收盘K线 close
    - UP: close > entry_close -> 赢
    - DOWN: close < entry_close -> 赢
    - 相等视为输
    """

    if not state.get("pending"):
        return

    ts, close = get_latest_closed_candle(
        exchange, symbol
    )

    if ts is None:
        return

    entry_ts = state.get("last_signal_close_ts")

    if entry_ts is None or ts <= entry_ts:
        # 下一根还没收盘，继续等
        return

    signal = state.get("last_signal")
    entry_close = state.get("last_signal_close_price")
    last_stake = state.get("last_stake") or base_stake

    if entry_close is None:
        # 数据异常，直接清除 pending
        print("[结算] 缺少开仓价格，放弃结算")
        state["pending"] = False
        save_state(state)
        return

    if signal == "UP":
        win = close > entry_close
    elif signal == "DOWN":
        win = close < entry_close
    else:
        print(f"[结算] 未知信号 {signal}，放弃结算")
        state["pending"] = False
        save_state(state)
        return

    print(
        f"\n[结算 {dual_str(ts)}] "
        f"信号={signal}, "
        f"开仓close={entry_close}, "
        f"到期close={close} "
        f"-> {'盈利' if win else '亏损'}"
    )

    if win:
        state["consecutive_losses"] = 0
        state["stake"] = base_stake
        print(
            f"  连亏清零，"
            f"下一笔投注 = {state['stake']} 张"
        )
    else:
        state["consecutive_losses"] += 1
        new_stake = last_stake * 2 + base_stake
        state["stake"] = new_stake
        print(
            f"  连亏={state['consecutive_losses']}, "
            f"上一笔={last_stake}, "
            f"下一笔投注 = {new_stake} 张"
        )

    # 清理待结算字段
    state["pending"] = False
    state["last_signal"] = None
    state["last_signal_close_ts"] = None
    state["last_signal_close_price"] = None
    state["last_stake"] = None

    # 风控：连亏上限
    if state["consecutive_losses"] >= max_losses:
        print(
            f"\n!!! 已连续亏损 "
            f"{state['consecutive_losses']} 次，"
            f"达到风控上限 {max_losses}，停止下单 !!!"
        )
        state["stake"] = 0

    # 风控：投注上限
    elif state["stake"] > max_stake:
        print(
            f"\n!!! 下一笔投注 {state['stake']} 张 "
            f"超过上限 {max_stake}，停止下单 !!!"
        )
        state["stake"] = 0

    # 状态日志：结算完成
    print_status(
        "已结算",
        state,
        {
            "信号": signal,
            "开仓价": entry_close,
            "到期价": close,
            "结果": "盈利" if win else "亏损",
            "上笔投注": last_stake,
            "下笔投注": state["stake"],
            "是否熔断": (
                "是"
                if (
                    state["consecutive_losses"] >= max_losses
                    or state["stake"] <= 0
                )
                else "否"
            ),
        }
    )

    save_state(state)


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
        default=10,
        help="连亏多少次后停止下单"
    )

    parser.add_argument(
        "--max-stake",
        type=float,
        default=1000.0,
        help="单笔投注张数上限，超过则停止下单"
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

    if args.max_losses < 1:
        print("错误：--max-losses 必须大于等于1")
        sys.exit(1)

    if args.max_stake <= 0:
        print("错误：--max-stake 必须大于0")
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

    state = load_state(args.base)

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

    print(f"基础投注数量: {args.base} 张")

    print(f"当前投注数量: {state['stake']} 张")

    print(f"连亏次数: {state['consecutive_losses']}")

    print(f"连亏上限: {args.max_losses}")

    print(f"单笔投注上限: {args.max_stake} 张")

    print(f"待结算单: {state['pending']}")

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

            # 1) 先结算上一笔
            settle_pending(
                state,
                exchange,
                args.symbol,
                args.base,
                args.max_losses,
                args.max_stake
            )

            # 2) 熔断检查
            if state["consecutive_losses"] >= args.max_losses:
                print(
                    "风控熔断中（连亏上限），跳过本轮"
                )
                time.sleep(args.poll_interval)
                continue

            if state["stake"] <= 0:
                print(
                    "当前投注张数为0（已熔断或超上限），"
                    "跳过本轮。请检查状态文件。"
                )
                time.sleep(args.poll_interval)
                continue

            # 3) 获取信号
            signal, closed_ts, closed_close = (
                get_boll_signal(
                    exchange,
                    args.symbol,
                    args.period,
                    args.std
                )
            )

            if not closed_ts:
                time.sleep(args.poll_interval)
                continue

            # 同一根K线只处理一次
            if closed_ts == state["last_processed_ts"]:
                time.sleep(args.poll_interval)
                continue

            # 先记录处理时间，避免异常后重复提交
            state["last_processed_ts"] = closed_ts
            save_state(state)

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

            contracts = state["stake"]

            if contracts <= 0:
                print("  投注张数为0，跳过")
                time.sleep(args.poll_interval)
                continue

            result = None

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

            # 4) 记录待结算（LIVE 与 DRY RUN 都记录）
            state["pending"] = True
            state["last_signal"] = signal
            state["last_signal_close_ts"] = closed_ts
            state["last_signal_close_price"] = closed_close
            state["last_stake"] = contracts
            save_state(state)

            # 状态日志：下单完成
            print_status(
                "ORDER_PLACED",
                state,
                {
                    "instId": inst_id,
                    "outcome": outcome,
                    "sz": contracts,
                    "signal": signal,
                    "closed_ts": closed_ts,
                    "ordId": (
                        result.get("ordId")
                        if result else "DRY_RUN"
                    ),
                }
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