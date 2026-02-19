# -*- coding: utf-8 -*-
"""
BTC 15分钟 布林信号策略
"""

import requests
import pandas as pd
from datetime import timedelta

# 设置显示参数
pd.set_option('display.max_columns', 1000)
pd.set_option('display.max_rows', 1000)
pd.set_option('display.width', 1000)
pd.set_option('display.max_colwidth', 1000)

SIGNAL_COOLDOWN = timedelta(minutes=60)
last_signal_time = {}

CHAT_ID = "-5068436114"
TOKEN = "你的TOKEN"
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
LOG_FILE = "btc_15m_new_signal.txt"


# ==================== Telegram ====================
def send_message(msg):
    try:
        requests.get(
            f"{BASE_URL}/sendMessage",
            params={"chat_id": CHAT_ID, "text": msg},
            timeout=10
        )
    except:
        pass


# ==================== 获取K线 ====================
def get_candles(instId="BTC-USDT", bar="15m", limit=5000):
    url = "https://www.okx.com/api/v5/market/candles"

    r = requests.get(url, params={
        "instId": instId,
        "bar": bar,
        "limit": limit
    }, timeout=10)

    data = r.json()["data"]

    df = pd.DataFrame(data, columns=[
        "ts", "open", "high", "low", "close",
        "vol", "volCcy", "volCcyQuote", "confirm"
    ])

    df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms") + pd.Timedelta(hours=7)

    for c in ["open", "high", "low", "close", "vol"]:
        df[c] = df[c].astype(float)

    df = df.sort_values("ts").reset_index(drop=True)

    return df[["ts", "open", "high", "low", "close", "vol"]]


# ==================== 布林指标 ====================
def add_indicators(df):
    df["mid_price"] = (df["close"] + df["open"]) / 2
    df["mid"] = df["close"].rolling(20).mean()
    df["std"] = df["close"].rolling(20).std()
    df["upper"] = df["mid"] + 2 * df["std"]
    df["lower"] = df["mid"] - 2 * df["std"]
    return df


# ==================== 冷却 ====================
def allow_signal(name, ts):
    last_ts = last_signal_time.get(name)

    if last_ts is None:
        last_signal_time[name] = ts
        return True

    if ts - last_ts >= SIGNAL_COOLDOWN:
        last_signal_time[name] = ts
        return True

    return False


# ==================== 阳线波峰 ====================
def find_last_two_bull_peaks(df, n=3):
    peaks = []

    for i in range(n, len(df) - n):
        k = df.iloc[i]

        # 必须阳线
        if k["close"] <= k["open"]:
            continue

        h = k["high"]

        left = df.iloc[i - n:i]["high"].max()
        right = df.iloc[i + 1:i + n + 1]["high"].max()

        if h > left and h > right:
            peaks.append(i)

    if len(peaks) >= 2:
        return peaks[-2], peaks[-1]

    return None, None


