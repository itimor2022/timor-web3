# -*- coding: utf-8 -*-
"""
BTC 日线布林线反转策略脚本（2026版 - 双向信号，无去重）
核心：以日线布林带（25,2）作为主要反转趋势判断依据
- 只检测指定的跨线反转信号
- 信号1: 前一根阳线 + 后一根下穿上轨或中轨（空头反转信号）
- 信号2: 前一根阴线 + 后一根上穿下轨或中轨（多头反转信号）
- 每次运行只要有信号就发送消息（无去重，适合实时监控）
- 所有触发信号一次性整合成一条消息，避免刷屏
"""

import requests
import pandas as pd
import time
from datetime import datetime, timedelta

# ==================== 配置 ====================
TELEGRAM_TOKEN = "8444348700:AAGqkeUUuB_0rI_4qIaJxrTylpRGh020wU0"
CHAT_ID = "-4836241115"
OKX_BASE = "https://www.okx.com"
SYMBOL = "BTC-USDT"
BAR = "1D"  # 修改为日线
LIMIT = 300

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


def send_telegram(msg, retry=2):
    payload = {
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    for attempt in range(retry + 1):
        try:
            r = requests.get(TELEGRAM_API, params=payload, timeout=10)
            if r.json().get("ok"):
                return True
            print(f"Telegram发送失败: {r.text}")
        except Exception as e:
            print(f"发送异常 (第{attempt+1}次): {e}")
        if attempt < retry:
            time.sleep(1.5)
    return False


def fetch_klines(symbol=SYMBOL, bar=BAR, limit=LIMIT, retries=3):
    url = f"{OKX_BASE}/api/v5/market/candles"
    params = {"instId": symbol, "bar": bar, "limit": str(limit)}

    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=12)
            r.raise_for_status()
            data = r.json()["data"]
            if not data:
                return pd.DataFrame()

            df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"])
            df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms")
            df["ts"] = df["ts"] + timedelta(hours=7)  # 亚洲时区（可依需求调整）
            df = df.astype({"open": float, "high": float, "low": float, "close": float, "vol": float})
            df = df[["ts", "open", "high", "low", "close", "vol"]].sort_values("ts").reset_index(drop=True)
            return df
        except Exception as e:
            print(f"获取K线失败 (第{attempt+1}次): {e}")
            if attempt < retries - 1:
                time.sleep(2.5)
    return pd.DataFrame()


def enrich_indicators(df):
    if len(df) < 50:
        return df

    # 布林带 25周期，2倍标准差（主流设置）
    df["mid"]   = df["close"].rolling(25).mean()
    df["std"]   = df["close"].rolling(25).std()
    df["upper"] = df["mid"] + 2 * df["std"]
    df["lower"] = df["mid"] - 2 * df["std"]

    # K线性质
    df["body"]     = df["close"] - df["open"]
    df["is_bull"]  = df["body"] > 0
    df["is_bear"]  = df["body"] < 0
    df["entity"]   = abs(df["body"])
    df["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]

    # 交易量放2倍（相对于前一根）- 保留原逻辑，可用于额外过滤
    df["vol_spike_2x"] = df["vol"] > 2 * df["vol"].shift(1)

    return df


def detect_reversal_signals(df):
    if len(df) < 3:
        return [], None

    latest   = df.iloc[-1]   # 当前最新K（后一根）
    prev     = df.iloc[-2]   # 前一根

    signals = []
    price_info = {
        "close": latest["close"],
        "mid":   latest["mid"],
        "upper": latest["upper"],
        "lower": latest["lower"],
        "ts":    latest["ts"].strftime("%m-%d %H:%M"),
    }

    # 当前整体位置判断
    pos_desc = "中轨附近震荡"
    if latest["close"] > latest["upper"]:
        pos_desc = "<b>站上上轨</b>（强势）"
    elif latest["close"] > latest["mid"]:
        pos_desc = "站上中轨（多头区间）"
    elif latest["close"] < latest["lower"]:
        pos_desc = "<b>跌破下轨</b>（弱势）"
    else:
        pos_desc = "位于中下轨之间"

    # ─── 信号1 ─── 前一根阳线 + 后一根下穿上轨或中轨（空头反转） ───
    if prev["is_bull"]:
        cross_upper_down = (latest["open"] > latest["upper"] > latest["close"])
        cross_mid_down   = (latest["open"] > latest["mid"] > latest["close"])

        if cross_upper_down or cross_mid_down:
            cross_type = "上轨" if cross_upper_down else "中轨"
            drop_pct = (latest["open"] - latest["close"]) / latest["open"] * 100
            sig_msg = f"⚠️ 信号1：前阳 + 后K下穿<b>{cross_type}</b>（跌幅约 {drop_pct:.1f}%）→ 空头反转信号"
            # 可选过滤：如果伴随放量，更强
            if latest["vol_spike_2x"]:
                sig_msg += "（伴随2倍量，更强）"
            signals.append(sig_msg)

    # ─── 信号2 ─── 前一根阴线 + 后一根上穿下轨或中轨（多头反转） ───
    if prev["is_bear"]:
        cross_lower_up = (latest["open"] < latest["lower"] < latest["close"])
        cross_mid_up   = (latest["open"] < latest["mid"] < latest["close"])

        if cross_lower_up or cross_mid_up:
            cross_type = "下轨" if cross_lower_up else "中轨"
            rise_pct = (latest["close"] - latest["open"]) / latest["open"] * 100
            sig_msg = f"🚀 信号2：前阴 + 后K上穿<b>{cross_type}</b>（涨幅约 {rise_pct:.1f}%）→ 多头反转信号"
            # 可选过滤：如果伴随放量，更强
            if latest["vol_spike_2x"]:
                sig_msg += "（伴随2倍量，更强）"
            signals.append(sig_msg)


    return signals, price_info, pos_desc


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] BTC 日线 布林线反转信号监控启动...")

    df = fetch_klines()
    if df.empty:
        print("无法获取K线数据，退出本次运行")
        return

    df = enrich_indicators(df)
    signals, info, pos= detect_reversal_signals(df)

    if not signals:
        print(f"[{info['ts']}] 暂无符合的反转信号 | {pos}")
        return

    # 构建消息
    msg = f"<b>【BTC 日线 反转信号】{info['ts']}</b>\n\n"
    msg += f"现价　　<b>${info['close']:,.0f}</b>\n"
    msg += f"中轨　　${info['mid']:,.0f}\n"
    msg += f"上轨　　${info['upper']:,.0f}\n"
    msg += f"下轨　　${info['lower']:,.0f}\n"
    msg += f"位置　　{pos}\n"
    msg += "─────────────────\n"

    for sig in signals:
        msg += f"• {sig}\n"

    msg += f"\n<i>仅供参考，非交易建议</i>"

    if send_telegram(msg):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 已推送 {len(signals)} 个信号！")
    else:
        print("Telegram推送失败")


if __name__ == '__main__':
    main()
