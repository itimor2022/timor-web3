# -*- coding: utf-8 -*-
"""
加密货币 1小时 布林信号策略
支持命令行参数传入交易对
用法: python ss.py BTC
      python ss.py ETH
      python ss.py SOL
"""

import requests
import pandas as pd
import sys
from datetime import timedelta

# 设置显示参数
pd.set_option('display.max_columns', 1000)
pd.set_option('display.max_rows', 1000)
pd.set_option('display.width', 1000)
pd.set_option('display.max_colwidth', 1000)

SIGNAL_COOLDOWN = timedelta(minutes=240)
last_signal_time = {}

CHAT_ID = "-4850300375"
TOKEN = "8444348700:AAGqkeUUuB_0rI_4qIaJxrTylpRGh020wU0"
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

# ==================== 交易对映射 ====================
SYMBOL_MAP = {
    "BTC": "BTC-USDT-SWAP",
    "ETH": "ETH-USDT-SWAP",
    "SOL": "SOL-USDT-SWAP",
    "DOGE": "DOGE-USDT-SWAP",
    "XRP": "XRP-USDT-SWAP",
    "ADA": "ADA-USDT-SWAP",
    "DOT": "DOT-USDT-SWAP",
    "LINK": "LINK-USDT-SWAP",
    "MATIC": "MATIC-USDT-SWAP",
    "AVAX": "AVAX-USDT-SWAP",
    "XAG": "XAG-USDT-SWAP",
    "XAU": "XAU-USDT-SWAP",
}


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
def get_candles(inst_id="BTC-USDT-SWAP", bar="1H"):
    """
    获取K线数据
    
    参数:
        inst_id: 交易对，如 "BTC-USDT-SWAP", "ETH-USDT-SWAP"
        bar: K线周期，如 "1H", "4H", "1D"
    """
    url = "https://www.okx.com/api/v5/market/candles"

    r = requests.get(url, params={
        "instId": inst_id,
        "bar": bar,
        "limit": 1000
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
    # bbq
    df["bbq"] = (df["mid_price"] - df["mid"]).abs()
    df["bbq_min"] = df["bbq"].rolling(7).min()
    # ema
    df["ema5"] = df["close"].ewm(span=5, adjust=False).mean()
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


def detect_signals(sub):
    if len(sub) < 25:
        return []

    signals = []

    k1 = sub.iloc[-1]
    k2 = sub.iloc[-2]
    k3 = sub.iloc[-3]
    last_3 = sub.iloc[-3:]
    last_5 = sub.iloc[-5:]

    body = abs(k1["close"] - k1["open"])
    pct = abs(k1["close"] - k1["open"]) / k1["open"]

    upper_shadow = k1["high"] - max(k1["open"], k1["close"])
    lower_shadow = min(k1["open"], k1["close"]) - k1["low"]

    now_ts = k1["ts"]

    # ===============================
    # 工具函数：实体突破判断
    # ===============================
    def body_break_upper(k):
        return k["open"] > k["upper"] or k["close"] > k["upper"]

    def body_break_lower(k):
        return k["open"] < k["lower"] or k["close"] < k["lower"]


    # ===============================
    # 信号6 看多 阳线强势上穿中轨
    # ===============================
    if k1["is_bull"]:

        # 涨幅 > 0.4%
        cond_big_up = k1["change_pct"] >= 0.25

        # 实体上穿中轨（开盘在下，收盘在上）
        cond_cross_mid = (
                (k1["open"] < k1["mid"]) &
                (k1["close"] > k1["mid"])
        )

        if cond_big_up and cond_cross_mid:

            name = "信号6 看多 阳线强势上穿中轨"

            if allow_signal(name, now_ts):
                signals.append(name)


    # ===============================
    # 信号8 看空 2连阴 + Boll开口向下
    # ===============================
    if len(sub) >= 25:
        # 条件1：2连阴
        cond_two_bear = (
                k2["is_bear"] and
                k1["is_bear"]
        )

        # 条件2：
        cond_boll_down = (
                k1["mid_price"] < k2["mid_price"]
        )

        # 条件2.1
        prev_cross = (
                k2["open"] > k2["mid"] and
                k2["close"] < k2["mid"]
        )

        now_cross = (
                k1["open"] > k1["mid"] and
                k1["close"] < k1["mid"]
        )

        # 条件3：收盘价在中轨下面
        cond_close_boll_mid = (
                (k1["close"] < k1["mid"]) &
                (k2["close"] < k2["mid"])
        )

        if cond_two_bear and cond_close_boll_mid and (cond_boll_down or prev_cross or now_cross):

            name = "信号8 看空 2连阴 + Boll向下"

            if allow_signal(name, k1["ts"]):
                signals.append(name)

    # ===============================
    # 信号9 看多 2连阳 + Boll开口向上
    # ===============================

    if len(sub) >= 25:
        # 条件1：2连阳
        cond_two_bull = (
                k2["is_bull"] and
                k1["is_bull"]
        )

        # 条件2：
        cond_boll_down = (
                k1["mid_price"] > k2["mid_price"]
        )

        # 条件2.1
        prev_cross = (
                k2["open"] < k2["mid"] and
                k2["close"] > k2["mid"]
        )

        now_cross = (
                k1["open"] < k1["mid"] and
                k1["close"] > k1["mid"]
        )

        # 条件3：收盘价在中轨上面
        cond_close_boll_mid = (
                (k1["close"] > k1["mid"]) &
                (k2["close"] > k2["mid"])
        )

        if cond_two_bull and cond_close_boll_mid and (cond_boll_down or prev_cross or now_cross):

            name = "信号9 看多 2连阳 + Boll向上"

            if allow_signal(name, k1["ts"]):
                signals.append(name)

    # ===============================
    # 信号10 看空 大力下杀中线
    # ===============================
    if k1["is_bear"]:
        # 条件：当前阴线 开盘在中轨上，收盘在中轨下
        cond_cross_mid = (
            (k1["open"] > k1["mid"]) and
            (k1["close"] < k1["mid"])
        )

        if cond_cross_mid:

            ratio1 = k1["high"] / k1["low"] if k1["low"] > 0 else 0
            ratio2 = k2["high"] / k1["low"] if k1["low"] > 0 else 0
            ratio3 = k3["high"] / k1["low"] if k1["low"] > 0 else 0

            cond1 = (
                k1["high"] > k1["upper"] and
                ratio1 > 1.011
            )

            cond2 = (
                k2["high"] > k2["upper"] and
                ratio2 > 1.011
            )

            cond3 = (
                k3["high"] > k3["upper"] and
                ratio3 > 1.011
            )

            if cond1 or cond2 or cond3:

                detail = []
                if cond1:
                    detail.append("当前K突破上轨")
                if cond2:
                    detail.append("前1K突破上轨")
                if cond3:
                    detail.append("前2K突破上轨")

                name = f"信号10 看空 大力下杀中线 ({'+'.join(detail)})"

                if allow_signal(name, now_ts):
                    signals.append(name)

    return signals


# ==================== 历史扫描 ====================
def scan_history(df, symbol_name="BTC"):
    """
    历史扫描
    
    参数:
        df: K线数据
        symbol_name: 交易对名称，用于日志
    """
    print(f"开始历史扫描 {symbol_name}...")
    total = 0
    log_file = f"{symbol_name}_1h_signal.txt"
    open(log_file, "w").close()

    for i in range(30, len(df)):
        sub = df.iloc[:i + 1]
        sigs = detect_signals(sub)

        if sigs:
            k = sub.iloc[-1]
            ts1 = (k["ts"] - timedelta(hours=1)).strftime("%m-%d %H:%M")
            ts2 = k["ts"].strftime("%m-%d %H:%M")

            text = f"{ts1} ~ {ts2} | {symbol_name} {k['close']:,.2f} | {k['vol']:,.2f} | {k['change_pct']:,.2f}% \n"
            for s in sigs:
                text += f" - {s}\n"
            text += "-" * 30 + "\n"

            with open(log_file, "a", encoding="utf-8") as f:
                f.write(text)

            total += 1

    print(f"{symbol_name} 历史信号数量: {total}")


# ==================== 实时检测 ====================
def check_latest(df, symbol_name="BTC"):
    """
    实时检测
    
    参数:
        df: K线数据
        symbol_name: 交易对名称，用于消息
    """
    sub = df.iloc[:-1]  # 用于检测
    sigs = detect_signals(sub)

    if not sigs:
        print(f"{symbol_name} 最新K线无信号")
        return

    k = df.iloc[-1]  # 取最后一根K线（单行）
    ts1 = (k["ts"] - timedelta(hours=1)).strftime("%m-%d %H:%M")
    ts2 = k["ts"].strftime("%m-%d %H:%M")

    msg = f"🚨 {symbol_name} 1H 新信号触发\n"
    msg += f"⏰ 时间: {ts1} ~ {ts2}\n"
    msg += f"💰 价格: {k['close']:,.2f}\n"
    msg += f"📊 成交量: {k['vol']:,.2f}\n"
    msg += f"📉 涨幅: {k['change_pct']:,.2f}%\n\n"

    for s in sigs:
        msg += f"🔴 {s} \n"

    send_message(msg)

    log_file = f"{symbol_name}_1h_signal.txt"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(msg + "\n")

    print(f"已发送 {symbol_name} 实时信号")


# ==================== 主程序 ====================
def main():
    """
    主程序 - 支持命令行参数
    用法: python ss.py BTC
          python ss.py ETH
          python ss.py SOL
    """
    # 获取命令行参数
    if len(sys.argv) < 2:
        print("用法: python ss.py <交易对>")
        print("支持: BTC, ETH, SOL, DOGE, XRP, ADA, DOT, LINK, MATIC, AVAX")
        print("示例: python ss.py BTC")
        sys.exit(1)
    
    symbol = sys.argv[1].upper()
    
    # 检查是否支持该交易对
    if symbol not in SYMBOL_MAP:
        print(f"不支持的交易对: {symbol}")
        print(f"支持的交易对: {', '.join(SYMBOL_MAP.keys())}")
        sys.exit(1)
    
    inst_id = SYMBOL_MAP[symbol]
    bar = "1H"
    
    print(f"{symbol} {bar} 新策略启动")
    print(f"交易对: {inst_id}")

    try:
        df = get_candles(inst_id, bar)
        df = add_indicators(df)
        check_latest(df, symbol)
        scan_history(df, symbol)
    except Exception as e:
        print(f"运行出错: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()