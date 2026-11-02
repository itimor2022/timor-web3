# -*- coding: utf-8 -*-
"""
加密货币 1小时 布林信号策略 + 成交量信号 + 涨跌幅信号

用法:
    python ss.py BTC                          # 模拟模式：扫描今天所有信号，输出文件
    python ss.py BTC --date 2026-10-01        # 模拟模式：扫描指定日期所有信号，输出文件
    python ss.py BTC --live                   # 实时模式：查询最新K线并推送 Telegram
"""

import requests
import pandas as pd
import sys
import argparse
from datetime import timedelta, datetime

# 设置显示参数
pd.set_option('display.max_columns', 1000)
pd.set_option('display.max_rows', 1000)
pd.set_option('display.width', 1000)
pd.set_option('display.max_colwidth', 1000)

SIGNAL_COOLDOWN = timedelta(minutes=240)
last_signal_time = {}

# 成交量信号冷却
VOLUME_COOLDOWN = timedelta(minutes=60)
last_volume_signal_time = {}

# 涨跌幅信号冷却
AMPLITUDE_COOLDOWN = timedelta(minutes=60)
last_amplitude_signal_time = {}

CHAT_ID = "-5264477303"
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

    df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms") + pd.Timedelta(hours=8)

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
    # 振幅 = 最高 - 最低
    df["amplitude"] = df["high"] - df["low"]
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


def allow_volume_signal(name, ts):
    last_ts = last_volume_signal_time.get(name)

    if last_ts is None:
        last_volume_signal_time[name] = ts
        return True

    if ts - last_ts >= VOLUME_COOLDOWN:
        last_volume_signal_time[name] = ts
        return True

    return False


def allow_amplitude_signal(name, ts):
    last_ts = last_amplitude_signal_time.get(name)

    if last_ts is None:
        last_amplitude_signal_time[name] = ts
        return True

    if ts - last_ts >= AMPLITUDE_COOLDOWN:
        last_amplitude_signal_time[name] = ts
        return True

    return False


# ==================== 成交量信号 ====================
def detect_volume_signal(df):
    """
    检测成交量信号
    
    当前K线成交量 >= 前面第2、3、4根K线成交量之和
    k0 >= (k2 + k3 + k4)
    
    其中 k0 为当前K线（最新），k1 为前1根，k2 为前2根...
    """
    if len(df) < 6:
        return None

    # df 最后一行是最新K线(当前K线)
    k0 = df.iloc[-1]  # 当前K线
    k1 = df.iloc[-2]  # 前1根
    k2 = df.iloc[-3]  # 前2根
    k3 = df.iloc[-4]  # 前3根
    k4 = df.iloc[-5]  # 前4根

    # k0 >= (k2 + k3 + k4)
    sum_vol = k2["vol"] + k3["vol"] + k4["vol"]

    if sum_vol > 0 and k0["vol"] >= sum_vol:
        ratio = k0["vol"] / sum_vol if sum_vol > 0 else 0
        return {
            "k0_vol": k0["vol"],
            "sum_vol": sum_vol,
            "ratio": ratio,
            "k0": k0,
            "ts": k0["ts"],
        }

    return None


# ==================== 涨跌幅信号 ====================
def detect_amplitude_signal(df):
    """
    检测涨跌幅信号

    条件：
      1. 当前K0振幅（high - low）>= 前K1~K5振幅平均值 * 2
      2. 当前K0涨跌幅绝对值 >= 0.6%

    返回 dict 或 None
    """
    if len(df) < 6:
        return None

    k0 = df.iloc[-1]
    prev = df.iloc[-6:-1]  # 前5根K1~K5

    avg_amplitude = prev["amplitude"].mean()

    if avg_amplitude <= 0:
        return None

    k0_amplitude = k0["amplitude"]
    ratio = k0_amplitude / avg_amplitude
    abs_change = abs(k0["change_pct"])

    if ratio >= 2.0 and abs_change >= 0.6:
        return {
            "k0": k0,
            "ts": k0["ts"],
            "k0_amplitude": k0_amplitude,
            "avg_amplitude": avg_amplitude,
            "ratio": ratio,
            "change_pct": k0["change_pct"],
        }

    return None


