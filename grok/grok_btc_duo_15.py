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

SIGNAL_COOLDOWN = timedelta(minutes=120)
last_signal_time = {}

CHAT_ID = "-4836241115"
TOKEN = "8444348700:AAGqkeUUuB_0rI_4qIaJxrTylpRGh020wU0"
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
LOG_FILE = "btc_4h_new_signal.txt"


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
def get_candles(instId="BTC-USDT", bar="4H", limit=1000):
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

    df["is_bull"] = df["close"] > df["open"]
    df["is_bear"] = df["close"] < df["open"]
    df["mid_price"] = (df["close"] + df["open"]) / 2
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
    if len(sub) < 25:
        return []

    signals = []

    k_now = sub.iloc[-1]
    k_prev = sub.iloc[-2]

    body = abs(k_now["close"] - k_now["open"])
    pct = abs(k_now["close"] - k_now["open"]) / k_now["open"]

    upper_shadow = k_now["high"] - max(k_now["open"], k_now["close"])
    lower_shadow = min(k_now["open"], k_now["close"]) - k_now["low"]

    now_ts = k_now["ts"]

    # ===============================
    # 工具函数：实体突破判断
    # ===============================
    def body_break_upper(k):
        return k["open"] > k["upper"] or k["close"] > k["upper"]

    def body_break_lower(k):
        return k["open"] < k["lower"] or k["close"] < k["lower"]

    # ===============================
    # 信号1：看空 双K实体突破上轨
    # ===============================
    if k_prev["is_bull"] and k_now["is_bear"]:
        if body_break_upper(k_prev) and body_break_upper(k_now):

            name = "信号1 看空 双K实体突破上轨"

            if allow_signal(name, now_ts):
                signals.append(name)

    # ===============================
    # 信号2：看多 双K实体突破下轨
    # ===============================
    if k_prev["is_bear"] and k_now["is_bull"]:
        if body_break_lower(k_prev) and body_break_lower(k_now):

            name = "信号2 看多 双K实体突破下轨"

            if allow_signal(name, now_ts):
                signals.append(name)

    # ===============================
    # 信号3：看多 强承接下影结构
    # ===============================
    if body > 0:
        if pct >= 0.002:
            # 必须：下影线 > 实体 且 下影线 > 上影线
            if lower_shadow > body and lower_shadow > upper_shadow  and k_now["is_bull"] and k_prev["low"] < k_prev["lower"]:
                ratio = lower_shadow / body
                level = None
                if ratio >= 3:
                    level = "3倍下影(强)"
                elif ratio >= 2:
                    level = "2倍下影(中)"
                elif ratio >= 1:
                    level = "1倍下影(弱)"

                if level:

                    name = f"信号3 看多 承接结构 + {level}"

                    if allow_signal(name, now_ts):
                        signals.append(name)

    # ===============================
    # 信号4：看空 上方压制结构
    # ===============================
    if body > 0:
        if pct >= 0.002:
            if (
                    upper_shadow > body and
                    upper_shadow > lower_shadow and
                    k_now["high"] > k_now["upper"]
            ):

                ratio = upper_shadow / body
                level = None
                if ratio >= 3:
                    level = "3倍上影(强)"
                elif ratio >= 2:
                    level = "2倍上影(中)"
                elif ratio >= 1:
                    level = "1倍上影(普通)"

                if level:
                    name = f"信号4 看空 压制结构 + {level}"
                    if allow_signal(name, now_ts):
                        signals.append(name)

    # ===============================
    # 信号5：看空 三K顶部转弱结构
    # ===============================
    if len(sub) >= 15:

        k1 = sub.iloc[-3]   # 阳线
        k2 = sub.iloc[-2]   # 阴线
        k3 = sub.iloc[-1]   # 阴线

        # 结构必须：阳 + 阴 + 阴
        if k1["is_bull"] and k2["is_bear"] and k3["is_bear"]:

            # 条件1：阳线是前10根最高价
            prev10 = sub.iloc[-13:-3]

            if not prev10.empty:
                if k1["high"] >= prev10["high"].max():

                    # 条件2：mid_price 持续下降
                    if k3["close"] < (k1["open"] + k1["close"]) / 2:
                        name = "信号5 看空 三K顶部转弱结构"
                        if allow_signal(name, k3["ts"]):
                            signals.append(name)

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
def check_k_now(df):
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
    print("BTC 1H 新策略启动")

    df = get_candles()
    df = add_indicators(df)

    scan_history(df)
    check_k_now(df)


if __name__ == "__main__":
    main()
