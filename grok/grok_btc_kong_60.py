# -*- coding: utf-8 -*-
"""
BTC 1小时布林线顶部反转做空策略脚本（2026版 - 只做空，无去重）
核心：以1小时布林线（25,2）作为主要空头趋势判断依据
- 只检测指定的顶部反转空头信号
- 信号1: 前一根是阳线，现在是跳空阴线（当前阴线实体完全在上轨之上，即 min(open, close) > 上轨）
- 每次运行只要有信号就发送消息（无去重，适合实时监控）
- 所有触发信号一次性整合成一条消息，避免刷屏
"""
import requests
import pandas as pd
from datetime import datetime

# ==================== 配置区 ====================
CHAT_ID = "-5264477303"
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


def get_candles(instId="BTC-USDT", bar="1H", limit=300):
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
    return df


def trend_alert(df_1h):
    if df_1h.empty or len(df_1h) < 3:
        return
    # 取最近几根K线（索引 -1=最新, -2=前一根）
    latest = df_1h.iloc[-1]  # 当前K（希望是阴线）
    prev = df_1h.iloc[-2]  # 前一根（阳线）
    close = latest["close"]
    ts = latest["ts"].strftime("%m-%d %H:%M")
    title = f"1H BTC-USDT - {ts}"
    boll_direction = "震荡"
    if close < latest["mid"]:
        boll_direction = "空头方向"
    elif close > latest["mid"]:
        boll_direction = "多头方向"
    signals = []
    # ──────────────────────────────────────────────
    # 信号1：前一根阳线 + 当前跳空阴线（实体完全在上轨之上，min(open,close) > upper）
    # ──────────────────────────────────────────────
    prev_is_bull = prev["close"] > prev["open"]
    current_is_bear = latest["is_bear"]
    entity_above_upper = min(latest["open"], latest["close"]) * 1.001 > latest["upper"]
    # 可选：检查“跳空” - 在crypto市场连续，但可定义为当前open > 前close（向上跳空后阴线砸回）
    is_gap_up = latest["open"] > prev["close"]  # 轻微跳空确认
    if prev_is_bull and current_is_bear and entity_above_upper and is_gap_up:
        drop_pct = (latest["open"] - latest["close"]) / latest["open"] * 100
        signals.append(
            f"⚠️ 信号1：顶部反转 - 前阳后跳空阴线（实体悬空上轨上方，跌幅约 {drop_pct:.1f}%）→ 空头反转陷阱"
        )
    # ──────────────────────────────────────────────
    # 添加标记
    # ──────────────────────────────────────────────
    mark_list = []
    if signals:  # 只在有信号时计算标记
        # 计算连续阳线数（从前一根往回）
        consecutive_bulls = 0
        for i in range(2, len(df_1h) + 1):
            if df_1h.iloc[-i]["close"] > df_1h.iloc[-i]["open"]:
                consecutive_bulls += 1
            else:
                break
        if consecutive_bulls >= 5:
            mark_list.append("1")
        elif consecutive_bulls >= 3:
            mark_list.append("2")

        # 标记3: 最近10个K线2倍量
        if len(df_1h) >= 10:
            recent_10 = df_1h.iloc[-10:]
            vol_spike_count = recent_10["vol_spike_2x"].sum()
            if vol_spike_count > 0:
                mark_list.append("3")
                # 找到前面最近一次不放2倍量的vol
                last_non_spike_vol = 0
                for j in range(1, len(df_1h) + 1):
                    if not df_1h.iloc[-j]["vol_spike_2x"]:
                        last_non_spike_vol = df_1h.iloc[-j]["vol"]
                        break
                # 添加到signals的描述（比较：这里简单列出值，用户可比较）
                signals.append(
                    f"标记3：最近10个K线有 {vol_spike_count} 个2倍量，上次不放量vol: {last_non_spike_vol:.0f}"
                )
    标记 = ",".join([f"标记{m}" for m in mark_list]) if mark_list else "无"
    # ──────────────────────────────────────────────
    # 整合发送（所有信号放一条消息）
    # ──────────────────────────────────────────────
    if signals:
        msg = f"【1小时空头信号-{标记}】{title} \n\n"
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
    df_1h = get_candles("BTC-USDT", "1H", 300)
    if df_1h.empty:
        print("无法获取1小时K线")
        return
    df_1h = add_technical_indicators(df_1h)
    trend_alert(df_1h)


if __name__ == '__main__':
    print("BTC 1小时布林线顶部反转做空策略监控启动（无去重）...")
    main()