# ==================== 格式化单根K线信号 ====================
def format_boll_signal(k, sigs):
    ts1 = (k["ts"] - timedelta(hours=1)).strftime("%m-%d %H:%M")
    ts2 = k["ts"].strftime("%m-%d %H:%M")

    text = f"{ts1} ~ {ts2} | {k['close']:,.2f} | {k['vol']:,.2f} | {k['change_pct']:,.2f}% \n"
    for s in sigs:
        text += f" - {s}\n"
    return text


def format_volume_signal(k, result):
    ts1 = (k["ts"] - timedelta(hours=1)).strftime("%m-%d %H:%M")
    ts2 = k["ts"].strftime("%m-%d %H:%M")

    text = f"{ts1} ~ {ts2} | {k['close']:,.2f} | {k['vol']:,.2f} | {k['change_pct']:,.2f}% \n"
    text += f" - 成交量信号: ({result['ratio']:.2f}倍)\n"
    return text


def format_amplitude_signal(k, result):
    ts1 = (k["ts"] - timedelta(hours=1)).strftime("%m-%d %H:%M")
    ts2 = k["ts"].strftime("%m-%d %H:%M")

    text = f"{ts1} ~ {ts2} | {k['close']:,.2f} | {k['vol']:,.2f} | {k['change_pct']:,.2f}% \n"
    text += (
        f" - 涨跌幅信号: 振幅 {result['k0_amplitude']:,.2f} / 均值 "
        f"{result['avg_amplitude']:,.2f} = {result['ratio']:.2f}倍, "
        f"涨跌幅 {result['change_pct']:.2f}%\n"
    )
    return text


# ==================== 模拟模式：扫描指定日期 ====================
def scan_date(df, symbol_name, target_date, debug=False):
    """
    模拟模式：扫描指定日期所有满足条件的信号，输出到文件

    参数:
        df: K线数据
        symbol_name: 交易对名称
        target_date: datetime.date 对象
    """
    print(f"开始模拟扫描 {symbol_name} 日期 {target_date}...")

    if debug:
        debug_volume(df, target_date, symbol_name)

    target_start = pd.Timestamp(target_date) + pd.Timedelta(hours=0)
    target_end = target_start + pd.Timedelta(days=1)

    log_file = f"{symbol_name}_{target_date.strftime('%Y%m%d')}_signal.txt"
    open(log_file, "w").close()

    total = 0
    out_lines = []

    # ---- 成交量信号扫描 ----
    for i in range(4, len(df)):
        k0 = df.iloc[i]
        if not (target_start <= k0["ts"] < target_end):
            continue

        sub = df.iloc[:i + 1]
        result = detect_volume_signal(sub)

        if result:
            text = format_volume_signal(k0, result)
            out_lines.append(text + "-" * 30 + "\n")
            total += 1

    # ---- 涨跌幅信号扫描 ----
    for i in range(5, len(df)):
        k0 = df.iloc[i]
        if not (target_start <= k0["ts"] < target_end):
            continue

        sub = df.iloc[:i + 1]
        result = detect_amplitude_signal(sub)

        if result:
            text = format_amplitude_signal(k0, result)
            out_lines.append(text + "-" * 30 + "\n")
            total += 1

    # 输出到文件
    if out_lines:
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(f"=== {symbol_name} {target_date} 信号汇总 ===\n\n")
            for line in out_lines:
                f.write(line)

    print(f"{symbol_name} {target_date} 信号数量: {total}")
    print(f"结果已写入: {log_file}")

    # 同时打印到控制台
    if out_lines:
        print(f"\n=== {symbol_name} {target_date} 信号明细 ===")
        for line in out_lines:
            print(line)


