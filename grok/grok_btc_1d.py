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

CHAT_ID = "-5264477303"
TOKEN = "8444348700:AAGqkeUUuB_0rI_4qIaJxrTylpRGh020wU0"
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
LOG_FILE = "btc_1d_new_signal.txt"


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
def get_candles(instId="BTC-USDT", bar="1D", limit=1000):
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
    if len(sub) < 10:
        return []

    signals = []

    # 取三根K线
    k1 = sub.iloc[-3]  # 第一根阳线
    k2 = sub.iloc[-2]  # 第二根阳线
    k3 = sub.iloc[-1]  # 阴线

    now_ts = k3["ts"]

    # ===============================
    # 信号1：看空 2阳后接阴 + 第二根阳线是前6根最高点
    # ===============================
    if k1["is_bull"] and k2["is_bull"] and k3["is_bear"]:

        # 前6根K线（包含k2）
        prev6 = sub.iloc[-7:-1]  # 取到k2

        if not prev6.empty:

            if k2["high"] >= prev6["high"].max():

                name = "信号1 看空 2阳后接阴 + 第2阳线6K最高"

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
            ts = k["ts"].strftime("%Y-%m-%d %H:%M")

            text = f"{ts} | BTC {k['close']:,.2f} | {k['change_pct']:,.2f} \n"
            for s in sigs:
                text += f" - {s}\n"
            text += "-" * 30 + "\n"

            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(text)

            total += 1

    print("历史信号数量:", total)


# ==================== 实时检测 ====================
def check_k_now(df):
    sub = df.iloc[:-1]          # 用于检测
    sigs = detect_signals(sub)

    if not sigs:
        print("最新K线无信号")
        return

    k = df.iloc[-1]             # 取最后一根K线（单行）
    ts = k["ts"].strftime("%m-%d %H:%M")

    msg = "BTC 1D 新信号触发\n"
    msg += f"{ts}\n"
    msg += f"价格: {k['close']:,.2f}\n\n"
    msg += f"涨幅: {k['change_pct']:,.2f}\n\n"

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
    check_k_now(df)
    scan_history(df)


if __name__ == "__main__":
    main()
