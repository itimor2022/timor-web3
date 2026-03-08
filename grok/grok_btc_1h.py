# -*- coding: utf-8 -*-
"""
BTC 1小时 布林信号策略
"""

import requests
import pandas as pd
from datetime import timedelta

# 设置显示参数
pd.set_option('display.max_columns', 1000)
pd.set_option('display.max_rows', 1000)
pd.set_option('display.width', 1000)
pd.set_option('display.max_colwidth', 1000)

SIGNAL_COOLDOWN = timedelta(minutes=240)
last_signal_time = {}

CHAT_ID = "-4850300375"
TOKEN = "8444348700:AAGqkeUUuB_0rI_4qIaJxrTylpRGh020wU0"
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
LOG_FILE = "btc_1h_new_signal.txt"


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
def get_candles(instId="BTC-USDT-SWAP", bar="1H", limit=1000):
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
    # bbq
    df["bbq"] = (df["mid_price"] - df["mid"]).abs()
    df["bbq_min"] = df["bbq"].rolling(7).min()
    # ema
    df["ema5"] = df["close"].ewm(span=5, adjust=False).mean()
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
    k3 = sub.iloc[-3]
    last_3 = sub.iloc[-3:]
    last_5 = sub.iloc[-5:]

    body = abs(k1["close"] - k1["open"])
    pct = abs(k1["close"] - k1["open"]) / k1["open"]

    upper_shadow = k1["high"] - max(k1["open"], k1["close"])
    lower_shadow = min(k1["open"], k1["close"]) - k1["low"]

    now_ts = k1["ts"]

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
    if k2["is_bull"] and k1["is_bear"]:
        if body_break_upper(k2) and body_break_upper(k1):

            name = "信号1 看空 双K实体突破上轨"

            if allow_signal(name, now_ts):
                signals.append(name)

    # ===============================
    # 信号2：看多 双K实体突破下轨
    # ===============================
    if k2["is_bear"] and k1["is_bull"]:
        if body_break_lower(k2) and body_break_lower(k1):

            name = "信号2 看多 双K实体突破下轨"

            if allow_signal(name, now_ts):
                signals.append(name)

    # ===============================
    # 信号3：看多 强承接下影结构
    # ===============================
    if body > 0:
        if pct >= 0.002:
            # 必须：下影线 > 实体 且 下影线 > 上影线
            if lower_shadow > body and lower_shadow > upper_shadow and k1["low"] < k1["lower"]:
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
                    k1["high"] > k1["upper"]
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
    # 信号5 看空 顶部强阳失守
    # ===============================
    recent_20 = sub.iloc[-21:-1]
    recent_5 = sub.iloc[-7:-1]

    if len(recent_20) >= 20 and len(recent_5) >= 5:

        # 20根中的最高点
        high_20 = recent_20["high"].max()

        # 判断最高点是否出现在最近5根中
        if recent_5["high"].max() == high_20:

            # 只找最近5根中的阳线
            bulls = recent_5[recent_5["close"] > recent_5["open"]]

            if not bulls.empty:

                bulls = bulls.copy()
                bulls["body_size"] = bulls["close"] - bulls["open"]

                # 找实体最大的阳线
                idx = bulls["body_size"].idxmax()
                ref_open = sub.loc[idx, "open"]

                # 当前必须阴线
                if k1["close"] < k1["open"] and k1["mid_price"] > k1["mid"]:

                    # 收盘跌破强阳开盘价
                    if k1["close"] < ref_open:

                        name = "信号5 看空 顶部强阳失守"

                        if allow_signal(name, now_ts):
                            signals.append(name)

    # ===============================
    # 信号6 看多 阳线强势上穿中轨
    # ===============================
    if k1["is_bull"]:

        # 涨幅 > 0.4%
        cond_big_up = k1["change_pct"] >= 0.25

        # 实体上穿中轨（开盘在下，收盘在上）
        cond_cross_mid = (
                (k1["open"] < k1["mid"]) &
                (k1["close"] > k1["mid"])
        )

        if cond_big_up and cond_cross_mid:

            name = "信号6 看多 阳线强势上穿中轨"

            if allow_signal(name, now_ts):
                signals.append(name)

    # ===============================
    # 信号7 看空 阴线强势下穿中轨
    # ===============================
    if k1["is_bear"]:

        # 跌幅 > 0.4%
        cond_big_down = k1["change_pct"] <= -0.25

        # 实体下穿中轨（开盘在上，收盘在下）
        cond_cross_mid_down = (
                (k1["open"] > k1["mid"]) &
                (k1["close"] < k1["mid"])
        )

        if cond_big_down and cond_cross_mid_down:

            name = "信号7 看空 阴线强势下穿中轨"

            if allow_signal(name, now_ts):
                signals.append(name)

    # ===============================
    # 信号8 看空 2连阴 + Boll开口向下
    # ===============================
    if len(sub) >= 25:
        # 条件1：2连阴
        cond_two_bear = (
                k2["is_bear"] and
                k1["is_bear"]
        )

        # 条件2：
        cond_boll_down = (
                k1["mid_price"] < k2["mid_price"]
        )

        # 条件2.1
        prev_cross = (
                k2["open"] > k2["mid"] and
                k2["close"] < k2["mid"]
        )

        now_cross = (
                k1["open"] > k1["mid"] and
                k1["close"] < k1["mid"]
        )

        # 条件3：收盘价在中轨下面
        cond_close_boll_mid = (
                (k1["close"] < k1["mid"]) &
                (k2["close"] < k2["mid"])
        )

        if cond_two_bear and cond_close_boll_mid and (cond_boll_down or prev_cross or now_cross):

            name = "信号8 看空 2连阴 + Boll向下"

            if allow_signal(name, k1["ts"]):
                signals.append(name)

    # ===============================
    # 信号9 看多 2连阳 + Boll开口向上
    # ===============================

    if len(sub) >= 25:
        # 条件1：2连阳
        cond_two_bull = (
                k2["is_bull"] and
                k1["is_bull"]
        )

        # 条件2：
        cond_boll_down = (
                k1["mid_price"] > k2["mid_price"]
        )

        # 条件2.1
        prev_cross = (
                k2["open"] < k2["mid"] and
                k2["close"] > k2["mid"]
        )

        now_cross = (
                k1["open"] < k1["mid"] and
                k1["close"] > k1["mid"]
        )

        # 条件3：收盘价在中轨上面
        cond_close_boll_mid = (
                (k1["close"] > k1["mid"]) &
                (k2["close"] > k2["mid"])
        )

        if cond_two_bull and cond_close_boll_mid and (cond_boll_down or prev_cross or now_cross):

            name = "信号9 看多 2连阳 + Boll向上"

            if allow_signal(name, k1["ts"]):
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
            ts1 = (k["ts"] - timedelta(hours=1)).strftime("%m-%d %H:%M")
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
    ts1 = (k["ts"] - timedelta(hours=1)).strftime("%m-%d %H:%M")
    ts2 = k["ts"].strftime("%m-%d %H:%M")

    msg = "🚨 BTC 1H 新信号触发\n"
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
    print("BTC 1H 新策略启动")

    df = get_candles()
    df = add_indicators(df)
    check_latest(df)
    scan_history(df)


if __name__ == "__main__":
    main()
