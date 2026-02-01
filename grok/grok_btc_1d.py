# -*- coding: utf-8 -*-
"""
BTC 日线 EMA 组合趋势策略脚本（带 TXT 历史日志版）
- 所有信号追加记录到 btc_ema_signals_log.txt
- 每次只推送新信号
- 适合长期监控 + 手动复盘
"""

import requests
import pandas as pd
import pandas_ta as ta
import time
import os
from datetime import datetime, timedelta

# ==================== 配置 ====================
TELEGRAM_TOKEN = "8444348700:AAGqkeUUuB_0rI_4qIaJxrTylpRGh020wU0"
CHAT_ID = "-5264477303"
OKX_BASE = "https://www.okx.com"
SYMBOL = "BTC-USDT"
BAR = "1D"
LIMIT = 400          # 多取数据以检测历史信号
LOG_FILE = "btc_ema_signals_log.txt"

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


def send_telegram(msg, retry=2):
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}
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
            df["ts"] = df["ts"] + timedelta(hours=7)  # 亚洲时区调整
            df = df.astype({"open": float, "high": float, "low": float, "close": float, "vol": float})
            df = df[["ts", "open", "high", "low", "close", "vol"]].sort_values("ts").reset_index(drop=True)
            return df
        except Exception as e:
            print(f"获取K线失败 (第{attempt+1}次): {e}")
            if attempt < retries - 1:
                time.sleep(2.5)
    return pd.DataFrame()


def enrich_indicators(df):
    if len(df) < 200:
        return df
    df["ema20"] = ta.ema(df["close"], length=20)
    df["ema50"] = ta.ema(df["close"], length=50)
    df["ema200"] = ta.ema(df["close"], length=200)
    df["vol_spike_2x"] = df["vol"] > 2 * df["vol"].shift(1)
    return df


def append_to_txt(signal):
    """追加一条信号到 TXT 文件"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = (
        f"[{timestamp}] "
        f"{signal['date']} | "
        f"{signal['signal_type']} | "
        f"价格 ${signal['close_price']:,.0f} | "
        f"EMA20 {signal['ema20']:,.0f} | EMA50 {signal['ema50']:,.0f} | EMA200 {signal['ema200']:,.0f} | "
        f"{signal['description']}\n"
    )
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)
    print(f"已记录: {line.strip()}")


def get_last_n_lines(n=10):
    """读取 TXT 最后 n 行（最近信号）"""
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return lines[-n:] if len(lines) >= n else lines
    except:
        return []


def detect_all_signals(df):
    if len(df) < 3:
        return []

    signals = []
    for i in range(2, len(df)):
        prev = df.iloc[i-1]
        curr = df.iloc[i]

        date_str = curr["ts"].strftime("%Y-%m-%d")
        price = curr["close"]
        e20 = round(curr["ema20"], 2) if pd.notna(curr["ema20"]) else 0
        e50 = round(curr["ema50"], 2) if pd.notna(curr["ema50"]) else 0
        e200 = round(curr["ema200"], 2) if pd.notna(curr["ema200"]) else 0

        vol_spike = "（放量）" if curr["vol_spike_2x"] else ""

        # 死叉
        if (prev["ema20"] > prev["ema50"] and curr["ema20"] < curr["ema50"]) or \
           (prev["ema20"] > prev["ema200"] and curr["ema20"] < curr["ema200"]):
            cross_type = "EMA50" if prev["ema20"] > prev["ema50"] else "EMA200"
            signals.append({
                "date": date_str,
                "signal_type": "死叉",
                "close_price": price,
                "ema20": e20,
                "ema50": e50,
                "ema200": e200,
                "description": f"EMA20 下穿 {cross_type}（死叉）{vol_spike}"
            })

        # 金叉
        if (prev["ema20"] < prev["ema50"] and curr["ema20"] > curr["ema50"]) or \
           (prev["ema20"] < prev["ema200"] and curr["ema20"] > curr["ema200"]):
            cross_type = "EMA50" if prev["ema20"] < prev["ema50"] else "EMA200"
            signals.append({
                "date": date_str,
                "signal_type": "金叉",
                "close_price": price,
                "ema20": e20,
                "ema50": e50,
                "ema200": e200,
                "description": f"EMA20 上穿 {cross_type}（金叉）{vol_spike}"
            })

        # 多头排列确认
        if curr["ema20"] > curr["ema50"] > curr["ema200"] and not (prev["ema20"] > prev["ema50"] > prev["ema200"]):
            signals.append({
                "date": date_str,
                "signal_type": "多头排列",
                "close_price": price,
                "ema20": e20,
                "ema50": e50,
                "ema200": e200,
                "description": "EMA20/50/200 多头排列确认"
            })

        # 空头排列确认
        if curr["ema20"] < curr["ema50"] < curr["ema200"] and not (prev["ema20"] < prev["ema50"] < prev["ema200"]):
            signals.append({
                "date": date_str,
                "signal_type": "空头排列",
                "close_price": price,
                "ema20": e20,
                "ema50": e50,
                "ema200": e200,
                "description": "EMA20/50/200 空头排列确认"
            })

    return signals


def main():
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{now_str}] BTC 日线 EMA 趋势监控（TXT日志版）启动...")

    df = fetch_klines()
    if df.empty:
        print("无法获取K线数据")
        return

    df = enrich_indicators(df)
    if len(df) < 200:
        print("数据不足，无法计算 EMA200")
        return

    all_signals = detect_all_signals(df)

    # 读取已有日志的日期+类型组合（防重）
    existing_keys = set()
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if "|" in line:
                    parts = line.split("|")
                    if len(parts) >= 2:
                        date_part = parts[0].strip().split()[-1] if len(parts[0].split()) > 1 else ""
                        sig_type = parts[1].strip() if len(parts) > 1 else ""
                        if date_part and sig_type:
                            existing_keys.add((date_part, sig_type))

    new_signals = []
    for sig in all_signals:
        key = (sig["date"], sig["signal_type"])
        if key not in existing_keys:
            new_signals.append(sig)
            append_to_txt(sig)           # 立即追加到 TXT
            existing_keys.add(key)       # 更新防重集合

    # 如果有新信号，推送最新一条
    if new_signals:
        latest = new_signals[-1]
        msg = f"<b>【BTC EMA 新信号】{latest['date']}</b>\n"
        msg += f"价格: ${latest['close_price']:,.0f}\n"
        msg += f"EMA: {latest['ema20']:,.0f} / {latest['ema50']:,.0f} / {latest['ema200']:,.0f}\n"
        msg += f"<b>{latest['description']}</b>\n"
        msg += f"\n<i>金叉 死叉 可以当做反向指标</i>"
        send_telegram(msg)
        print("已推送新信号到 Telegram")

    # 打印最近历史记录（控制台查看）
    recent_lines = get_last_n_lines(10)
    if recent_lines:
        print("\n最近 10 条信号记录（从旧到新）：")
        for line in recent_lines:
            print(line.strip())
    else:
        print("\n暂无历史信号记录")

    print(f"\n日志文件位置: {os.path.abspath(LOG_FILE)}")
    print(f"总信号数约: {len(existing_keys) + len(new_signals)} 条")


if __name__ == '__main__':
    main()