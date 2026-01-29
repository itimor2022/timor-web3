# -*- coding: utf-8 -*-
"""
BTC 15分钟布林线趋势监控脚本（2025版 - 只做多，无去重）
核心：以15分钟布林线（25,2）作为主要多头趋势判断依据
- 只检测多头信号
- 信号1: 第一阳线上穿下轨，第二也是阳线，且第一阳线实体比上一根阴线实体大
- 信号2: 2根连续阳线直接从下轨碰到中轨（第一根开盘低于下轨，最后一根收盘突破中轨）
- 每次运行只要有信号就发送消息（无去重，适合实时监控）
"""

import requests
import pandas as pd
from datetime import datetime

# ==================== 配置区 ====================
CHAT_ID = "-5068436114"
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

    # 阳线/阴线
    df["is_bull"] = df["close"] > df["open"]
    df["entity_size"] = abs(df["close"] - df["open"])

    return df


def trend_alert(df_15m):
    if df_15m.empty or len(df_15m) < 3:
        return

    # 取最后三根K线（索引 -1,-2,-3）
    latest     = df_15m.iloc[-1]   # 当前K（希望是第二根阳线）
    prev       = df_15m.iloc[-2]   # 第一根阳线
    prev_prev  = df_15m.iloc[-3]   # 通常是阴线（用于实体比较）

    close = latest["close"]
    ts = latest["ts"].strftime("%m-%d %H:%M")
    title = f"15m BTC-USDT - {ts}"

    # 布林带方向（辅助显示）
    boll_direction = "震荡"
    if close > latest["mid"]:
        boll_direction = "多头方向"
    elif close < latest["mid"]:
        boll_direction = "空头方向"

    signals = []

    # ──────────────────────────────────────────────
    # 信号1：第一阳线上穿下轨 + 第二根也是阳线 + 实体放大（两种方式任一满足）
    # ──────────────────────────────────────────────
    # 条件A：第一根阳线完成上穿下轨（前一根收盘 ≤ 下轨，本根收盘 > 下轨）
    cross_up_from_lower = (
        prev_prev["close"] <= prev_prev["lower"] and
        prev["close"] > prev["lower"]
    )

    signal1_entity_condition = False

    # 方式1：第一根阳线实体 > 前一根阴线实体
    if prev_prev["is_bull"] == False:  # 确保前一根是阴线（更严格）
        if prev["entity_size"] > prev_prev["entity_size"]:
            signal1_entity_condition = True
    # 方式2：两根阳线实体之和 > 前一根阴线实体（即使第一根没明显放大，合力也可以）
    else:
        # 如果前一根不是阴线，也允许用两根阳线合力判断（比较宽松，可选）
        two_bull_entity_sum = prev["entity_size"] + latest["entity_size"]
        if two_bull_entity_sum > prev_prev["entity_size"] * 1.0:   # 可调倍数 1.0~1.3
            signal1_entity_condition = True

    if (
        cross_up_from_lower and
        prev["is_bull"] and
        latest["is_bull"] and
        signal1_entity_condition
    ):
        signals.append(
            "🚀 信号1：第一阳线上穿下轨 + 连阳 + 实体放大（单根或两根合力）"
        )

    # ──────────────────────────────────────────────
    # 信号2：两根连续阳线从下轨区域强势拉到中轨上方
    # ──────────────────────────────────────────────
    # 核心要求：第一根低点触及/跌破下轨区，最后一根收盘突破中轨
    touched_lower_zone = prev["low"] <= prev["lower"] * 1.005   # 允许轻微超出1%容忍

    break_mid = latest["close"] > latest["mid"]

    if (
        prev["is_bull"] and
        latest["is_bull"] and
        touched_lower_zone and
        break_mid
    ):
        distance_pct = (latest["close"] - prev["low"]) / prev["low"] * 100
        signals.append(
            f"🚀 信号2：连阳从下轨拉升至中轨上方（涨幅约 {distance_pct:.1f}%）"
        )

    # ──────────────────────────────────────────────
    # 发送与打印
    # ──────────────────────────────────────────────
    if signals:
        msg = f"【15分钟多头信号】{title} \n\n"
        msg += f"当前方向：{boll_direction}\n"
        msg += f"现价：${close:,.0f}　中轨：${latest['mid']:,.0f}\n"
        msg += "──────────────\n"

        for sig in signals:
            msg += f"• {sig}\n"

        send_message(msg)
        print(f"【{datetime.now().strftime('%H:%M')}】发送！找到 {len(signals)} 个多头信号")
    else:
        print(f"【{datetime.now().strftime('%H:%M')}】暂无符合的多头信号")

    # 状态行（方便观察）
    print(f"{ts} | BTC ${close:,.0f} | 方向: {boll_direction} | 信号数: {len(signals)}")


def main():
    df_15m = get_candles("BTC-USDT", "15m", 300)
    if df_15m.empty:
        print("无法获取15分钟K线")
        return

    df_15m = add_technical_indicators(df_15m)
    trend_alert(df_15m)


if __name__ == '__main__':
    print("BTC 15分钟布林线多头趋势监控启动（无去重）...")
    main()
