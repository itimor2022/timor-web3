#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
加密货币 15分钟 布林信号策略 + 成交量信号 + 涨跌幅信号

使用方法：

模拟模式：
    python okx_15m.py BTC

指定日期：
    python okx_15m.py BTC --date 2026-11-04

实时模式（单次检测，适合定时任务）：
    python okx_15m.py BTC --live

开启详细诊断：
    python okx_15m.py BTC --debug

环境变量：
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

策略：
    布林：20周期，2倍标准差
    成交量：K0 >= K2 + K3 + K4
    成交量冷却：60分钟
    涨跌幅：当前振幅 >= 前5根平均振幅 * 2
    涨跌幅：绝对涨跌幅 >= 0.6%
    涨跌幅冷却：60分钟

时间：
    北京时间 UTC+8
"""

import os
import sys
import json
import time
import argparse
import traceback
import requests
import pandas as pd

from datetime import timedelta, datetime
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# 基础配置
# ============================================================

pd.set_option("display.max_columns", 1000)
pd.set_option("display.max_rows", 1000)
pd.set_option("display.width", 1000)
pd.set_option("display.max_colwidth", 1000)

TIMEZONE_OFFSET = 8

# K线周期：15分钟
BAR_INTERVAL = "15m"

# 单根K线时长（用于起止时间格式化）
BAR_DURATION = timedelta(minutes=15)

VOLUME_COOLDOWN = timedelta(minutes=15)

AMPLITUDE_COOLDOWN = timedelta(minutes=15)
# 成交量激增倍数阈值
VOLUME_SURGE_RATIO = 3.5

# 状态文件：跨进程记录已经推送的信号
STATE_FILE = "okx_15m_state.json"

# Telegram 配置从环境变量读取
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_15M_CHAT_ID", "").strip()

BASE_URL = (
    f"https://api.telegram.org/bot{TOKEN}"
    if TOKEN else ""
)

# ============================================================
# 交易对映射
# ============================================================

SYMBOL_MAP = {
    "BTC": "BTC-USDT",
    "ETH": "ETH-USDT",
    "SOL": "SOL-USDT",
    "ZEC": "ZEC-USDT",
    "HYPE": "HYPE-USDT",
    "XAU": "XAU-USDT",
    "XQQQ": "XQQQ-USDT",
    "XSNDK": "XSNDK-USDT",
    "XSOXL": "XSOXL-USDT",
    "XNVDA": "XNVDA-USDT",

}


# ============================================================
# 状态管理
# ============================================================

def load_state():
    """读取历史推送状态"""

    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        return data if isinstance(data, dict) else {}

    except Exception as e:
        print(f"读取状态文件失败: {e}")
        return {}


def save_state(state):
    """保存历史推送状态"""

    temp_file = STATE_FILE + ".tmp"

    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(
                state,
                f,
                ensure_ascii=False,
                indent=4
            )

        os.replace(temp_file, STATE_FILE)

    except Exception as e:
        print(f"保存状态文件失败: {e}")


def allow_signal(signal_type, symbol, ts, cooldown):
    """
    判断是否允许发送信号。

    同时检查：
    1. 是否已经发送过当前K线
    2. 是否处于冷却时间
    """

    state = load_state()

    key = f"{symbol}_{signal_type}"

    last_value = state.get(key)

    if not last_value:
        return True

    try:
        last_ts = pd.Timestamp(last_value)
        current_ts = pd.Timestamp(ts)

        if current_ts <= last_ts:
            return False

        if current_ts - last_ts < cooldown:
            return False

        return True

    except Exception:
        return True


def mark_signal_sent(signal_type, symbol, ts):
    """发送成功后记录信号"""

    state = load_state()

    key = f"{symbol}_{signal_type}"

    state[key] = pd.Timestamp(ts).isoformat()

    save_state(state)


# ============================================================
# Telegram
# ============================================================

def send_message(msg):
    """发送 Telegram 消息"""

    if not TOKEN or not CHAT_ID:
        print("Telegram 配置缺失，请设置环境变量")
        print("TELEGRAM_BOT_TOKEN")
        print("TELEGRAM_CHAT_ID")
        return False

    try:
        response = requests.post(
            f"{BASE_URL}/sendMessage",
            data={
                "chat_id": CHAT_ID,
                "text": msg
            },
            timeout=15
        )

        response.raise_for_status()

        result = response.json()

        if not result.get("ok"):
            print(f"Telegram 返回失败: {result}")
            return False

        print("Telegram 消息发送成功")

        return True

    except requests.exceptions.RequestException as e:
        print(f"Telegram 网络异常: {e}")
        return False

    except Exception as e:
        print(f"Telegram 发送异常: {e}")
        return False


# ============================================================
# 获取 OKX K线
# ============================================================

def get_candles(inst_id="BTC-USDT-SWAP", bar=BAR_INTERVAL):

    url = "https://www.okx.com/api/v5/market/candles"

    response = requests.get(
        url,
        params={
            "instId": inst_id,
            "bar": bar,
            "limit": 300
        },
        timeout=15
    )

    response.raise_for_status()

    result = response.json()

    if result.get("code") != "0":
        raise RuntimeError(
            f"OKX接口错误: {result}"
        )

    data = result.get("data", [])

    if not data:
        raise RuntimeError("OKX没有返回K线数据")

    df = pd.DataFrame(
        data,
        columns=[
            "ts",
            "open",
            "high",
            "low",
            "close",
            "vol",
            "volCcy",
            "volCcyQuote",
            "confirm"
        ]
    )

    # 时间戳转换为北京时间
    df["ts"] = (
        pd.to_datetime(
            df["ts"].astype("int64"),
            unit="ms"
        )
        + pd.Timedelta(hours=TIMEZONE_OFFSET)
    )

    for col in [
        "open",
        "high",
        "low",
        "close",
        "vol"
    ]:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce"
        )

    df["confirm"] = df["confirm"].astype(str)

    df = df.dropna(
        subset=[
            "open",
            "high",
            "low",
            "close",
            "vol"
        ]
    )

    df = (
        df.sort_values("ts")
        .drop_duplicates(subset=["ts"], keep="last")
        .reset_index(drop=True)
    )

    return df[
        [
            "ts",
            "open",
            "high",
            "low",
            "close",
            "vol",
            "confirm"
        ]
    ]


# ============================================================
# 布林指标
# ============================================================

def add_indicators(df):

    df = df.copy()

    df["mid"] = df["close"].rolling(20).mean()

    df["std"] = df["close"].rolling(20).std()

    df["upper"] = df["mid"] + 2 * df["std"]

    df["lower"] = df["mid"] - 2 * df["std"]

    df["is_bull"] = df["close"] > df["open"]

    df["is_bear"] = df["close"] < df["open"]

    df["mid_price"] = (
        df["close"] + df["open"]
    ) / 2

    # 涨跌幅
    df["change_pct"] = (
        (df["close"] - df["open"])
        / df["open"]
        * 100
    )

    # BBQ
    df["bbq"] = (
        df["mid_price"] - df["mid"]
    ).abs()

    df["bbq_min"] = df["bbq"].rolling(7).min()

    # EMA
    df["ema5"] = df["close"].ewm(
        span=5,
        adjust=False
    ).mean()

    # 振幅
    df["amplitude"] = df["high"] - df["low"]

    return df


# ============================================================
# 成交量信号
# ============================================================

def detect_volume_signal(df):
    """
    成交量信号：

    K0 >= K2 + K3 + K4

    K0：当前检测K线
    K1：前1根
    K2：前2根
    K3：前3根
    K4：前4根
    """

    if len(df) < 5:
        return None

    k0 = df.iloc[-1]

    k1 = df.iloc[-2]

    k2 = df.iloc[-3]

    k3 = df.iloc[-4]

    k4 = df.iloc[-5]

    sum_vol = (
        k2["vol"]
        + k3["vol"]
        + k4["vol"]
    )

    if sum_vol <= 0:
        return None

    if k0["vol"] >= sum_vol:

        ratio = k0["vol"] / sum_vol

        return {
            "k0": k0,
            "k1": k1,
            "k2": k2,
            "k3": k3,
            "k4": k4,
            "k0_vol": k0["vol"],
            "sum_vol": sum_vol,
            "ratio": ratio,
            "ts": k0["ts"]
        }

    return None


# ============================================================
# 涨跌幅信号
# ============================================================

def detect_amplitude_signal(df):
    """
    条件：

    1. 当前K0振幅 >= 前5根K线平均振幅 * 2
    2. 当前K0涨跌幅绝对值 >= 0.6%
    """

    if len(df) < 6:
        return None

    k0 = df.iloc[-1]

    prev = df.iloc[-6:-1]

    avg_amplitude = prev["amplitude"].mean()

    if avg_amplitude <= 0:
        return None

    k0_amplitude = k0["amplitude"]

    ratio = k0_amplitude / avg_amplitude

    abs_change = abs(k0["change_pct"])

    if ratio >= 2.0 and abs_change >= 0.6:

        return {
            "k0": k0,
            "ts": k0["ts"],
            "k0_amplitude": k0_amplitude,
            "avg_amplitude": avg_amplitude,
            "ratio": ratio,
            "change_pct": k0["change_pct"]
        }

    return None

# ============================================================
# 成交量激增信号（K0 >= K1 * 3.5）
# ============================================================

def detect_volume_surge_signal(df):
    """
    成交量激增信号：

    K0 >= K1 * 3.5

    K0：当前检测K线
    K1：前1根
    """

    if len(df) < 2:
        return None

    k0 = df.iloc[-1]

    k1 = df.iloc[-2]

    if k1["vol"] <= 0:
        return None

    ratio = k0["vol"] / k1["vol"]

    if ratio >= VOLUME_SURGE_RATIO:

        return {
            "k0": k0,
            "k1": k1,
            "k0_vol": k0["vol"],
            "k1_vol": k1["vol"],
            "ratio": ratio,
            "ts": k0["ts"]
        }

    return None

# ============================================================
# 信号格式化
# ============================================================

def format_volume_signal(k, result):

    ts1 = (
        k["ts"] - BAR_DURATION
    ).strftime("%m-%d %H:%M")

    ts2 = k["ts"].strftime("%m-%d %H:%M")

    text = (
        f"{ts1} ~ {ts2} | "
        f"{k['close']:,.2f} | "
        f"{k['vol']:,.2f} | "
        f"{k['change_pct']:,.2f}%\n"
    )

    text += (
        f" - 成交量信号: "
        f"({result['ratio']:.2f}倍)\n"
    )

    return text


def format_volume_surge_signal(k, result):

    ts1 = (
        k["ts"] - BAR_DURATION
    ).strftime("%m-%d %H:%M")

    ts2 = k["ts"].strftime("%m-%d %H:%M")

    text = (
        f"{ts1} ~ {ts2} | "
        f"{k['close']:,.2f} | "
        f"{k['vol']:,.2f} | "
        f"{k['change_pct']:,.2f}%\n"
    )

    text += (
        f" - 成交量激增信号: "
        f"({result['ratio']:.2f}倍, "
        f"K0 {result['k0_vol']:,.2f} / "
        f"K1 {result['k1_vol']:,.2f})\n"
    )

    return text

def format_amplitude_signal(k, result):

    ts1 = (
        k["ts"] - BAR_DURATION
    ).strftime("%m-%d %H:%M")

    ts2 = k["ts"].strftime("%m-%d %H:%M")

    text = (
        f"{ts1} ~ {ts2} | "
        f"{k['close']:,.2f} | "
        f"{k['vol']:,.2f} | "
        f"{k['change_pct']:,.2f}%\n"
    )

    text += (
        f" - 涨跌幅信号: "
        f"振幅 {result['k0_amplitude']:,.2f} / "
        f"均值 {result['avg_amplitude']:,.2f} = "
        f"{result['ratio']:.2f}倍, "
        f"涨跌幅 {result['change_pct']:.2f}%\n"
    )

    return text


# ============================================================
# 模拟模式：扫描指定日期
# ============================================================

def scan_date(
    df,
    symbol_name,
    target_date,
    debug=False
):

    print(
        f"开始模拟扫描 {symbol_name} 日期 {target_date}..."
    )

    # 历史模拟同样只使用已收盘K线
    df = df[
        df["confirm"] == "1"
    ].copy().reset_index(drop=True)

    if debug:
        debug_volume(df, target_date, symbol_name)

    target_start = (
        pd.Timestamp(target_date)
    )

    target_end = (
        target_start + pd.Timedelta(days=1)
    )

    log_file = (
        f"{symbol_name}_"
        f"{target_date.strftime('%Y%m%d')}_signal_15m.txt"
    )

    total_volume = 0

    total_volume_surge = 0

    out_lines = []

    # ------------------------------
    # 成交量信号扫描
    # ------------------------------

    for i in range(4, len(df)):

        k0 = df.iloc[i]

        if not (
            target_start <= k0["ts"] < target_end
        ):
            continue

        sub = df.iloc[:i + 1]

        result = detect_volume_signal(sub)

        if result:

            text = format_volume_signal(
                k0,
                result
            )

            out_lines.append(
                text + "-" * 30 + "\n"
            )

            total_volume += 1


    # ------------------------------
    # 成交量激增信号扫描（K0 >= K1*3.5）
    # ------------------------------

    for i in range(1, len(df)):

        k0 = df.iloc[i]

        if not (
            target_start <= k0["ts"] < target_end
        ):
            continue

        sub = df.iloc[:i + 1]

        result = detect_volume_surge_signal(sub)

        if result:

            text = format_volume_surge_signal(
                k0,
                result
            )

            out_lines.append(
                text + "-" * 30 + "\n"
            )

            total_volume_surge += 1

    # ------------------------------
    # 输出文件
    # ------------------------------

    with open(
        log_file,
        "w",
        encoding="utf-8"
    ) as f:

        f.write(
            f"成交量信号数量: {total_volume}\n"
        )

        f.write(
            f"成交量激增信号数量: {total_volume_surge}\n\n"
        )
        for line in out_lines:
            f.write(line)

    print("\n========== 模拟扫描结果 ==========")

    print(f"交易对: {symbol_name}")

    print(f"日期: {target_date}")

    print(f"成交量信号: {total_volume}")

    print(f"成交量激增信号: {total_volume_surge}")

    print(f"结果文件: {log_file}")

    if out_lines:

        print(
            f"\n=== {symbol_name} "
            f"{target_date} 信号明细 ==="
        )

        for line in out_lines:
            print(line)


# ============================================================
# 实时成交量诊断
# ============================================================

def debug_latest_volume(df, symbol_name):

    if len(df) < 5:
        print("历史K线不足5根，无法检测")
        return

    k0 = df.iloc[-1]

    k1 = df.iloc[-2]

    k2 = df.iloc[-3]

    k3 = df.iloc[-4]

    k4 = df.iloc[-5]

    sum_vol = (
        k2["vol"]
        + k3["vol"]
        + k4["vol"]
    )

    ratio = (
        k0["vol"] / sum_vol
        if sum_vol > 0 else 0
    )

    print("\n========== 实时成交量诊断 ==========")

    print(f"交易对: {symbol_name}")

    print(f"K0 时间: {k0['ts']}")

    print(f"K0 收盘价: {k0['close']:,.2f}")

    print(f"K0 成交量: {k0['vol']:,.2f}")

    print(f"K1 时间: {k1['ts']}")

    print(f"K1 成交量: {k1['vol']:,.2f}")

    print(f"K2 时间: {k2['ts']}")

    print(f"K2 成交量: {k2['vol']:,.2f}")

    print(f"K3 时间: {k3['ts']}")

    print(f"K3 成交量: {k3['vol']:,.2f}")

    print(f"K4 时间: {k4['ts']}")

    print(f"K4 成交量: {k4['vol']:,.2f}")

    print("------------------------------------")

    print(f"K2+K3+K4: {sum_vol:,.2f}")

    print(f"K0成交量: {k0['vol']:,.2f}")

    print(f"成交量倍数: {ratio:.2f}x")

    print(
        f"检测结果: "
        f"{'满足条件' if k0['vol'] >= sum_vol and sum_vol > 0 else '不满足条件'}"
    )

    print("====================================\n")


# ============================================================
# 实时成交量信号
# ============================================================

def check_volume_latest(df, symbol_name="BTC"):

    result = detect_volume_signal(df)

    if not result:

        print(
            f"{symbol_name} 最新已收盘K线无成交量信号"
        )

        return

    k0 = result["k0"]

    now_ts = result["ts"]

    print(
        f"{symbol_name} 检测到成交量信号 "
        f"{result['ratio']:.2f}倍"
    )

    # 检查冷却
    if not allow_signal(
        "volume",
        symbol_name,
        now_ts,
        VOLUME_COOLDOWN
    ):

        print(
            f"{symbol_name} 成交量信号已经发送或处于冷却中"
        )

        return

    ts1 = (
        k0["ts"] - BAR_DURATION
    ).strftime("%m-%d %H:%M")

    ts2 = k0["ts"].strftime("%m-%d %H:%M")

    msg = f"📊 {symbol_name} 15m 成交量信号\n"

    msg += f"⏰ 时间: {ts1} ~ {ts2}\n"

    msg += f"💰 价格: {k0['close']:,.2f}\n"

    msg += f"📉 涨跌幅: {k0['change_pct']:,.2f}%\n\n"

    msg += f"🔥 当前成交量: {result['k0_vol']:,.2f}\n"

    msg += f"⚡ 成交量倍数: {result['ratio']:.2f}x\n\n"

    msg += (
        f"K2+K3+K4: {result['sum_vol']:,.2f}\n"
    )

    msg += "🔴 条件: K0 >= K2+K3+K4"

    # 只有发送成功才记录状态
    if send_message(msg):

        mark_signal_sent(
            "volume",
            symbol_name,
            now_ts
        )

        log_file = (
            f"{symbol_name}_15m_volume_signal.txt"
        )

        with open(
            log_file,
            "a",
            encoding="utf-8"
        ) as f:

            f.write(msg + "\n\n")

        print(
            f"已发送 {symbol_name} 实时成交量信号"
        )


# ============================================================
# 实时成交量激增信号（K0 >= K1 * 3.5）
# ============================================================

def check_volume_surge_latest(df, symbol_name="BTC"):

    result = detect_volume_surge_signal(df)

    if not result:

        print(
            f"{symbol_name} 最新已收盘K线无成交量激增信号"
        )

        return

    k0 = result["k0"]

    now_ts = result["ts"]

    print(
        f"{symbol_name} 检测到成交量激增信号 "
        f"{result['ratio']:.2f}倍"
    )

    # 检查冷却
    if not allow_signal(
        "volume_surge",
        symbol_name,
        now_ts,
        VOLUME_COOLDOWN
    ):

        print(
            f"{symbol_name} 成交量激增信号已经发送或处于冷却中"
        )

        return

    ts1 = (
        k0["ts"] - BAR_DURATION
    ).strftime("%m-%d %H:%M")

    ts2 = k0["ts"].strftime("%m-%d %H:%M")

    msg = f"🚨 {symbol_name} 15m 成交量激增信号\n"

    msg += f"⏰ 时间: {ts1} ~ {ts2}\n"

    msg += f"💰 价格: {k0['close']:,.2f}\n"

    msg += f"📉 涨跌幅: {k0['change_pct']:,.2f}%\n\n"

    msg += f"🔥 当前成交量: {result['k0_vol']:,.2f}\n"

    msg += f"📊 前1根成交量: {result['k1_vol']:,.2f}\n"

    msg += f"⚡ 激增倍数: {result['ratio']:.2f}x\n\n"

    msg += (
        f"🔴 条件: K0 >= K1 × {VOLUME_SURGE_RATIO}"
    )

    # 只有发送成功才记录状态
    if send_message(msg):

        mark_signal_sent(
            "volume_surge",
            symbol_name,
            now_ts
        )

        log_file = (
            f"{symbol_name}_15m_volume_surge_signal.txt"
        )

        with open(
            log_file,
            "a",
            encoding="utf-8"
        ) as f:

            f.write(msg + "\n\n")

        print(
            f"已发送 {symbol_name} 实时成交量激增信号"
        )

# ============================================================
# 历史成交量诊断
# ============================================================

def debug_volume(
    df,
    target_date,
    symbol_name="BTC"
):

    target_start = pd.Timestamp(target_date)

    target_end = (
        target_start + pd.Timedelta(minutes=15)
    )

    print(f"\n=== {symbol_name} 数据诊断 ===")

    print(f"总K线数: {len(df)}")

    print(
        f"数据范围: {df['ts'].min()} ~ {df['ts'].max()}"
    )

    print(f"目标日期: {target_date}")

    if (
        df["ts"].max() < target_start
        or df["ts"].min() >= target_end
    ):

        print(
            f"目标日期 {target_date} 不在数据范围内"
        )

        return

    print(
        f"\n=== {symbol_name} "
        f"{target_date} 全部K线成交量对比 ==="
    )

    print(
        f"{'时间':<18}"
        f"{'K0成交量':>16}"
        f"{'K2+K3+K4':>16}"
        f"{'比值':>10}"
        f"{'满足?':>10}"
    )

    print("-" * 75)

    for i in range(4, len(df)):

        k0 = df.iloc[i]

        if not (
            target_start <= k0["ts"] < target_end
        ):
            continue

        k2 = df.iloc[i - 2]

        k3 = df.iloc[i - 3]

        k4 = df.iloc[i - 4]

        sum_vol = (
            k2["vol"]
            + k3["vol"]
            + k4["vol"]
        )

        ratio = (
            k0["vol"] / sum_vol
            if sum_vol > 0 else 0
        )

        ok = (
            "是"
            if sum_vol > 0 and k0["vol"] >= sum_vol
            else "否"
        )

        ts = k0["ts"].strftime("%m-%d %H:%M")

        print(
            f"{ts:<18}"
            f"{k0['vol']:>16,.2f}"
            f"{sum_vol:>16,.2f}"
            f"{ratio:>10.2f}"
            f"{ok:>10}"
        )


# ============================================================
# 实时模式
# ============================================================

def run_live(df, symbol_name):

    print("\n========== 实时监控 ==========")

    print(f"交易对: {symbol_name}")

    print(f"原始K线数量: {len(df)}")

    # 统计未收盘K线
    unclosed = df[
        df["confirm"] != "1"
    ]

    print(f"未收盘K线数量: {len(unclosed)}")

    # 只保留已收盘K线
    closed_df = df[
        df["confirm"] == "1"
    ].copy()

    closed_df = (
        closed_df.sort_values("ts")
        .reset_index(drop=True)
    )

    if len(closed_df) < 6:

        print("已收盘K线不足6根，无法检测")

        return

    print(
        f"已收盘K线数量: {len(closed_df)}"
    )

    latest = closed_df.iloc[-1]

    print(
        f"最新已收盘K线: {latest['ts']}"
    )

    print(
        f"最新收盘价格: {latest['close']:,.2f}"
    )

    print(
        f"最新成交量: {latest['vol']:,.2f}"
    )

    print(
        f"最新涨跌幅: {latest['change_pct']:+.2f}%"
    )

    # 实时成交量诊断
    debug_latest_volume(
        closed_df,
        symbol_name
    )

    # 检测成交量信号
    check_volume_latest(
        closed_df,
        symbol_name
    )

    # 检测成交量激增信号（K0 >= K1*3.5）
    check_volume_surge_latest(
        closed_df,
        symbol_name
    )

    print("\n========== 实时检测完成 ==========")


# ============================================================
# 参数
# ============================================================

def parse_args():

    parser = argparse.ArgumentParser(
        description=(
            "加密货币 15m 布林 + 成交量 信号策略"
        ),
        formatter_class=argparse.RawTextHelpFormatter
    )

    parser.add_argument(
        "symbol",
        help="交易对，例如 BTC / ETH / SOL"
    )

    parser.add_argument(
        "--live",
        action="store_true",
        help="实时模式：检测最新已收盘K线并推送Telegram"
    )

    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="模拟模式指定日期 YYYY-MM-DD"
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="显示历史成交量诊断"
    )

    return parser.parse_args()


# ============================================================
# 主程序
# ============================================================

def main():

    args = parse_args()

    symbol = args.symbol.upper()

    if symbol not in SYMBOL_MAP:

        print(f"不支持的交易对: {symbol}")

        print(
            f"支持的交易对: {', '.join(SYMBOL_MAP.keys())}"
        )

        sys.exit(1)

    inst_id = SYMBOL_MAP[symbol]

    bar = BAR_INTERVAL

    print("=" * 60)

    print(f"{symbol} {bar} 策略启动")

    print(f"交易对: {inst_id}")

    print(
        f"北京时间: {datetime.utcnow() + timedelta(hours=8)}"
    )

    mode = (
        "实时模式"
        if args.live
        else "模拟模式"
    )

    print(f"运行模式: {mode}")

    # 日期处理
    if args.date:

        try:

            target_date = datetime.strptime(
                args.date,
                "%Y-%m-%d"
            ).date()

        except ValueError:

            print(
                f"--date 格式错误: {args.date}"
            )

            sys.exit(1)

    else:

        target_date = (
            datetime.utcnow() + timedelta(hours=8)
        ).date()

    if not args.live:

        print(f"目标日期: {target_date}")

    print("=" * 60)

    try:

        # 获取K线
        df = get_candles(
            inst_id,
            bar
        )

        if df.empty:

            print("没有获取到K线数据")

            return

        # 指标计算
        df = add_indicators(df)

        print(
            f"获取K线数量: {len(df)}"
        )

        # 实时模式
        if args.live:

            run_live(
                df,
                symbol
            )

        # 模拟模式
        else:

            scan_date(
                df,
                symbol,
                target_date,
                debug=args.debug
            )

    except Exception as e:

        print(f"运行出错: {e}")

        traceback.print_exc()


if __name__ == "__main__":
    # send_message("OKX 15m 策略启动成功")
    main()