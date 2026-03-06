# -*- coding: utf-8 -*-
"""
BTC 4小时 布林信号策略
"""

import requests
import pandas as pd
from datetime import timedelta

# 设置显示参数
pd.set_option('display.max_columns', 1000)
pd.set_option('display.max_rows', 1000)
pd.set_option('display.width', 1000)
pd.set_option('display.max_colwidth', 1000)

SIGNAL_COOLDOWN = timedelta(minutes=480)
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
    df["mid"] = df["close"].rolling(20).mean()
    df["std"] = df["close"].rolling(20).std()
    df["upper"] = df["mid"] + 2 * df["std"]
    df["lower"] = df["mid"] - 2 * df["std"]

    df["is_bull"] = df["close"] > df["open"]
    df["is_bear"] = df["close"] < df["open"]
    df["mid_price"] = (df["close"] + df["open"]) / 2
    # 涨幅
    df["change_pct"] = (df["close"] - df["open"]) / df["open"] * 100
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


def detect_signals(sub):
    if len(sub) < 25:
        return []

    signals = []

    k1 = sub.iloc[-1]
    k2 = sub.iloc[-2]

    prev_body = abs(k2["close"] - k2["open"])
    now_body = abs(k1["close"] - k1["open"])

    now_ts = k1["ts"]

    # ===============================
    # 信号1：看空 关键阳线转弱
    # ===============================
    if k1["is_bear"] and k1["mid_price"] < k2["mid_price"]:
        # 向前找最近阳线（允许上一根）
        target_bull = None
        # name = "信号1 看空 关键阳线转弱"
        #
        # if allow_signal(name, now_ts):
        #     signals.append(name)

        for i in range(len(sub) - 2, -1, -1):
            k = sub.iloc[i]

            if k["is_bull"]:
                target_bull = k
                bull_index = i
                break

        if target_bull is not None:

            # 条件2：该阳线是前10K最高收盘价
            if bull_index >= 6:

                prev10 = sub.iloc[bull_index - 6:bull_index]

                if target_bull["close"] >= prev10["close"].max():

                    name = "信号1 看空 关键阳线转弱"

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
            ts = k["ts"].strftime("%m-%d %H:%M")
            ts2 = (k["ts"] - timedelta(hours=4)).strftime("%m-%d %H:%M")

            text = f"{ts} ~ {ts2} | BTC {k['close']:,.2f} | {k['vol']:,.2f} | {k['change_pct']:,.2f}% \n"
            for s in sigs:
                text += f" - {s}\n"
            text += "-" * 30 + "\n"

            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(text)

            total += 1

    print("历史信号数量:", total)


# ==================== 实时检测 ====================
def check_k_now(df):
    sub = df.iloc[:-1]  # 用于检测
    sigs = detect_signals(sub)

    if not sigs:
        print("最新K线无信号")
        return

    k = df.iloc[-1]  # 取最后一根K线（单行）
    ts = k["ts"].strftime("%m-%d %H:%M")
    ts2 = (k["ts"] - timedelta(hours=4)).strftime("%m-%d %H:%M")

    msg = "🚨 BTC 4H 新信号触发\n"
    msg += f"⏰ 时间: {ts}\n"
    msg += f"💰 价格: {k['close']:,.2f}\n"
    msg += f"📊 成交量: {k['vol']:,.2f}\n"
    msg += f"📉 涨幅: {k['change_pct']:,.2f}%\n\n"

    for s in sigs:
        msg += f"🔴 {s} \n"

    send_message(msg)

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(msg + "\n")

    print("已发送实时信号")


# ==================== 主程序 ====================
def main():
    print("BTC 4H 新策略启动")

    df = get_candles()
    df = add_indicators(df)
    scan_history(df)
    check_k_now(df)


if __name__ == "__main__":
    main()
