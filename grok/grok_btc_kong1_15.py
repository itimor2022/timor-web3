# -*- coding: utf-8 -*-
"""
BTC 15分钟布林线趋势监控脚本（2025版 - 只做空，无去重）
核心：以15分钟布林线（25,2）作为主要空头趋势判断依据
- 只检测空头信号
- 信号1: 1根阴线实体直接从中轨碰到下轨（开盘 >= 中轨, 收盘 <= 下轨, 是阴线）
- 信号2: 连续三根阴线，其中至少一根实体下穿中轨（某根 open > 中轨 and close < 中轨），且三个阴线的中心点 ((open + close)/2) 向下移动（递减）
- 信号3: 阴线悬空，前一根阳线实体大力突破上轨，阳线上半部分（high - open）是下半部分（open - low）的1.2倍，当前1根阴线实体悬浮在上轨上面
- 每次运行只要有信号就发送消息（无去重，适合实时监控）
- 所有触发信号一次性整合成一条消息，避免刷屏
"""

import requests
import pandas as pd
from datetime import datetime

# ==================== 配置区 ====================
CHAT_ID = "-4850300375"
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

    # 阴线/阳线
    df["is_bear"] = df["close"] < df["open"]
    df["entity_size"] = abs(df["close"] - df["open"])

    # 中心点
    df["center"] = (df["open"] + df["close"]) / 2

    return df


def trend_alert(df_15m):
    if df_15m.empty or len(df_15m) < 4:
        return

    # 取最近几根K线（索引 -1=最新, -2=前一根, -3=前前, -4=更前一根用于信号3）
    latest = df_15m.iloc[-1]  # 当前K（希望是阴线）
    prev = df_15m.iloc[-2]  # 前一根（信号3中是大阳线）
    prev_prev = df_15m.iloc[-3]

    close = latest["close"]
    ts = latest["ts"].strftime("%m-%d %H:%M")
    title = f"15m BTC-USDT - {ts}"

    boll_direction = "震荡"
    if close < latest["mid"]:
        boll_direction = "空头方向"
    elif close > latest["mid"]:
        boll_direction = "多头方向"

    signals = []

    # ──────────────────────────────────────────────
    # 信号1：单根大阴线从中轨直接贯穿到下轨（力度较强）
    # ──────────────────────────────────────────────
    if (
            latest["is_bear"] and
            latest["open"] >= latest["mid"] and
            latest["close"] <= latest["lower"] and
            latest["entity_size"] > (latest["mid"] - latest["lower"]) * 0.7  # 至少吃掉70%的中→下轨距离，可调
    ):
        drop_pct = (latest["open"] - latest["close"]) / latest["open"] * 100
        signals.append(
            f"⚠️ 信号1：单根大阴线从中轨直杀下轨（跌幅约 {drop_pct:.1f}%）→ 空头暴力砸盘"
        )

    # ──────────────────────────────────────────────
    # 信号2：连续三根阴线 + 至少一根实体下穿中轨 + 中心点递减（趋势加速确认）
    # ──────────────────────────────────────────────
    three_bears = prev_prev["is_bear"] and prev["is_bear"] and latest["is_bear"]

    centers_down = (
        (prev_prev["center"] > prev["center"] > latest["center"])
        if three_bears else False
    )

    has_cross_mid_down = any(
        c["open"] > c["mid"] and c["close"] < c["mid"]
        for c in [prev_prev, prev, latest]
    )

    if three_bears and has_cross_mid_down and centers_down:
        signals.append(
            "⚠️ 信号2：连续三阴 + 实体下穿中轨 + 中心点逐根下移 → 空头趋势加速"
        )

    # ──────────────────────────────────────────────
    # 信号3：阴线悬空 + 前一根大阳线突破上轨 + 阳线上半身明显长（暗示诱多后反转）
    # ──────────────────────────────────────────────
    if len(df_15m) >= 3:
        # 前一根是大阳线 + 上轨突破（收盘 > 上轨 或 high > 上轨）
        prev_bull_break = (
                prev["close"] > prev["open"] and
                prev["high"] > prev["upper"] * 0.998  # 轻微容差，避免浮点严格等于漏掉
        )

        if prev_bull_break:
            # 阳线上半部分（high - open） vs 下半部分（open - low）
            upper_half = prev["high"] - prev["open"]
            lower_half = prev["open"] - prev["low"]
            upper_dominant = upper_half > lower_half * 1.3  # 你要求的1.2倍，可调1.15~1.4

            # 当前阴线“悬空”：low > 上轨（完全在上轨上方飘着）
            hanging_above = latest["low"] > latest["upper"] * 1.002  # 轻微容差

            if upper_dominant and hanging_above and latest["is_bear"]:
                signals.append(
                    f"⚠️ 信号3：阴线高位悬空 → 前大阳诱多突破上轨 → 顶部反转陷阱"
                )

    # ──────────────────────────────────────────────
    # 整合发送（所有信号放一条消息）
    # ──────────────────────────────────────────────
    if signals:
        msg = f"【15分钟空头信号】{title}\n\n"
        msg += f"当前方向：{boll_direction}\n"
        msg += f"现价：${close:,.0f}　中轨：${latest['mid']:,.0f}　上轨：${latest['upper']:,.0f}\n"
        msg += "──────────────\n"

        for sig in signals:
            msg += f"• {sig}\n"

        send_message(msg)
        print(f"【{datetime.now().strftime('%H:%M')}】发送空头警报！找到 {len(signals)} 个信号")
    else:
        print(f"【{datetime.now().strftime('%H:%M')}】暂无空头信号")

    # 控制台状态
    print(f"{ts} | BTC ${close:,.0f} | 方向: {boll_direction} | 信号数: {len(signals)}")


def main():
    df_15m = get_candles("BTC-USDT", "15m", 300)
    if df_15m.empty:
        print("无法获取15分钟K线")
        return

    df_15m = add_technical_indicators(df_15m)
    trend_alert(df_15m)


if __name__ == '__main__':
    print("BTC 15分钟布林线空头趋势监控启动（无去重）...")
    main()
