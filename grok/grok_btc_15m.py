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

    k_now = sub.iloc[-1]
    k_prev = sub.iloc[-2]
    k_prev2 = sub.iloc[-3]
    k_prev3 = sub.iloc[-4]
    last3 = sub.iloc[-4:-1]

    now_ts = k_now["ts"]
    cond_bull = k_now["open"] < k_now["close"]
    cond_bear = k_now["close"] < k_now["open"]
    cond_break_lower = k_now["close"] < k_now["lower"]
    cond_three_bear = (last3["close"] < last3["open"]).all()
    cond_prev_bear = k_prev["close"] < k_prev["open"]
    cond_bull_prev = k_prev["close"] > k_prev["open"]

    prev_body = abs(k_prev["close"] - k_prev["open"])
    now_body = abs(k_now["close"] - k_now["open"])

    # ==================================================
    # 信号1：最高收盘阳线 + 大阴线反包
    # ==================================================
    if cond_bear and cond_bull_prev:

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
    if cond_bear and cond_prev_bear:

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

                name = f"信号4 放量反转 {ratio:.1f}倍爆量 观察后续走势"

                if allow_signal(name, now_ts):
                    signals.append(name)

                break

    # =========================
    # 信号5：做空 放量跌破下轨
    # =========================
    if cond_bear and cond_break_lower:

        prev_bull = None
        for i in range(len(sub) - 2, -1, -1):
            k = sub.iloc[i]
            if k["close"] > k["open"]:
                prev_bull = k
                break

        if prev_bull is not None:
            cond_vol = k_now["vol"] >= prev_bull["vol"] * 4.3

            if cond_vol:
                name = "信号5 做空 放量5倍阴线跌破下轨"
                if allow_signal(name, now_ts):
                    signals.append(name)

    # =========================
    # 信号6：做多 4K中轨上方结构
    # =========================
    last4 = sub.iloc[-4:]

    # ≥3阳线
    bull_count = (last4["close"] > last4["open"]).sum()
    cond_bull3 = bull_count >= 4

    # 4根最高价都在中轨上
    cond_high_above_mid = (last4["high"] > last4["mid"]).all()

    # ≥3根开盘价在中轨上
    open_above_mid_count = (last4["mid_price"] > last4["mid"]).sum()
    cond_open3 = open_above_mid_count >= 4

    # 第一根从中轨下方启动
    k1 = last4.iloc[0]
    cond_first_low_below_mid = k1["low"] < k1["mid"] and k1['close'] > k1['open']

    if (cond_bull3 and cond_bull and
            cond_high_above_mid and
            cond_open3 and
            cond_first_low_below_mid):

        name = f"信号6 做多 4K阳中轨上方强势结构"
        if allow_signal(name, now_ts):
            signals.append(name)

    # =========================
    # 信号7：三阴做空结构
    # =========================

    if cond_three_bear:

        # 条件2：至少一根阴线实体穿越中轨
        cross_mid = (
                (k_prev2["open"] > k_prev2["mid"]) &
                (k_prev2["close"] > k_prev2["lower"]) &
                (k_prev2["close"] < k_prev2["mid"])
        ).any()

        # 条件3：mid_price 连续降低
        mp1, mp2, mp3 = last3["mid_price"].values
        cond_mid_price_down = mp1 > mp2 > mp3

        if cross_mid and cond_mid_price_down:
            name = "信号7 做空 三阴下压穿中轨"
            if allow_signal(name, now_ts):
                signals.append(name)

    # =========================
    # 信号8：长上影线反转做空
    # =========================
    body = abs(k_now["close"] - k_now["open"])
    upper_shadow = k_now["high"] - max(k_now["open"], k_now["close"])
    lower_shadow = min(k_now["open"], k_now["close"]) - k_now["low"] + 0.00000001

    cond_shadow_exist = body > 0  # 防止十字星除零
    cond_upper_3x_body = upper_shadow >= body * 1.77 if cond_shadow_exist else False
    cond_upper_4x_lower = upper_shadow >= lower_shadow * 4 if lower_shadow > 0 else True

    if k_now["high"] >= k_now["upper"] and cond_upper_3x_body and cond_upper_4x_lower:
        name = "信号8 做空 超长上影线压制"
        if allow_signal(name, now_ts):
            signals.append(name)

    # =========================
    # 信号9：做空 上破结构反杀
    # =========================
    if len(sub) >= 5:

        last3 = sub.iloc[-5:-2]   # 前3根K线

        # 当前阴线
        cond_now_bear = k_now["close"] < k_now["open"]

        # 开盘价 > 前3根
        cond_open_above_midprice = (
                (k_now["open"] > last3["open"]) &
                (k_now["open"] > last3["close"])
        ).all()

        # 收盘价 < 前3根
        cond_close_below_prev_close = (
                (now_body / prev_body > 1.68) &
                (k_now["close"] < last3["close"]) &
                (k_now["close"] < last3["open"])
        ).all()

        if cond_now_bear and cond_open_above_midprice and cond_close_below_prev_close:

            name = "信号9 做空 上破结构反杀"

            if allow_signal(name, now_ts):
                signals.append(name)

    # =========================
    # 信号10：做多 突破中轨强势启动
    # =========================
    if len(sub) >= 8:

        # 当前阳线
        cond_now_bull = k_now["close"] > k_now["open"]

        # 当前涨幅 > 0.4%
        cond_big_up = k_now["change_pct"]  > 0.2

        # 上穿中轨（开盘在下，收盘在上）
        cond_cross_mid = (
                k_now["open"] < k_now["mid"] and
                k_now["close"] > k_now["mid"]
        )

        # 前7根最高价全部低于中轨
        prev7 = sub.iloc[-8:-1]
        cond_prev_below_mid = (prev7["high"] < prev7["mid"]).all()

        if cond_now_bull and cond_big_up and cond_cross_mid and cond_prev_below_mid:

            name = "信号10 做多 强势上穿中轨启动"

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
    sub = df.iloc[:-1]          # 用于检测
    sigs = detect_signals(sub)

    if not sigs:
        print("最新K线无信号")
        return

    k = df.iloc[-1]             # 取最后一根K线（单行）
    ts = k["ts"].strftime("%m-%d %H:%M")

    msg = "BTC 15M 新信号触发\n"
    msg += f"{ts}\n"
    msg += f"价格: {k['close']:,.0f}\n\n"
    msg += f"涨幅: {k['change_pct']:,.0f}\n\n"

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
    check_latest(df)
    scan_history(df)


if __name__ == "__main__":
    main()
