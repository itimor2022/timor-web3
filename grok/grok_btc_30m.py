# -*- coding: utf-8 -*-
"""
BTC 30分钟 布林信号策略
"""

import requests
import pandas as pd
from datetime import timedelta

# 设置显示参数
pd.set_option('display.max_columns', 1000)
pd.set_option('display.max_rows', 1000)
pd.set_option('display.width', 1000)
pd.set_option('display.max_colwidth', 1000)

SIGNAL_COOLDOWN = timedelta(minutes=30)
last_signal_time = {}

CHAT_ID = "-5068436114"
TOKEN = "8444348700:AAGqkeUUuB_0rI_4qIaJxrTylpRGh020wU0"
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
LOG_FILE = "btc_30m_signal.txt"
INST_ID = "BTC-USDT-SWAP"
BAR = "30m"


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


# ==================== 信号检测 ====================
def detect_signals(sub):
    # print(sub)
    if len(sub) < 40:
        return []

    if "vol" not in sub.columns:
        return []

    signals = []

    k1 = sub.iloc[-1]

    now_ts = k1["ts"]

    # 实体下穿中轨（开盘在上，收盘在下）
    cond_cross_mid_down = (
            (k1["open"] > k1["mid"]) &
            (k1["close"] < k1["mid"])
    )
    cond_cross_mid_up = (
            (k1["open"] < k1["mid"]) &
            (k1["close"] > k1["mid"])
    )

    if k1['is_bear'] and cond_cross_mid_down:
        # =========================
        # 单根跌幅
        # =========================
        single_drop = (k1["open"] - k1["close"]) / k1["open"] * 100

        if single_drop > 0.31:
            name = f"信号1 看空 单根暴跌 {single_drop:.2f}%"
            if allow_signal(name, now_ts):
                signals.append(name)

        # =========================
        # 连续累计跌幅
        # =========================
        consecutive = []

        for i in range(len(sub) - 1, -1, -1):
            k = sub.iloc[i]
            if k["close"] < k["open"]:
                consecutive.append(k)
            else:
                break

        if len(consecutive) >= 2:
            consecutive = consecutive[::-1]

            first_k = consecutive[0]
            last_k = consecutive[-1]

            total_drop = (first_k["open"] / last_k["close"] - 1) * 100

            if total_drop > 0.33:
                name = f"信号1 看空 连续{len(consecutive)}阴 累计跌幅{total_drop:.2f}%"
                if allow_signal(name, now_ts):
                    signals.append(name)

    if k1['is_bull'] and cond_cross_mid_up:
        # =========================
        # 单根跌幅
        # =========================
        single_drop = (k1["close"] / k1["open"] - 1) * 100

        if single_drop > 0.31:
            name = f"信号2 看多 单根暴涨 {single_drop:.2f}%"
            if allow_signal(name, now_ts):
                signals.append(name)

        # =========================
        # 连续累计跌幅
        # =========================
        consecutive = []

        for i in range(len(sub) - 1, -1, -1):
            k = sub.iloc[i]
            if k["close"] > k["open"]:
                consecutive.append(k)
            else:
                break

        if len(consecutive) >= 2:
            consecutive = consecutive[::-1]

            first_k = consecutive[0]
            last_k = consecutive[-1]

            total_drop = (first_k["close"] / last_k["open"] - 1) * 100

            if total_drop > 0.33:
                name = f"信号2 看多 连续{len(consecutive)}阳 累计涨幅{total_drop:.2f}%"
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
            ts1 = (k["ts"] - timedelta(minutes=30)).strftime("%m-%d %H:%M")
            ts2 = k["ts"].strftime("%m-%d %H:%M")

            text = f"{ts1} ~ {ts2} | BTC {k['close']:,.2f} | {k['vol']:,.2f} | {k['change_pct']:,.2f}% \n"

            for s in sigs:
                text += f" - {s}\n"
            text += "-" * 30 + "\n"

            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(text)

            total += 1

    print("历史信号数量:", total)


# ==================== 实时检测 ====================
def check_latest(df):
    sub = df.iloc[:-1]  # 用于检测
    sigs = detect_signals(sub)

    if not sigs:
        print("最新K线无信号")
        return

    k = df.iloc[-1]  # 取最后一根K线（单行）
    ts1 = (k["ts"] - timedelta(minutes=30)).strftime("%m-%d %H:%M")
    ts2 = k["ts"].strftime("%m-%d %H:%M")

    msg = "🚨 BTC 30m 新信号触发\n"
    msg += f"⏰ 时间: {ts1} ~ {ts2}\n"
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
    print("BTC 30M 新策略启动")

    df = get_candles()
    df = add_indicators(df)
    check_latest(df)
    scan_history(df)


if __name__ == "__main__":
    main()
