# -*- coding: utf-8 -*-
"""
BTC 15分钟布林线趋势监控脚本（2025版 - 只做多，无去重）
核心：以15分钟布林线（25,2）作为主要多头趋势判断依据
- 只检测多头信号
- 信号1：一根阴线之后出现2连阳，其中至少一根阳线实体上穿中轨
- 信号2：2根连续阳线从下轨区域强势拉到中轨上方
- 信号3：2根阳线实体突破上轨 + 其中一根上半部分 ≥ 下半部分2倍
- 信号4：一根阴线之后出现2连阳，其中至少一根阳线实体上穿下轨
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

    df["return"] = df["close"].pct_change() * 100

    # BOLL 25,2
    df["sma25"] = df["close"].rolling(25).mean()
    df["std25"] = df["close"].rolling(25).std()
    df["upper"] = df["sma25"] + 2 * df["std25"]
    df["lower"] = df["sma25"] - 2 * df["std25"]
    df["mid"] = df["sma25"]

    # 阳/阴线
    df["is_bull"] = df["close"] > df["open"]
    df["is_bear"] = df["close"] < df["open"]
    df["entity_size"] = abs(df["close"] - df["open"])

    return df


def trend_alert(df_15m):
    if df_15m.empty or len(df_15m) < 4:
        return

    latest     = df_15m.iloc[-1]   # 第二根阳线
    prev       = df_15m.iloc[-2]   # 第一根阳线
    prev_prev  = df_15m.iloc[-3]   # 阴线

    close = latest["close"]
    ts = latest["ts"].strftime("%m-%d %H:%M")
    title = f"15m BTC-USDT - {ts}"

    boll_direction = "震荡"
    if close > latest["mid"]:
        boll_direction = "多头方向"
    elif close < latest["mid"]:
        boll_direction = "空头方向"

    signals = []

    # ──────────────────────────────────────────────
    # 信号1：阴线后2连阳 + 至少一根上穿中轨
    # ──────────────────────────────────────────────
    is_prev_bear = prev_prev["is_bear"]
    two_bulls = prev["is_bull"] and latest["is_bull"]
    cross_mid_prev   = (prev["open"] <= prev["mid"] < prev["close"])
    cross_mid_latest = (latest["open"] <= latest["mid"] < latest["close"])
    has_mid_cross = cross_mid_prev or cross_mid_latest

    if is_prev_bear and two_bulls and has_mid_cross:
        strength = "（最新阳线中轨突破力度较强）" if cross_mid_latest else ""
        signals.append(f"🚀 信号1：阴线后2连阳 + 至少一根上穿中轨 {strength}")

    # ──────────────────────────────────────────────
    # 信号2：连阳从下轨拉到中轨上方（原版）
    # ──────────────────────────────────────────────
    touched_lower_zone = prev["low"] <= prev["lower"] * 1.005
    break_mid = latest["close"] > latest["mid"]

    if prev["is_bull"] and latest["is_bull"] and touched_lower_zone and break_mid:
        distance_pct = (latest["close"] - prev["low"]) / prev["low"] * 100
        signals.append(f"🚀 信号2：连阳从下轨拉升至中轨上方（涨幅约 {distance_pct:.1f}%）")

    # ──────────────────────────────────────────────
    # 信号3：2根阳线突破上轨 + 上半身≥下半身2倍（原版）
    # ──────────────────────────────────────────────
    bull1_break = prev["is_bull"] and prev["open"] <= prev["upper"] < prev["close"]
    bull2_break = latest["is_bull"] and latest["open"] <= latest["upper"] < latest["close"]
    if prev["is_bull"] and latest["is_bull"] and (bull1_break or bull2_break):
        cond_prev = (prev["close"] - prev["upper"]) >= 2 * (prev["upper"] - prev["open"] + 1e-8)
        cond_latest = (latest["close"] - latest["upper"]) >= 2 * (latest["upper"] - latest["open"] + 1e-8)
        if cond_prev or cond_latest:
            signals.append("🚀 信号3：2根阳线实体突破上轨 + 其中一根上半部分≥下半部分2倍 → 主升浪")

    # ──────────────────────────────────────────────
    # 信号4：阴线后2连阳 + 至少一根阳线上穿下轨
    # ──────────────────────────────────────────────
    cross_lower_prev   = (prev["low"] <= prev["lower"] < prev["close"])
    cross_lower_latest = (latest["low"] <= latest["lower"] < latest["close"])
    has_lower_cross = cross_lower_prev or cross_lower_latest

    if is_prev_bear and two_bulls and has_lower_cross:
        strength = "（最新阳线下轨突破较强）" if cross_lower_latest else ""
        signals.append(f"🚀 信号4：阴线后2连阳 + 至少一根阳线实体上穿下轨 {strength}")

    # ──────────────────────────────────────────────
    # 发送消息
    # ──────────────────────────────────────────────
    if signals:
        msg = f"<b>【15分钟多头信号】{title}</b>\n\n"
        msg += f"当前方向：{boll_direction}\n"
        msg += f"现价：${close:,.0f}　中轨：${latest['mid']:,.0f}　下轨：${latest['lower']:,.0f}\n"
        msg += "──────────────\n"

        for sig in signals:
            msg += f"• {sig}\n"

        send_message(msg)
        print(f"【{datetime.now().strftime('%H:%M')}】发送！找到 {len(signals)} 个多头信号")
    else:
        print(f"【{datetime.now().strftime('%H:%M')}】暂无符合的多头信号")

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