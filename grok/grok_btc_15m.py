# -*- coding: utf-8 -*-
"""
BTC 15分钟 新布林信号策略
信号1：
✔ 当前阴线
✔ 阴线下穿上轨
✔ 前一根是阳线
✔ 前一根阳线 = 最近10根最高点
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

CHAT_ID = "-5264477303"
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


# ==================== 信号检测 ====================
def detect_signals(sub):
    print(sub)
    if len(sub) < 40:
        return []

    if "vol" not in sub.columns:
        return []

    signals = []

    k_now = sub.iloc[-1]
    last3 = sub.iloc[-3:]
    now_ts = k_now["ts"]
    cond_bull = k_now["open"] < k_now["close"]
    cond_bear = k_now["close"] < k_now["open"]
    cond_break_lower = k_now["close"] < k_now["lower"]
    cond_three_bear = (last3["close"] < last3["open"]).all()

    # # =========================
    # # 信号1：做空 放量跌破下轨
    # # =========================
    # if cond_bear and cond_break_lower:
    #
    #     prev_bull = None
    #     for i in range(len(sub) - 2, -1, -1):
    #         k = sub.iloc[i]
    #         if k["close"] > k["open"]:
    #             prev_bull = k
    #             break
    #
    #     if prev_bull is not None:
    #         cond_vol = k_now["vol"] >= prev_bull["vol"] * 4.3
    #
    #         if cond_vol:
    #             name = "信号1 做空 放量5倍阴线跌破下轨"
    #             if allow_signal(name, now_ts):
    #                 signals.append(name)
    #
    # # =========================
    # # 信号2：做多 4K中轨上方结构
    # # =========================
    # last4 = sub.iloc[-4:]
    #
    # # ≥3阳线
    # bull_count = (last4["close"] > last4["open"]).sum()
    # cond_bull3 = bull_count >= 3
    #
    # # 4根最高价都在中轨上
    # cond_high_above_mid = (last4["high"] > last4["mid"]).all()
    #
    # # ≥3根开盘价在中轨上
    # open_above_mid_count = (last4["mid_price"] > last4["mid"]).sum()
    # cond_open3 = open_above_mid_count >= 3
    #
    # # 第一根从中轨下方启动
    # k1 = last4.iloc[0]
    # cond_first_low_below_mid = k1["low"] < k1["mid"] and k1['close'] > k1['open']
    #
    # if (cond_bull3 and cond_bull and
    #         cond_high_above_mid and
    #         cond_open3 and
    #         cond_first_low_below_mid):
    #
    #     name = f"信号2 做多 4K中轨上方强势结构({bull_count}阳)"
    #     if allow_signal(name, now_ts):
    #         signals.append(name)
    #
    # # =========================
    # # 信号3：三阴做空结构
    # # =========================
    #
    # if cond_three_bear:
    #
    #     # 条件2：至少一根阴线实体穿越中轨
    #     cross_mid = (
    #             (last3["open"] > last3["mid"]) &
    #             (last3["close"] > last3["lower"]) &
    #             (last3["close"] < last3["mid"])
    #     ).any()
    #
    #     # 条件3：mid_price 连续降低
    #     mp1, mp2, mp3 = last3["mid_price"].values
    #     cond_mid_price_down = mp1 > mp2 > mp3
    #
    #     if cross_mid and cond_mid_price_down:
    #         name = "信号3 做空 三阴下压穿中轨"
    #         if allow_signal(name, now_ts):
    #             signals.append(name)
    #
    # # =========================
    # # 信号4：长上影线反转做空
    # # =========================
    # body = abs(k_now["close"] - k_now["open"])
    # upper_shadow = k_now["high"] - max(k_now["open"], k_now["close"])
    # lower_shadow = min(k_now["open"], k_now["close"]) - k_now["low"] + 0.00000001
    #
    # cond_shadow_exist = body > 0  # 防止十字星除零
    # cond_upper_3x_body = upper_shadow >= body * 1.77 if cond_shadow_exist else False
    # cond_upper_4x_lower = upper_shadow >= lower_shadow * 4 if lower_shadow > 0 else True
    #
    # if k_now["high"] >= k_now["upper"] and cond_upper_3x_body and cond_upper_4x_lower:
    #     name = "信号4 做空 超长上影线压制"
    #     if allow_signal(name, now_ts):
    #         signals.append(name)

    # =========================
    # 信号5：阳线高点下降结构
    # =========================

    p2, p1 = find_last_two_bull_peaks(sub, n=3)

    if p1 is not None and p2 is not None:

        high_prev = sub.iloc[p2]["high"]
        high_now = sub.iloc[p1]["high"]

        # 本次阳线高点 < 上一次阳线高点
        if high_now < high_prev and cond_bull:

            # 当前K线必须是阳线才触发
            if cond_bull:
                name = "信号5 做空 阳线高点下降(Lower High)"

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
