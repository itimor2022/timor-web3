# -*- coding: utf-8 -*-
"""
BTC 1小时布林线顶部反转做空策略脚本（2026版 - 只做空，无去重）
"""

import requests
import pandas as pd
from datetime import datetime

# ==================== 配置区 ====================
CHAT_ID = "-5264477303"
TOKEN = "你的TOKEN"
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"


# ==================== Telegram ====================
def send_message(msg):
    url = f"{BASE_URL}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        r = requests.get(url, params=payload, timeout=10)
        if not r.json().get("ok"):
            print("Telegram发送失败:", r.json())
    except Exception as e:
        print("发送异常:", e)


# ==================== K线 ====================
def get_candles(instId="BTC-USDT", bar="1H", limit=300):
    url = "https://www.okx.com/api/v5/market/candles"
    params = {"instId": instId, "bar": bar, "limit": limit}

    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()["data"]

        df = pd.DataFrame(data, columns=[
            "ts", "open", "high", "low", "close",
            "vol", "volCcy", "volCcyQuote", "confirm"
        ])

        df["ts"] = pd.to_datetime(df["ts"].astype(int), unit='ms') + pd.Timedelta(hours=7)
        df = df.astype({
            "open": float,
            "high": float,
            "low": float,
            "close": float,
            "vol": float
        })

        df = df[["ts", "open", "high", "low", "close", "vol"]]
        df = df.sort_values("ts").reset_index(drop=True)

        return df

    except Exception as e:
        print("获取K线失败:", e)
        return pd.DataFrame()


# ==================== 指标 ====================
def add_technical_indicators(df):
    if len(df) < 50:
        return df

    # 收益
    df["return"] = df["close"].pct_change() * 100

    # BOLL 25,2
    df["sma25"] = df["close"].rolling(25).mean()
    df["std25"] = df["close"].rolling(25).std()
    df["upper"] = df["sma25"] + 2 * df["std25"]
    df["lower"] = df["sma25"] - 2 * df["std25"]
    df["mid"] = df["sma25"]

    # K线属性
    df["is_bear"] = df["close"] < df["open"]
    df["is_bull"] = df["close"] > df["open"]

    # 成交量放量
    df["vol_spike_2x"] = df["vol"] >= df["vol"].shift(1) * 2
    df["vol_spike_3x"] = df["vol"] >= df["vol"].shift(1) * 3

    return df


# ==================== 信号 ====================
def trend_alert(df):
    if df.empty or len(df) < 6:
        return

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    close = latest["close"]
    ts = latest["ts"].strftime("%m-%d %H:%M")

    title = f"1H BTC-USDT - {ts}"

    if close < latest["mid"]:
        boll_direction = "空头方向"
    elif close > latest["mid"]:
        boll_direction = "多头方向"
    else:
        boll_direction = "震荡"

    signals = []

    # =========================
    # 信号1：上轨跳空阴线反转
    # =========================
    prev_is_bull = prev["is_bull"]
    current_is_bear = latest["is_bear"]

    entity_above_upper = min(latest["open"], latest["close"]) * 1.001 > latest["upper"]
    is_gap_up = latest["open"] > prev["close"]

    if prev_is_bull and current_is_bear and entity_above_upper and is_gap_up:
        drop_pct = (latest["open"] - latest["close"]) / latest["open"] * 100
        signals.append(
            f"⚠️ 信号1：顶部反转跳空阴线（实体在上轨上方，跌幅 {drop_pct:.1f}%）"
        )

    # =========================
    # 信号2：4K放量反转结构
    # =========================
    k1 = df.iloc[-1]
    k2 = df.iloc[-2]
    k3 = df.iloc[-3]
    k4 = df.iloc[-4]

    cond_k2_bear = k2["is_bear"]
    cond_k3_bear = k3["is_bear"]
    cond_k4_bull = k4["is_bull"]

    cond_vol2_2x = k2["vol"] >= k3["vol"] * 2
    cond_vol3_2x = k3["vol"] >= k4["vol"] * 2

    cond_one_3x = (
        k2["vol"] >= k3["vol"] * 3 or
        k3["vol"] >= k4["vol"] * 3
    )

    # 位置过滤：至少一根靠近上轨
    pos_filter = max(k2["high"], k3["high"]) > k2["upper"] * 0.995

    if cond_k2_bear and cond_k3_bear and cond_k4_bull \
            and cond_vol2_2x and cond_vol3_2x and cond_one_3x \
            and pos_filter:

        v2 = k2["vol"] / k3["vol"]
        v3 = k3["vol"] / k4["vol"]

        signals.append(
            f"⚠️ 信号2：4K放量反转（阴阴放量→阳） "
            f"量比 {v3:.1f}x / {v2:.1f}x"
        )

    # =========================
    # 标记系统
    # =========================
    mark_list = []

    if signals:

        # 连续阳线
        bulls = 0
        for i in range(2, len(df)):
            if df.iloc[-i]["is_bull"]:
                bulls += 1
            else:
                break

        if bulls >= 5:
            mark_list.append("1")
        elif bulls >= 3:
            mark_list.append("2")

        # 最近10根2倍量
        recent_10 = df.iloc[-10:]
        vol_spike_count = recent_10["vol_spike_2x"].sum()

        if vol_spike_count > 0:
            mark_list.append("3")

            last_non_spike = 0
            for j in range(1, len(df)):
                if not df.iloc[-j]["vol_spike_2x"]:
                    last_non_spike = df.iloc[-j]["vol"]
                    break

            signals.append(
                f"标记3：近10K有 {vol_spike_count} 次2倍量，上次常量 {last_non_spike:.0f}"
            )

    标记 = ",".join([f"标记{m}" for m in mark_list]) if mark_list else "无"

    # =========================
    # 发送
    # =========================
    if signals:
        msg = f"【1小时空头信号-{标记}】{title}\n\n"
        msg += f"方向：{boll_direction}\n"
        msg += f"现价：${close:,.0f}\n"
        msg += f"中轨：${latest['mid']:,.0f} 上轨：${latest['upper']:,.0f}\n"
        msg += "──────────────\n"

        for s in signals:
            msg += f"• {s}\n"

        send_message(msg)
        print(f"【{datetime.now().strftime('%H:%M')}】发送空头信号")

    else:
        print(f"【{datetime.now().strftime('%H:%M')}】暂无信号")

    print(f"{ts} | BTC ${close:,.0f} | {boll_direction} | 信号数:{len(signals)}")


# ==================== 主程序 ====================
def main():
    df = get_candles("BTC-USDT", "1H", 300)

    if df.empty:
        print("获取失败")
        return

    df = add_technical_indicators(df)
    trend_alert(df)


if __name__ == '__main__':
    print("BTC 1H 空头反转监控启动...")
    main()