def check_volume_latest(df, symbol_name="BTC"):
    """实时成交量信号检测"""
    result = detect_volume_signal(df)

    if not result:
        print(f"{symbol_name} 最新K线无成交量信号")
        return

    k0 = result["k0"]
    now_ts = result["ts"]

    name = "成交量信号 k0 >= (k2+k3+k4)"

    if not allow_volume_signal(name, now_ts):
        print(f"{symbol_name} 成交量信号冷却中")
        return

    ts1 = (k0["ts"] - timedelta(hours=1)).strftime("%m-%d %H:%M")
    ts2 = k0["ts"].strftime("%m-%d %H:%M")

    msg = f"📊 {symbol_name} 1H 成交量信号\n"
    msg += f"⏰ 时间: {ts1} ~ {ts2}\n"
    msg += f"💰 价格: {k0['close']:,.2f}\n"
    msg += f"📉 涨幅: {k0['change_pct']:,.2f}%\n\n"
    msg += f"🔥 当前成交量: {result['k0_vol']:,.2f}\n"
    msg += f"⚡ 倍数: {result['ratio']:.2f}x\n\n"

    send_message(msg)

    log_file = f"{symbol_name}_1h_volume_signal.txt"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(msg + "\n")

    print(f"已发送 {symbol_name} 实时成交量信号")


def check_amplitude_latest(df, symbol_name="BTC"):
    """实时涨跌幅信号检测"""
    result = detect_amplitude_signal(df)

    if not result:
        print(f"{symbol_name} 最新K线无涨跌幅信号")
        return

    k0 = result["k0"]
    now_ts = result["ts"]

    name = "涨跌幅信号 振幅>=2倍均值 且 |涨跌幅|>=0.6%"

    if not allow_amplitude_signal(name, now_ts):
        print(f"{symbol_name} 涨跌幅信号冷却中")
        return

    ts1 = (k0["ts"] - timedelta(hours=1)).strftime("%m-%d %H:%M")
    ts2 = k0["ts"].strftime("%m-%d %H:%M")

    msg = f"⚡ {symbol_name} 1H 涨跌幅信号\n"
    msg += f"⏰ 时间: {ts1} ~ {ts2}\n"
    msg += f"💰 价格: {k0['close']:,.2f}\n"
    msg += f"📉 涨跌幅: {result['change_pct']:,.2f}%\n\n"
    msg += f"🔥 当前振幅: {result['k0_amplitude']:,.2f}\n"
    msg += f"⚡ 倍数: {result['ratio']:.2f}x\n\n"
    msg += f"🔴 振幅 >= 2倍均值 且 涨跌幅绝对值 >= 0.6%\n"

    send_message(msg)

    log_file = f"{symbol_name}_1h_amplitude_signal.txt"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(msg + "\n")

    print(f"已发送 {symbol_name} 实时涨跌幅信号")


