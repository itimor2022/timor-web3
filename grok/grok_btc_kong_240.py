# -*- coding: utf-8 -*-
"""
BTC 4小时布林线顶部反转做空策略脚本（2026版 - 只做空，无去重）
核心：以4小时布林线（25,2）作为主要空头趋势判断依据
- 只检测指定的顶部反转空头信号
- 信号1: 本次阴线，上次阴线，前一次阳线，且中间阳线的量是上次阴线2倍，同时注意趋势，如果上升趋势需谨慎盈利和时间
- 信号2: 3连阴 继续一根向下的K（即4连阴）
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


def get_candles(instId="BTC-USDT", bar="4H", limit=300):
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

    # 交易量放2倍（相对于前一根）
    df["vol_spike_2x"] = df["vol"] > 2 * df["vol"].shift(1)
    return df


def trend_alert(df_4h):
    if df_4h.empty or len(df_4h) < 4:
        return
    # 取最近几根K线（索引 -1=最新, -2=前一根，-3=前前一根，-4=前前前一根）
    latest = df_4h.iloc[-1]  # 当前K（阴线 for 信号1/2）
    prev = df_4h.iloc[-2]  # 前一根
    prev2 = df_4h.iloc[-3]  # 前前一根
    prev3 = df_4h.iloc[-4]  # 前前前一根（for 信号2）
    close = latest["close"]
    ts = latest["ts"].strftime("%m-%d %H:%M")
    title = f"4H BTC-USDT - {ts}"
    boll_direction = "震荡"
    if close < latest["mid"]:
        boll_direction = "空头方向"
    elif close > latest["mid"]:
        boll_direction = "多头方向"
    signals = []

    # ──────────────────────────────────────────────
    # 信号1：当前阴线 + 前一根阴线 + 前面连续一段阳线（至少1根）
    #        且这段阳线中至少有一根 vol >= 前一根阴线 vol 的 2倍
    # ──────────────────────────────────────────────
    current_is_bear = latest["is_bear"]
    prev_is_bear = prev["is_bear"]

    if current_is_bear and prev_is_bear:
        # 从 prev2 开始往前找连续阳线
        bull_streak_start = -3  # 从 -3 开始（prev2）
        consecutive_bulls = 0
        max_vol_in_bulls = 0
        ref_vol = prev["vol"]  # “上次阴线”的量（即前一根）

        i = 3
        while bull_streak_start >= -len(df_4h):
            idx = -i
            if idx < -len(df_4h):
                break
            candle = df_4h.iloc[idx]
            if not candle["is_bear"]:  # 是阳线
                consecutive_bulls += 1
                max_vol_in_bulls = max(max_vol_in_bulls, candle["vol"])
                i += 1
            else:
                # 遇到阴线 → 阳线段结束
                break

        has_sufficient_vol = max_vol_in_bulls >= 2 * ref_vol

        if consecutive_bulls >= 1 and has_sufficient_vol:
            drop_pct = (latest["open"] - latest["close"]) / latest["open"] * 100
            sig_msg = (
                f"⚠️ 信号1：顶部反转 - "
                f"前段连续 {consecutive_bulls} 阳线（其中最大量 {max_vol_in_bulls:.0f} ≥ 前阴2倍）"
                f" + 前阴 + 当前阴（跌幅约 {drop_pct:.1f}%）→ 空头反转信号"
            )
            if boll_direction == "多头方向":
                sig_msg += "（上升趋势中，需谨慎盈利和时间）"
            if consecutive_bulls >= 3:
                sig_msg += f"（较长阳线段 {consecutive_bulls} 根，放量特征更强）"
            signals.append(sig_msg)

    # ──────────────────────────────────────────────
    # 信号2：3连阴 + 继续一根向下的K（第4根可以是阴或阳）
    # ──────────────────────────────────────────────
    if len(df_4h) >= 4:
        k1 = df_4h.iloc[-4]  # 最前面那根（最早）
        k2 = df_4h.iloc[-3]
        k3 = df_4h.iloc[-2]
        k4 = df_4h.iloc[-1]  # 最新一根（“继续的一根”）

        three_consecutive_bears = (
                k1["is_bear"] and
                k2["is_bear"] and
                k3["is_bear"]
        )

        if three_consecutive_bears:
            # 第4根无论阴阳都触发（但可加轻微过滤）
            drop_pct_k4 = (k4["open"] - k4["close"]) / k4["open"] * 100 if k4["open"] > 0 else 0

            sig_msg = f"⚠️ 信号2：3连阴后继续第4根K（"
            if k4["is_bear"]:
                sig_msg += f"阴线，跌幅约 {drop_pct_k4:.1f}%"
            else:
                sig_msg += f"阳线，但未能有效反转"
            sig_msg += f"）→ 空头压力延续 / 顶部滞涨确认"

            # 可选：如果第4根阳线但涨幅很小，可视为更强的滞涨信号
            if not k4["is_bear"] and abs(drop_pct_k4) < 0.3:  # 阳线但涨幅 < 0.3%
                sig_msg += "（小阳滞涨，更倾向空头）"

            signals.append(sig_msg)

    # ──────────────────────────────────────────────
    # 添加标记
    # ──────────────────────────────────────────────
    mark_list = []
    if signals:  # 只在有信号时计算标记
        # 计算连续阴线数（从当前往回）
        consecutive_bears = 0
        for i in range(1, len(df_4h) + 1):
            if df_4h.iloc[-i]["is_bear"]:
                consecutive_bears += 1
            else:
                break
        if consecutive_bears >= 5:
            mark_list.append("1")
        elif consecutive_bears >= 3:
            mark_list.append("2")

        # 标记3: 最近10个K线2倍量
        if len(df_4h) >= 10:
            recent_10 = df_4h.iloc[-10:]
            vol_spike_count = recent_10["vol_spike_2x"].sum()
            if vol_spike_count > 0:
                mark_list.append("3")
                # 找到前面最近一次不放2倍量的vol
                last_non_spike_vol = 0
                for j in range(1, len(df_4h) + 1):
                    if not df_4h.iloc[-j]["vol_spike_2x"]:
                        last_non_spike_vol = df_4h.iloc[-j]["vol"]
                        break
                # 添加到signals的描述
                signals.append(
                    f"标记3：最近10个K线有 {vol_spike_count} 个2倍量，上次不放量vol: {last_non_spike_vol:.0f}"
                )
    标记 = ",".join([f"标记{m}" for m in mark_list]) if mark_list else "无"
    # ──────────────────────────────────────────────
    # 整合发送（所有信号放一条消息）
    # ──────────────────────────────────────────────
    if signals:
        msg = f"【4小时空头信号-{标记}】{title} \n\n"
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
    df_4h = get_candles("BTC-USDT", "4H", 300)
    if df_4h.empty:
        print("无法获取4小时K线")
        return
    df_4h = add_technical_indicators(df_4h)
    trend_alert(df_4h)


if __name__ == '__main__':
    print("BTC 4小时布林线顶部反转做空策略监控启动（无去重）...")
    main()
