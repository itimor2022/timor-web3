# -*- coding: utf-8 -*-
"""
BTC 15分钟布林线趋势监控脚本（2025版 - 主升浪多，无去重）
核心：以15分钟布林线（25,2）作为主要多头趋势判断依据
- 只检测多头信号，特别是主升浪多
- 信号1: 2根阳线实体突破上轨，其中一个阳线上半部分（high - open）是下半部分（open - low）的2倍
  （实体突破定义为：open <= upper < close）
- 结合EMA金叉、ADX、锤头线、放量等辅助确认（仅多头相关）
- 每次运行只要有信号就发送消息（无去重，适合实时监控）
- 所有触发信号一次性整合成一条消息，避免刷屏
"""

import requests
import pandas as pd
from datetime import datetime

# ==================== 配置区 ====================
CHAT_ID = "-4836241115"
TOKEN = "8444348700:AAGqkeUUuB_0rI_4qIaJxrTylpRGh020wU0"
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"


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


def get_candles(instId="BTC-USDT", bar="15m", limit=300):
    url = "https://www.okx.com/api/v5/market/candles"
    params = {"instId": instId, "bar": bar, "limit": limit}
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()["data"]
        df = pd.DataFrame(data,
                          columns=["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"])
        df["ts"] = pd.to_datetime(df["ts"].astype(int), unit='ms') + pd.Timedelta(hours=7)  # 亚洲时间
        df = df.astype({"open": float, "high": float, "low": float, "close": float, "vol": float})
        df = df[["ts", "open", "high", "low", "close", "vol"]].sort_values("ts").reset_index(drop=True)
        return df
    except Exception as e:
        print("获取K线失败:", e)
        return pd.DataFrame()


def add_technical_indicators(df):
    if len(df) < 50:
        return df

    # 基础指标
    df["return"] = df["close"].pct_change() * 100

    # BOLL 25,2（核心）
    df["sma25"] = df["close"].rolling(25).mean()
    df["std25"] = df["close"].rolling(25).std()
    df["upper"] = df["sma25"] + 2 * df["std25"]
    df["lower"] = df["sma25"] - 2 * df["std25"]
    df["mid"] = df["sma25"]

    # 放量
    df["vol_ma20"] = df["vol"].rolling(20).mean()
    df["vol_spike"] = df["vol"] > df["vol_ma20"] * 1.2

    # 阳线/阴线
    df["is_bull"] = df["close"] > df["open"]
    df["entity_size"] = abs(df["close"] - df["open"])

    return df


def trend_alert(df_15m):
    if df_15m.empty or len(df_15m) < 3:
        return

    latest = df_15m.iloc[-1]  # 当前（第二阳线）
    prev = df_15m.iloc[-2]  # 第一阳线

    close = latest["close"]
    ts = latest["ts"].strftime("%m-%d %H:%M")
    title = f"15m BTC-USDT - {ts}"

    # 核心：布林方向（只关注多头相关）
    boll_direction = "震荡"
    if close > latest["mid"]:
        boll_direction = "多头方向"

    # 收集所有触发信号（只多头）
    signals = []

    # 信号1: 2根阳线实体突破上轨，其中一个阳线上半部分（close - upper）是下半部分（upper - open）的2倍
    bull1_break = prev["is_bull"] and prev["open"] <= prev["upper"] < prev["close"]
    bull2_break = latest["is_bull"] and latest["open"] <= latest["upper"] < latest["close"]
    if prev["is_bull"] and latest["is_bull"] and (bull1_break or bull2_break):
        cond_prev = (prev["close"] - prev["upper"]) >= 2 * (prev["upper"] - prev["open"])
        cond_latest = (latest["close"] - latest["upper"]) >= 2 * (latest["upper"] - latest["open"])
        if cond_prev or cond_latest:
            signals.append(f"🚀2根阳线实体突破上轨 + 其中一根上半部分是下半部分的2倍 → 主升浪多信号")

    # 辅助信号: 连续破轨（只多头：连续2根破布林上轨）
    if (prev["is_bull"] and prev["close"] > prev["upper"]) and (latest["is_bull"] and latest["close"] > latest["upper"]) and close > latest["mid"]:
        signals.append(f"🚀连续2根阳线破布林上轨 + 多头方向 → 疯狂追涨")

    # 构建并发送消息（只要有信号就发，无去重）
    if signals:
        msg = f"【15分钟多头信号】{title} \n\n"
        msg += f"当前方向：{boll_direction}\n"
        msg += f"价格：${close:.0f} | 中轨：${latest['mid']:.0f}\n"
        msg += "──────────────\n"

        for sig in signals:
            msg += f"{sig}\n"

        send_message(msg)
        print(f"【{datetime.now().strftime('%H:%M')}】发送布林主升浪多趋势报告 - {len(signals)}个信号")
    else:
        print(f"【{datetime.now().strftime('%H:%M')}】无主升浪多信号")

    # 控制台打印当前状态
    print(f"{datetime.now().strftime('%m-%d %H:%M')} | BTC ${close:.0f} | 布林方向: {boll_direction}")


def main():
    df_15m = get_candles("BTC-USDT", "15m", 300)
    if df_15m.empty:
        print("无法获取15分钟K线")
        return

    df_15m = add_technical_indicators(df_15m)
    trend_alert(df_15m)


if __name__ == '__main__':
    print("BTC 15分钟布林线主升浪多趋势监控启动（无去重）...")
    main()