def debug_volume(df, target_date, symbol_name="BTC"):
    """打印指定日期所有K线成交量对比 + 19~21点明细"""
    target_start = pd.Timestamp(target_date)
    target_end = target_start + pd.Timedelta(days=1)

    print(f"\n=== {symbol_name} 数据诊断 ===")
    print(f"总K线数: {len(df)}")
    print(f"数据范围: {df['ts'].min()} ~ {df['ts'].max()}")
    print(f"目标日期: {target_date}")

    # 检查目标日期是否在数据范围内
    if df["ts"].max() < target_start or df["ts"].min() >= target_end:
        print(f"⚠️ 目标日期 {target_date} 不在数据范围内！")
        print(f"   数据最早: {df['ts'].min()}")
        print(f"   数据最晚: {df['ts'].max()}")
        return

    # ---- 1. 打印目标日期全部K线 ----
    print(f"\n=== {symbol_name} {target_date} 全部K线成交量对比 ===")
    print(f"{'时间':<18}{'k0_vol':>14}{'k2+k3+k4':>14}{'比值':>10}{'满足?':>8}")
    print("-" * 70)

    for i in range(5, len(df)):
        k0 = df.iloc[i]
        if not (target_start <= k0["ts"] < target_end):
            continue

        k2 = df.iloc[i - 2]
        k3 = df.iloc[i - 3]
        k4 = df.iloc[i - 4]

        sum_vol = k2["vol"] + k3["vol"] + k4["vol"]
        ratio = k0["vol"] / sum_vol if sum_vol > 0 else 0
        ok = "✅" if (sum_vol > 0 and k0["vol"] >= sum_vol) else "❌"

        ts = k0["ts"].strftime("%m-%d %H:%M")
        print(f"{ts:<18}{k0['vol']:>14,.2f}{sum_vol:>14,.2f}{ratio:>10.2f}{ok:>8}")

    # ---- 2. 单独打印 19:00 / 20:00 / 21:00 三根明细 ----
    print(f"\n=== {symbol_name} {target_date} 19~21点 明细 ===")
    for hour in [19, 20, 21]:
        # 找目标日期对应小时那根K线
        mask = (df["ts"] >= target_start + pd.Timedelta(hours=hour)) & \
               (df["ts"] < target_start + pd.Timedelta(hours=hour + 1))
        hits = df[mask]

        if hits.empty:
            print(f"\n[{hour:02d}:00] ⚠️ 数据缺失，找不到该小时K线")
            continue

        pos = hits.index[0]  # df 是 reset_index 后的，index 即位置
        if pos < 4:
            print(f"\n[{hour:02d}:00] ⚠️ 历史数据不足4根，无法计算 k2+k3+k4")
            continue

        k0 = df.iloc[pos]
        k1 = df.iloc[pos - 1]
        k2 = df.iloc[pos - 2]
        k3 = df.iloc[pos - 3]
        k4 = df.iloc[pos - 4]

        sum_vol = k2["vol"] + k3["vol"] + k4["vol"]
        ratio = k0["vol"] / sum_vol if sum_vol > 0 else 0
        ok = "✅ 满足" if (sum_vol > 0 and k0["vol"] >= sum_vol) else "❌ 不满足"

        print(f"\n[{hour:02d}:00] {k0['ts']} | 收 {k0['close']:,.2f} | 涨跌 {k0['change_pct']:+.2f}% | {ok}")
        print(f"  k0 当前   : vol={k0['vol']:>14,.2f}")
        print(f"  k1 前1根  : vol={k1['vol']:>14,.2f}  ({k1['ts']})")
        print(f"  k2 前2根  : vol={k2['vol']:>14,.2f}  ({k2['ts']})")
        print(f"  k3 前3根  : vol={k3['vol']:>14,.2f}  ({k3['ts']})")
        print(f"  k4 前4根  : vol={k4['vol']:>14,.2f}  ({k4['ts']})")
        print(f"  条件 k0 >= k2+k3+k4 : {k0['vol']:,.2f} >= {sum_vol:,.2f}  →  比值 {ratio:.2f}x  {ok}")

# ==================== 主程序 ====================
def parse_args():
    parser = argparse.ArgumentParser(
        description="加密货币 1H 布林 + 成交量 + 涨跌幅信号策略",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("symbol", help="交易对，如 BTC / ETH / SOL")
    parser.add_argument(
        "--live", action="store_true",
        help="实时模式：查询最新K线并推送 Telegram\n（不加此参数则为模拟模式）"
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="模拟模式指定日期，格式 YYYY-MM-DD\n（默认扫描今天）\n示例: --date 2026-10-01"
    )
    parser.add_argument("--debug", action="store_true", help="打印成交量对比调试信息")
    return parser.parse_args()


def main():
    args = parse_args()

    symbol = args.symbol.upper()

    if symbol not in SYMBOL_MAP:
        print(f"不支持的交易对: {symbol}")
        print(f"支持的交易对: {', '.join(SYMBOL_MAP.keys())}")
        sys.exit(1)

    inst_id = SYMBOL_MAP[symbol]
    bar = "1H"

    print(f"{symbol} {bar} 策略启动")
    print(f"交易对: {inst_id}")

    # 解析日期
    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"--date 格式错误: {args.date}，应为 YYYY-MM-DD")
            sys.exit(1)
    else:
        target_date = datetime.now().date()

    mode = "实时模式" if args.live else "模拟模式"
    print(f"运行模式: {mode}")
    if not args.live:
        print(f"目标日期: {target_date}")

    try:
        df = get_candles(inst_id, bar)
        df = add_indicators(df)

        if args.live:
            # 实时模式
            check_volume_latest(df, symbol)
            # check_amplitude_latest(df, symbol)
        else:
            # 模拟模式
            scan_date(df, symbol, target_date, debug=args.debug)

    except Exception as e:
        print(f"运行出错: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()