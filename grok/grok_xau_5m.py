# -*- coding: utf-8 -*-
"""
黄金 XAU 永续 5分钟 布林信号策略
"""

import requests
import pandas as pd
from datetime import timedelta

pd.set_option('display.max_columns', 1000)

SIGNAL_COOLDOWN = timedelta(minutes=60)
last_signal_time = {}

CHAT_ID = "-4836241115"
TOKEN = "8444348700:AAGqkeUUuB_0rI_4qIaJxrTylpRGh020wU0"
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
LOG_FILE = "xau_5m_signal.txt"
INST_ID = "XAU-USDT-SWAP"
BAR = "5m"


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
def get_candles():
    url = "https://www.okx.com/api/v5/market/candles"

    r = requests.get(url, params={
        "instId": INST_ID,
        "bar": BAR,
        "limit": 1000
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


# ==================== 指标 ====================
def add_indicators(df):
    df["mid"] = df["close"].rolling(20).mean()
    df["std"] = df["close"].rolling(20).std()

    df["upper"] = df["mid"] + 2 * df["std"]
    df["lower"] = df["mid"] - 2 * df["std"]

    df["is_bull"] = df["close"] > df["open"]
    df["is_bear"] = df["close"] < df["open"]

    df["mid_price"] = (df["close"] + df["open"]) / 2

    df["change_pct"] = (df["close"] - df["open"]) / df["open"] * 100

    df["body"] = (df["close"] - df["open"]).abs()

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


# ==================== 信号检测 ====================
def detect_signals(sub):
    if len(sub) < 10:
        return []

    signals = []

    k1 = sub.iloc[-1]
    prev3 = sub.iloc[-4:-1]

    now_ts = k1["ts"]

    # ====================
    # 信号1 爆量
    # ====================

    avg_vol = prev3["vol"].mean()

    vol_ratio = k1["vol"] / avg_vol if avg_vol > 0 else 0

    if vol_ratio > 6:
        if vol_ratio >= 17.3:
            name = f"信号1 爆量 屌爆了啊 🧨🧨🧨 {vol_ratio:.2f}倍"
        elif vol_ratio >= 9.2:
            name = f"信号1 爆量 超级爆量 💢💢💢 {vol_ratio:.2f}倍"
        else:
            name = f"信号1 爆量 一般爆量 🟡🟡🟡 {vol_ratio:.2f}倍"

        if allow_signal(name, now_ts):
            signals.append(name)

    # ====================
    # 信号2 暴涨实体
    # ====================
    body_prev3 = sub.iloc[-11:-1]

    avg_body = body_prev3["body"].mean() + 0.000001

    body_ratio = k1["body"] / avg_body if avg_body > 0 else 0

    if body_ratio >= 4:

        if body_ratio >= 15:
            name = f"信号2 暴涨 炸裂 🚀🚀🚀 {body_ratio:.2f}倍"
        elif body_ratio >= 10:
            name = f"信号2 暴涨 超级 💥💥💥 {body_ratio:.2f}倍"
        else:
            name = f"信号2 暴涨 一般 🔥🔥🔥 {body_ratio:.2f}倍"

        if allow_signal(name, now_ts):
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

            ts1 = (k["ts"] - timedelta(minutes=5)).strftime("%m-%d %H:%M")
            ts2 = k["ts"].strftime("%m-%d %H:%M")

            text = f"{ts1} ~ {ts2} | XAU黄金 {k['close']:,.2f} | {k['vol']:,.2f} | {k['change_pct']:,.2f}%\n"

            for s in sigs:
                text += f" - {s}\n"

            text += "-" * 30 + "\n"

            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(text)

            total += 1

    print("历史信号数量:", total)


# ==================== 实时检测 ====================
def check_k_now(df):
    sub = df.iloc[:-1]

    sigs = detect_signals(sub)

    if not sigs:
        print("最新K线无信号")
        return

    k = sub.iloc[-1]

    ts1 = (k["ts"] - timedelta(minutes=5)).strftime("%m-%d %H:%M")
    ts2 = k["ts"].strftime("%m-%d %H:%M")

    msg = "🚨 XAU黄金 永续 5m 新信号\n"

    msg += f"⏰ {ts1} ~ {ts2}\n"
    msg += f"💰 价格 {k['close']:,.2f}\n"
    msg += f"📊 成交量 {k['vol']:,.2f}\n"
    msg += f"📉 涨幅 {k['change_pct']:,.2f}%\n\n"

    for s in sigs:
        msg += f"🔴 {s}\n"

    send_message(msg)

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(msg + "\n")

    print("已发送实时信号")


# ==================== 主程序 ====================
def main():
    print("XAU黄金 永续 5m 策略启动")

    df = get_candles()

    df = add_indicators(df)

    check_k_now(df)

    scan_history(df)


if __name__ == "__main__":
    main()
