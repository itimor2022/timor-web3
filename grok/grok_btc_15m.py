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

SIGNAL_COOLDOWN = timedelta(minutes=30)
last_signal_time = {}

CHAT_ID = "-5068436114"
TOKEN = "8444348700:AAGqkeUUuB_0rI_4qIaJxrTylpRGh020wU0"
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
def get_candles(instId="BTC-USDT", bar="15m", limit=1000):
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


# ==================== 信号检测 ====================
def detect_signals(sub):
    # print(sub)
    if len(sub) < 40:
        return []

    if "vol" not in sub.columns:
        return []

    signals = []

    k1 = sub.iloc[-1]
    k2 = sub.iloc[-2]
    k3 = sub.iloc[-3]
    k4 = sub.iloc[-4]
    last3 = sub.iloc[-3:]

    vol_now = k1["vol"]
    vol_prev = k2["vol"]

    now_ts = k1["ts"]
    cond_bull = k1["open"] < k1["close"]
    cond_bear = k1["close"] < k1["open"]
    cond_break_lower = k1["close"] < k1["lower"]
    cond_two_bear = (k1["is_bear"] and k2["is_bear"])
    cond_prev_bear = k2["close"] < k2["open"]
    cond_bull_prev = k2["close"] > k2["open"]

    prev_body = abs(k2["close"] - k2["open"])
    now_body = abs(k1["close"] - k1["open"])
    cond_small_body = prev_body / now_body

    # ==================================================
    # 信号1：最高收盘阳线（加强版 双K破高）
    # ==================================================
    window = sub.iloc[-10:-1]
    highest_close = window["high"].max()

    # 最近4根K线
    last4 = sub.iloc[-4:]

    # 统计突破次数
    break_count = 0

    for _, row in last4.iterrows():
        if row["high"] >= highest_close and row["high"] > row["upper"]:
            break_count += 1

    # 当前K线实体更大
    if k1["high"] >= highest_close and k1["high"] > k1["upper"]:

        # 普通信号
        name = "信号1 价格升天 最高收盘阳线"
        if allow_signal(name, now_ts):
            signals.append(name)

        # 加强报警：双K破高
        if break_count >= 2:
            strong_name = "信号11 加强版 双K破高"
            if allow_signal(strong_name, now_ts):
                signals.append(strong_name)

    # ==================================================
    # 信号2：两连阴 + 上轨突破回落
    # ==================================================
    if cond_bear and cond_prev_bear:

        hit_upper = False

        if k2["high"] > k2["upper"]:
            hit_upper = True
        if k3["high"] > k3["upper"]:
            hit_upper = True
        if k4["high"] > k4["upper"]:
            hit_upper = True

        if hit_upper:
            # 条件1：2连阳
            cond_two_bear = (
                    k2["is_bear"] and
                    k1["is_bear"]
            )

            # 条件2：最高点上穿上轨
            for n in [80, 50, 20]:
                if len(sub) < n + 4:
                    continue

                window = sub.iloc[-n - 1:-1]
                highest_high = window["high"].max()

                ref_high = max(
                    k2["high"] if k2["high"] > k2["upper"] else 0,
                    k4["high"] if k4["high"] > k4["upper"] else 0
                )

                if ref_high >= highest_high and cond_two_bear:

                    name = f"信号2 做空 {n}根最高点上穿上轨 + 两连阴"

                    if allow_signal(name, now_ts):
                        signals.append(name)
                    break

    # ==================================================
    # 信号3：最低点 + 跌破下轨 + 超级下影线
    # ==================================================
    if k1["low"] < k1["lower"]:

        # 下影线长度
        lower_shadow = min(k1["open"], k1["close"]) - k1["low"]

        # 前一根实体
        prev_body = abs(k2["close"] - k2["open"])

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

                if k1["low"] <= lowest_low:

                    name = f"信号3 做多 {n}根最低点 + 下破下轨 + {strength}"

                    if allow_signal(name, now_ts):
                        signals.append(name)

                    break

    # =========================
    # 信号4：二阴下跌做空
    # =========================

    if cond_two_bear:
        # 条件2：至少一根阴线实体穿越中轨
        cross_mid = (
                (last3["high"] > last3["mid"]) &
                (last3["low"] < last3["mid"])
        ).any()

        # 条件3：布林开口向下
        m1, m2, m3 = last3["mid"].values
        cond_mid_down = m1 > m2 > m3

        if cross_mid and cond_mid_down:
            name = "信号4 做空 二阴下压穿中轨"
            if allow_signal(name, now_ts):
                signals.append(name)

    # =========================
    # 信号5：长上影线反转做空
    # =========================
    body = abs(k1["close"] - k1["open"])
    upper_shadow = k1["high"] - max(k1["open"], k1["close"])
    lower_shadow = min(k1["open"], k1["close"]) - k1["low"] + 0.00000001

    cond_shadow_exist = body > 0  # 防止十字星除零
    cond_upper_3x_body = upper_shadow >= body * 1.77 if cond_shadow_exist else False
    cond_upper_4x_lower = upper_shadow >= lower_shadow * 4 if lower_shadow > 0 else True

    if k1["high"] >= k1["upper"] and cond_upper_3x_body and cond_upper_4x_lower:
        name = "信号5 做空 超长上影线压制"
        if allow_signal(name, now_ts):
            signals.append(name)

    # =========================
    # 信号6：做空 上破结构反杀
    # =========================
    if len(sub) >= 5:

        last3 = sub.iloc[-5:-2]  # 前3根K线

        # 当前阴线
        cond_now_bear = k1["close"] < k1["open"]

        # 开盘价 > 前3根
        cond_open_above_midprice = (
                (k1["open"] > last3["open"]) &
                (k1["open"] > last3["close"])
        ).all()

        # 收盘价 < 前3根
        cond_close_below_prev_close = (
                (now_body / prev_body > 1.68) &
                (k1["close"] < last3["close"]) &
                (k1["close"] < last3["open"])
        ).all()

        if cond_now_bear and cond_open_above_midprice and cond_close_below_prev_close:

            name = "信号6 做空 上破结构反杀"

            if allow_signal(name, now_ts):
                signals.append(name)

    # =========================
    # 信号7：3倍放量 观察反转
    # =========================
    if len(sub) >= 2 and vol_now > 110:
        if vol_prev > 0:
            vol_ratio = vol_now / vol_prev
        else:
            vol_ratio = 0

        if vol_ratio >= 3:

            direction = "阳线" if k1["is_bull"] else "阴线"

            name = f"信号7 放量{vol_ratio:.2f}倍 {direction} 观察反转"

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
            ts2 = (k["ts"] - timedelta(minutes=15)).strftime("%m-%d %H:%M")

            text = f"{ts} ~ {ts2} | BTC {k['close']:,.2f} | {k['vol']:,.2f} | {k['change_pct']:,.2f}% \n"

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
    ts = k["ts"].strftime("%m-%d %H:%M")
    ts2 = (k["ts"] - timedelta(minutes=5)).strftime("%m-%d %H:%M")

    msg = "🚨 BTC 5m 新信号触发\n"
    msg += f"⏰ 时间: {ts} ~ {ts2}\n"
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
    print("BTC 15M 新策略启动")

    df = get_candles()
    df = add_indicators(df)
    check_latest(df)
    scan_history(df)


if __name__ == "__main__":
    main()
