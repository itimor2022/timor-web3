# -*- coding: utf-8 -*-
"""
BTC 15分钟布林线多头趋势监控脚本（2025优化版 - 只做多）
核心逻辑：以15分钟布林带(25,2)为主的多头信号识别

信号定义（更清晰版）：
1. 阴线 → 2连阳，且至少1根阳线实体有效穿越中轨（或下轨）
2. 连续2阳，从下轨区域（或贴近下轨）强势拉升站上中轨
3. 连续2阳，至少1根实体突破上轨，且该K线上影线/实体比例较小（上攻意愿强）

特点：
- 无去重 → 适合实时推送或配合外部去重
- 增加信号强度/位置描述
- 改进Telegram消息排版
- 加入简单重试机制
"""

import requests
import pandas as pd
import time
from datetime import datetime, timedelta

# 设置显示参数
pd.set_option('display.max_columns', 1000)
pd.set_option('display.max_rows', 1000)
pd.set_option('display.width', 1000)
pd.set_option('display.max_colwidth', 1000)

# ==================== 配置 ====================
TELEGRAM_TOKEN = "8444348700:AAGqkeUUuB_0rI_4qIaJxrTylpRGh020wU0"
CHAT_ID = "-5068436114"
OKX_BASE = "https://www.okx.com"
SYMBOL = "BTC-USDT"
BAR = "15m"
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
            print(f"发送异常 (第{attempt + 1}次): {e}")
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

            df = pd.DataFrame(data,
                              columns=["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"])
            df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms")
            df["ts"] = df["ts"] + timedelta(hours=7)  # 亚洲时区（可依需求调整）
            df = df.astype({"open": float, "high": float, "low": float, "close": float, "vol": float})
            df = df[["ts", "open", "high", "low", "close", "vol"]].sort_values("ts").reset_index(drop=True)
            return df
        except Exception as e:
            print(f"获取K线失败 (第{attempt + 1}次): {e}")
            if attempt < retries - 1:
                time.sleep(2.5)
    return pd.DataFrame()


def enrich_indicators(df):
    if len(df) < 50:
        return df

    # 布林带 25周期，2倍标准差（主流设置）
    df["mid"] = df["close"].rolling(25).mean()
    df["std"] = df["close"].rolling(25).std()
    df["upper"] = df["mid"] + 2 * df["std"]
    df["lower"] = df["mid"] - 2 * df["std"]

    # K线性质
    df["body"] = df["close"] - df["open"]
    df["is_bull"] = df["body"] > 0
    df["is_bear"] = df["body"] < 0
    df["entity"] = abs(df["body"])
    df["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]

    return df


def detect_bull_signals(df):
    if len(df) < 4:
        return [], None

    latest = df.iloc[-1]  # 当前最新K（可能是正在形成的）
    prev = df.iloc[-2]  # 前一根
    prev2 = df.iloc[-3]  # 前前一根

    signals = []
    price_info = {
        "close": latest["close"],
        "mid": latest["mid"],
        "upper": latest["upper"],
        "lower": latest["lower"],
        "ts": latest["ts"].strftime("%m-%d %H:%M"),
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

    # ─── 信号1 ─── 阴线后出现2连阳 + 至少1根有效穿越中轨/下轨 ───
    if prev2["is_bear"] and prev["is_bull"] and latest["is_bull"]:
        cross_mid = (
                (prev["open"] <= prev["mid"] < prev["close"]) or
                (latest["open"] <= latest["mid"] < latest["close"])
        )
        cross_lower = (
                (prev["low"] <= prev["lower"] < prev["close"]) or
                (latest["low"] <= latest["lower"] < latest["close"])
        )

        if cross_mid:
            who = "最新K" if latest["open"] <= latest["mid"] < latest["close"] else "前一根"
            signals.append(f"信号1：阴线后2连阳 + <b>{who}实体上穿中轨</b>")

        if cross_lower and not cross_mid:  # 避免重复报
            who = "最新K" if latest["low"] <= latest["lower"] < latest["close"] else "前一根"
            signals.append(f"信号1：阴线后2连阳 + <b>{who}实体上穿下轨</b>")

    # ─── 信号2 ─── 两根阳线从下轨区域拉升站上中轨 ───
    near_lower = prev["low"] <= prev["lower"] * 1.003  # 允许轻微超出
    stand_mid = latest["close"] > latest["mid"] + latest["std"] * 0.1  # 站得稍微扎实一点

    if prev["is_bull"] and latest["is_bull"] and near_lower and stand_mid:
        rise_pct = (latest["close"] - prev["low"]) / prev["low"] * 100
        signals.append(f"信号2：下轨区域起涨 → 连阳站上中轨 <b>(涨幅约{rise_pct:.1f}%)</b>")

    # ─── 信号3 ─── 两阳至少一阳突破上轨 + 上攻形态（上影线不宜过长） ───
    bull1_break = prev["is_bull"] and prev["open"] <= prev["upper"] < prev["close"]
    bull2_break = latest["is_bull"] and latest["open"] <= latest["upper"] < latest["close"]

    if prev["is_bull"] and latest["is_bull"] and (bull1_break or bull2_break):
        # 上半部分（突破后继续冲高） vs 下半部分（开盘到上轨）
        for k in [prev, latest]:
            if k["close"] > k["upper"]:
                upper_part = k["close"] - k["upper"]
                lower_part = k["upper"] - k["open"]
                if upper_part >= 2.0 * lower_part and k["upper_wick"] < 0.6 * k["entity"]:
                    signals.append("信号3：连阳突破上轨 + <b>强势续涨形态</b>（主升概率较高）")
                    break

    return signals, price_info, pos_desc


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] BTC 15m 多头信号监控启动...")

    df = fetch_klines()
    if df.empty:
        print("无法获取K线数据，退出本次运行")
        return

    df = enrich_indicators(df)
    signals, info, pos = detect_bull_signals(df)

    if not signals:
        print(f"[{info['ts']}] 暂无符合的多头信号 | {pos}")
        return

    # 构建消息
    msg = f"<b>【BTC 15m 多头信号】{info['ts']}</b>\n\n"
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