def detect_signals(sub):
    if len(sub) < 120:
        return []

    if "vol" not in sub.columns:
        return []

    signals = []

    k_now = sub.iloc[-1]
    k_prev = sub.iloc[-2]
    k_prev2 = sub.iloc[-3]
    k_prev3 = sub.iloc[-4]

    now_ts = k_now["ts"]

    cond_now_bear = k_now["close"] < k_now["open"]
    cond_prev_bear = k_prev["close"] < k_prev["open"]
    cond_bull_prev = k_prev["close"] > k_prev["open"]

    # ==================================================
    # 信号1：最高收盘阳线 + 大阴线反包
    # ==================================================
    if cond_now_bear and cond_bull_prev:

        prev_body = abs(k_prev["close"] - k_prev["open"])
        now_body = abs(k_now["close"] - k_now["open"])

        if now_body > prev_body:

            for n in [80, 50, 20]:
                if len(sub) < n + 2:
                    continue

                window = sub.iloc[-n - 1:-1]
                highest_close = window["close"].max()

                if k_prev["close"] >= highest_close:

                    name = f"信号1 做空 {n}根最高收盘阳线 + 大阴线反包"

                    if allow_signal(name, now_ts):
                        signals.append(name)

                    break

    # ==================================================
    # 信号2：两连阴 + 上轨突破回落
    # ==================================================
    if cond_now_bear and cond_prev_bear:

        hit_upper = False

        if k_prev["high"] > k_prev["upper"]:
            hit_upper = True

        if k_prev3["high"] > k_prev3["upper"]:
            hit_upper = True

        if hit_upper:

            for n in [80, 50, 20]:
                if len(sub) < n + 4:
                    continue

                window = sub.iloc[-n - 1:-1]
                highest_high = window["high"].max()

                ref_high = max(
                    k_prev["high"] if k_prev["high"] > k_prev["upper"] else 0,
                    k_prev3["high"] if k_prev3["high"] > k_prev3["upper"] else 0
                )

                if ref_high >= highest_high:

                    name = f"信号2 做空 {n}根最高点上穿上轨 + 两连阴"

                    if allow_signal(name, now_ts):
                        signals.append(name)
                    break

    # ==================================================
    # 信号3：最低点 + 跌破下轨 + 超级下影线
    # ==================================================
    if k_now["low"] < k_now["lower"]:

        # 下影线长度
        lower_shadow = min(k_now["open"], k_now["close"]) - k_now["low"]

        # 前一根实体
        prev_body = abs(k_prev["close"] - k_prev["open"])

        if prev_body == 0:
            return signals

        # 计算倍数
        ratio = lower_shadow / prev_body

        strength = None
        if ratio >= 8:
            strength = "8倍超级下影"
        elif ratio >= 5:
            strength = "5倍超强下影"
        elif ratio >= 3:
            strength = "3倍强下影"

        if strength:

            for n in [80, 50, 20]:
                if len(sub) < n + 2:
                    continue

                window = sub.iloc[-n - 1:-1]
                lowest_low = window["low"].min()

                if k_now["low"] <= lowest_low:

                    name = f"信号3 做多 {n}根最低点 + 下破下轨 + {strength}"

                    if allow_signal(name, now_ts):
                        signals.append(name)

                    break

    # ==================================================
    # 信号4：放量反转（极限成交量）
    # ==================================================
    for n in [80, 50, 20]:
        if len(sub) < n + 3:
            continue

        window = sub.iloc[-n - 1:-1]
        max_vol = window["vol"].max()

        current_vol = k_now["vol"]

        # 当前必须是该级别最大量
        if current_vol >= max_vol:

            # 前3根最小成交量
            prev3 = sub.iloc[-4:-1]
            min_prev3_vol = prev3["vol"].min()

            if min_prev3_vol == 0:
                continue

            ratio = current_vol / min_prev3_vol

            if ratio >= 6:

                name = f"信号4 放量反转 {n}根最大量 + {ratio:.1f}倍爆量"

                if allow_signal(name, now_ts):
                    signals.append(name)

                break

    return signals


# ==================== 历史扫描 ====================
def scan_history(df):
    print("开始历史扫描...")
    total = 0
    open(LOG_FILE, "w").close()

    for i in range(30, len(df)):
        sub = df.iloc[:i + 1]
        sigs = detect_signals(sub)

        if sigs:
            k = sub.iloc[-1]
            ts = k["ts"].strftime("%Y-%m-%d %H:%M")

            text = f"{ts} | BTC {k['close']:,.0f}\n"
            for s in sigs:
                text += f" - {s}\n"
            text += "-" * 30 + "\n"

            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(text)

            total += 1

    print("历史信号数量:", total)


# ==================== 实时检测 ====================
def check_latest(df):
    sigs = detect_signals(df)

    if not sigs:
        print("最新K线无信号")
        return

    k = df.iloc[-1]
    ts = k["ts"].strftime("%m-%d %H:%M")

    msg = "BTC 15M 新信号触发\n"
    msg += f"{ts}\n"
    msg += f"价格: {k['close']:,.0f}\n\n"

    for s in sigs:
        msg += f"• {s}\n"

    send_message(msg)

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(msg + "\n")

    print("已发送实时信号")


# ==================== 主程序 ====================
def main():
    print("BTC 15M 新策略启动")

    df = get_candles()
    df = add_indicators(df)

    scan_history(df)
    check_latest(df)


if __name__ == "__main__":
    main()
