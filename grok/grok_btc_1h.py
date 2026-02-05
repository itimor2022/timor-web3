# -*- coding: utf-8 -*-
"""
BTC 1小时布林线顶部反转做空策略（完整版）
功能：
✔ 三个空头信号
✔ 实时报警
✔ 历史信号全扫描
✔ 全部历史信号写入txt
"""

import requests
import pandas as pd
from datetime import timedelta

SIGNAL_COOLDOWN = timedelta(hours=2)
last_signal_time = {}


# 设置显示参数
pd.set_option('display.max_columns', 1000)
pd.set_option('display.max_rows', 1000)
pd.set_option('display.width', 1000)
pd.set_option('display.max_colwidth', 1000)

# ==================== 配置 ====================
CHAT_ID = "-5264477303"
TOKEN = "你的TOKEN"
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
LOG_FILE = "btc_1h_short_signals.txt"


# ==================== Telegram ====================
def send_message(msg):
    url = f"{BASE_URL}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": "HTML"
    }
    try:
        requests.get(url, params=payload, timeout=10)
    except:
        pass


# ==================== 获取K线 ====================
def get_candles(instId="BTC-USDT", bar="1H", limit=1000):
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


# ==================== 指标 ====================
def add_indicators(df):
    df["sma25"] = df["close"].rolling(25).mean()
    df["std25"] = df["close"].rolling(25).std()
    df["upper"] = df["sma25"] + 2 * df["std25"]
    df["lower"] = df["sma25"] - 2 * df["std25"]

    df["is_bull"] = df["close"] > df["open"]
    df["is_bear"] = df["close"] < df["open"]
    df["mid_bear_high_price"] = (df["high"] + df["close"]) / 2
    df["mid_bull_high_price"] = (df["high"] + df["open"]) / 2
    df["mid_price"] = (df["close"] + df["open"]) / 2

    return df


# ==================== 三信号检测 ====================
def detect_signals(sub):
    signals = []
    latest = sub.iloc[-1]
    prev = sub.iloc[-2]
    now_ts = latest["ts"]

    # ===== 信号1 最高阳线被低开跌破 =====
    lookback = sub.tail(10)
    bulls = lookback[lookback["is_bull"]]

    if len(bulls) > 0:
        idx = bulls["high"].idxmax()
        ref_low = sub.loc[idx, "low"]

        if latest["is_bear"] and latest["mid_price"] < ref_low:
            name = "信号1 失守最高阳线"
            if allow_signal(name, now_ts):
                signals.append(name)

    # ===== 信号2 抓最低阴线突破 =====
    lookback = sub.tail(10)
    bears = lookback[lookback["is_bear"]]
    if len(bears) > 0:
        # 找到最低阴线
        idx = bears["low"].idxmin()
        # 取该阴线最高价
        ref_high = sub.loc[idx, "high"]
        # 突破触发
        if latest["is_bull"] and latest["mid_price"] > ref_high:
            name = "信号2 突破最低阴线"
            if allow_signal(name, now_ts):
                signals.append(name)

    return signals


def allow_signal(name, ts):
    last_ts = last_signal_time.get(name)

    if last_ts is None:
        last_signal_time[name] = ts
        return True

    if ts - last_ts >= SIGNAL_COOLDOWN:
        last_signal_time[name] = ts
        return True

    return False


# ==================== 历史扫描 ====================
def scan_history(df):
    print("开始历史扫描...")
    total = 0

    open(LOG_FILE, "w").close()

    for i in range(50, len(df)):
        sub = df.iloc[:i + 1]
        sigs = detect_signals(sub)

        if sigs:
            k = sub.iloc[-1]
            ts = k["ts"].strftime("%Y-%m-%d %H:%M")
            price = k["close"]

            text = f"{ts} | BTC ${price:,.0f}\n"
            for s in sigs:
                text += f" - {s}\n"
            text += "-" * 40 + "\n"

            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(text)

            total += 1

    print(f"历史信号数量: {total}")


# ==================== 实时检测 ====================
def check_latest(df):
    sigs = detect_signals(df)
    if not sigs:
        print("最新K线无信号")
        return

    k = df.iloc[-1]
    ts = k["ts"].strftime("%m-%d %H:%M")

    msg = f"【BTC 1H 空头信号】{ts}\n"
    msg += f"现价: ${k['close']:,.0f}\n"
    msg += f"上轨: ${k['upper']:,.0f}\n\n"

    for s in sigs:
        msg += f"• {s}\n"

    send_message(msg)

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(msg + "\n")

    print("已发送实时信号")


# ==================== 主程序 ====================
def main():
    print("BTC 1H 空头系统启动")

    df = get_candles()
    df = add_indicators(df)

    # 历史扫描
    scan_history(df)

    # 实时检测
    check_latest(df)


if __name__ == "__main__":
    main()
