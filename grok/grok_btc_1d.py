# -*- coding: utf-8 -*-

import requests
import pandas as pd
import os
from datetime import timedelta

# 设置显示参数
pd.set_option('display.max_columns', 1000)
pd.set_option('display.max_rows', 1000)
pd.set_option('display.width', 1000)
pd.set_option('display.max_colwidth', 1000)

# ==================== 配置 ====================
TOKEN = "你的TOKEN"
CHAT_ID = "-5264477303"
SYMBOL = "BTC-USDT"
BAR = "1D"
LIMIT = 400
LOG_FILE = "btc_ema_signals_log.txt"

TG_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


# ==================== Telegram ====================
def send_tg(msg):
    requests.get(TG_URL, params={
        "chat_id": CHAT_ID,
        "text": msg
    })


# ==================== K线 ====================
def fetch():
    url = "https://www.okx.com/api/v5/market/candles"
    r = requests.get(url, params={"instId": SYMBOL, "bar": BAR, "limit": LIMIT})
    data = r.json()["data"]

    df = pd.DataFrame(data)
    df = df.iloc[:, :6]
    df.columns = ["ts", "open", "high", "low", "close", "vol"]

    df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms") + timedelta(hours=7)
    df = df.astype({"open":float,"high":float,"low":float,"close":float,"vol":float})
    return df.sort_values("ts").reset_index(drop=True)


# ==================== EMA ====================
def add_ema(df):
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    return df


# ==================== 信号扫描 ====================
def detect(df):
    out = []

    for i in range(2, len(df)):
        p = df.iloc[i-1]
        c = df.iloc[i]

        d = c["ts"].strftime("%Y-%m-%d")

        if p["ema20"] > p["ema50"] and c["ema20"] < c["ema50"]:
            out.append((d,"死叉",c["close"]))

        if p["ema20"] < p["ema50"] and c["ema20"] > c["ema50"]:
            out.append((d,"金叉",c["close"]))

        if c["ema20"]>c["ema50"]>c["ema200"] and not (p["ema20"]>p["ema50"]>p["ema200"]):
            out.append((d,"多头排列",c["close"]))

        if c["ema20"]<c["ema50"]<c["ema200"] and not (p["ema20"]<p["ema50"]<p["ema200"]):
            out.append((d,"空头排列",c["close"]))

    return out


# ==================== TXT ====================
def load_existing():
    keys=set()
    if not os.path.exists(LOG_FILE):
        return keys

    for line in open(LOG_FILE,encoding="utf-8"):
        if "|" in line:
            sp=line.split("|")
            keys.add((sp[0].strip(),sp[1].strip()))
    return keys


def append_line(d,t,p):
    line=f"{d} | {t} | ${p:,.0f}\n"
    with open(LOG_FILE,"a",encoding="utf-8") as f:
        f.write(line)


# ==================== 主 ====================
def main():
    print("===== 历史信号扫描开始 =====")

    df = fetch()
    df = add_ema(df)

    sigs = detect(df)

    if not sigs:
        print("无信号")
        return

    existing = load_existing()

    new_count = 0

    for d,t,p in sigs:
        print(f"{d} | {t} | ${p}")

        if (d,t) not in existing:
            append_line(d,t,p)
            new_count += 1

    print("\n总历史信号数:", len(sigs))
    print("新增写入TXT:", new_count)

    # 只推送最新
    d,t,p = sigs[-1]
    send_tg(f"BTC EMA 最新信号\n{d} {t} ${p}")


if __name__ == "__main__":
    main()
