#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OKX 5m 回测（永续 T+0 单链 · 固定投注 · 方向锁定）
—— 信号K线收盘后下一根开盘开仓，持有一个 5m 周期平仓

信号规则（去掉布林）：
- 中间价 mid = (open + close) / 2
- 连续两根阴线（close < open），且 mid1 < mid2 -> 看空（SHORT）
- 连续两根阳线（close > open），且 mid1 > mid2 -> 看多（LONG）
- 其余情况不产生新信号

方向锁定：
- 一旦出现看多/看空信号，就锁定该方向
- 之后每根 K 线都按锁定方向投注，直到出现反向的两根连线才翻转
- 即：锁定 LONG 时，只有出现“连续两根阴线且 mid1<mid2”才翻成 SHORT；
      锁定 SHORT 时，只有出现“连续两根阳线且 mid1>mid2”才翻成 LONG

投注：
- 每笔固定 base，不翻倍
- 张数 = floor(base / 每张名义价值)，至少 1 张
- 结算：T+1 open 开仓，T+2 close 平仓
  LONG: 平仓价 > 开仓价 -> WIN；SHORT: 平仓价 < 开仓价 -> WIN；相等 PUSH
- 手续费：开平各收一次，按名义价值 × 费率

简化模型：1 张 = 1 USDT 名义价值；实盘 OKX 永续需按 ctVal 换算。
"""

import argparse
import math
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

LOCAL_TZ = ZoneInfo("Asia/Phnom_Penh")

try:
    import ccxt
    import pandas as pd
except ImportError:
    print("缺少依赖，请先执行：pip install ccxt pandas", file=sys.stderr)
    raise


def parse_args():
    p = argparse.ArgumentParser(description="OKX 5m 回测（永续 T+0 单链 · 固定投注 · 方向锁定）")
    p.add_argument("--date",
                   default=(datetime.now(LOCAL_TZ) - timedelta(days=1)).strftime("%Y-%m-%d"),
                   help="回测日期，柬埔寨时间，格式 YYYY-MM-DD；默认昨天")
    p.add_argument("--symbol", default="BTC/USDT",
                   help="永续交易对，ccxt 格式，默认 BTC/USDT")
    p.add_argument("--base", type=float, default=10.0, help="每笔固定投注金额（USDT），默认 10")
    p.add_argument("--outdir", default="backtest_output", help="输出目录")
    p.add_argument("--fee", type=float, default=0.0005,
                   help="单边手续费率（taker），默认 0.0005 = 0.05%%，开平各收一次")
    p.add_argument("--contract-value", type=float, default=1.0,
                   help="每张合约名义价值（USDT），默认 1.0；实盘 BTC-USDT-SWAP 需按 ctVal 调整")
    return p.parse_args()


def validate_date(s):
    try:
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
    except ValueError:
        raise SystemExit("--date 必须使用 YYYY-MM-DD 格式")
    return dt


def fetch_day(exchange, symbol, start_ms, end_ms):
    timeframe_ms = 5 * 60 * 1000
    since = start_ms - 300 * timeframe_ms
    rows = []
    seen = set()

    while since < end_ms:
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe="5m", since=since, limit=300)
        except Exception as exc:
            print(f"获取行情失败：{exc}", file=sys.stderr)
            raise SystemExit(2)

        if not batch:
            break

        newest = batch[-1][0]
        for row in batch:
            ts = row[0]
            if ts not in seen and ts < end_ms:
                seen.add(ts)
                rows.append(row)

        next_since = newest + timeframe_ms
        if next_since <= since:
            break
        since = next_since

        if newest >= end_ms - timeframe_ms:
            break

    if not rows:
        raise SystemExit("没有获取到行情数据，请检查日期、交易对或网络。")

    rows.sort(key=lambda x: x[0])
    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])


def money(x):
    return round(float(x), 8)


def stake_to_contracts(stake_amount, contract_value):
    if stake_amount <= 0 or contract_value <= 0:
        return 0
    n = math.floor(stake_amount / contract_value)
    return max(n, 1)


def candle_mid(row):
    return (float(row["open"]) + float(row["close"])) / 2.0


def is_yin(row):
    """阴线：close < open"""
    return float(row["close"]) < float(row["open"])


def is_yang(row):
    """阳线：close > open"""
    return float(row["close"]) > float(row["open"])


def detect_reversal(df, i):
    """
    判断第 i 根 K 线是否与第 i-1 根构成“反向两根连线”。
    返回 'LONG' / 'SHORT' / None
    - 两根阴线且 mid[i-1] < mid[i] -> 看空 SHORT
    - 两根阳线且 mid[i-1] > mid[i] -> 看多 LONG
    注意：这里用 i-1 和 i 两根已收盘 K 线
    """
    if i < 1:
        return None
    prev = df.iloc[i - 1]
    cur = df.iloc[i]

    mid_prev = candle_mid(prev)
    mid_cur = candle_mid(cur)

    if is_yin(prev) and is_yin(cur) and mid_prev < mid_cur:
        return "SHORT"
    if is_yang(prev) and is_yang(cur) and mid_prev > mid_cur:
        return "LONG"
    return None


def run_backtest(df, args, day_start_ms, day_end_ms):
    rows = []
    base = args.base

    # 方向锁定状态
    locked_dir = None          # 'LONG' / 'SHORT' / None
    direction_flips = 0

    # 连续统计
    cur_loss_streak = 0
    cur_win_streak = 0
    max_loss_streak = 0
    max_win_streak = 0

    max_single_stake = 0.0
    max_single_contracts = 0
    total_staked = 0.0
    total_contracts = 0
    total_pnl = 0.0
    total_fee = 0.0
    wins = losses = pushes = 0
    bet_no = 0

    # 回测窗口：信号K线 i，开仓 i+1 open，平仓 i+2 close
    for i in range(1, len(df) - 2):
        signal_candle = df.iloc[i]
        entry_candle = df.iloc[i + 1]
        exit_candle = df.iloc[i + 2]

        signal_ts = int(signal_candle["timestamp"])
        if signal_ts < day_start_ms or signal_ts >= day_end_ms:
            continue

        # 1) 检查是否出现反向两根连线 -> 翻转方向
        reversal = detect_reversal(df, i)
        if reversal is not None and reversal != locked_dir:
            locked_dir = reversal
            direction_flips += 1

        # 2) 没有锁定方向，不投注
        if locked_dir is None:
            continue

        signal = locked_dir
        entry_price = float(entry_candle["open"])
        exit_price = float(exit_candle["close"])

        if entry_price <= 0:
            continue

        bet_no += 1

        stake_this_bet = base
        contracts = stake_to_contracts(stake_this_bet, args.contract_value)
        notional = contracts * args.contract_value
        total_staked += notional
        total_contracts += contracts
        max_single_stake = max(max_single_stake, notional)
        max_single_contracts = max(max_single_contracts, contracts)

        if signal == "LONG":
            price_delta = exit_price - entry_price
        else:
            price_delta = entry_price - exit_price

        gross_pnl = contracts * args.contract_value * (price_delta / entry_price)
        fee = notional * args.fee * 2
        total_fee += fee

        if price_delta == 0:
            result = "PUSH"
            net_pnl = -fee
            pushes += 1
        elif gross_pnl > 0:
            result = "WIN"
            net_pnl = gross_pnl - fee
            wins += 1
            cur_win_streak += 1
            cur_loss_streak = 0
            max_win_streak = max(max_win_streak, cur_win_streak)
        else:
            result = "LOSS"
            net_pnl = gross_pnl - fee
            losses += 1
            cur_loss_streak += 1
            cur_win_streak = 0
            max_loss_streak = max(max_loss_streak, cur_loss_streak)

        total_pnl += net_pnl
        rows.append({
            "bet_no": bet_no,
            "signal_time_local": datetime.fromtimestamp(signal_ts / 1000, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "entry_time_local": datetime.fromtimestamp(int(entry_candle["timestamp"]) / 1000, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "exit_time_local": datetime.fromtimestamp(int(exit_candle["timestamp"]) / 1000, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": args.symbol,
            "signal": signal,
            "signal_open": money(signal_candle["open"]),
            "signal_close": money(signal_candle["close"]),
            "signal_mid": money(candle_mid(signal_candle)),
            "entry_price": money(entry_price),
            "exit_price": money(exit_price),
            "stake_this_bet": money(stake_this_bet),
            "contracts": contracts,
            "notional": money(notional),
            "result": result,
            "gross_pnl": money(gross_pnl),
            "fee": money(fee),
            "net_pnl": money(net_pnl),
            "locked_dir": locked_dir,
            "loss_streak_raw": cur_loss_streak,
            "win_streak_raw": cur_win_streak,
            "cumulative_pnl": money(total_pnl),
        })

    summary = {
        "date_local": datetime.fromtimestamp(day_start_ms / 1000, LOCAL_TZ).strftime("%Y-%m-%d"),
        "symbol": args.symbol,
        "signal_rule": "two_same_color_candles_mid_compare",
        "direction_mode": "lock_until_reversal",
        "stake_mode": "fixed_base",
        "base_stake": args.base,
        "contract_value": args.contract_value,
        "fee_rate_per_side": args.fee,
        "settle_mode": "perpetual_t0_single_chain_fixed_stake",
        "settle_basis": "exit_price(T+2 close) vs entry_price(T+1 open)",
        "direction_flips": direction_flips,
        "bets": len(rows),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_rate_excluding_pushes_pct": round(wins / (wins + losses) * 100, 2) if wins + losses else 0.0,
        "total_contracts": total_contracts,
        "total_staked": money(total_staked),
        "total_fee": money(total_fee),
        "total_net_pnl": money(total_pnl),
        "avg_net_pnl_per_bet": money(total_pnl / len(rows)) if rows else 0.0,
        "max_single_stake": money(max_single_stake),
        "max_single_contracts": max_single_contracts,
        "max_loss_streak": max_loss_streak,
        "max_win_streak": max_win_streak,
    }
    return rows, summary


def main():
    args = parse_args()
    if args.base <= 0 or args.fee < 0:
        raise SystemExit("参数无效：base>0、fee>=0")
    if args.contract_value <= 0:
        raise SystemExit("--contract-value 必须 > 0")

    day = validate_date(args.date)
    day_start_ms = int(day.timestamp() * 1000)
    day_end_ms = int((day + timedelta(days=1)).timestamp() * 1000)

    exchange = ccxt.okx({"enableRateLimit": True})
    print(f"获取 {args.symbol} 5mK线，日期 {args.date}（柬埔寨时间）...")
    df = fetch_day(exchange, args.symbol, day_start_ms, day_end_ms)

    rows, summary = run_backtest(df, args, day_start_ms, day_end_ms)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    safe_symbol = args.symbol.replace("/", "_").replace(":", "_")
    stem = f"5m_mid_lock_{safe_symbol}_{args.date}"
    txt_path = outdir / f"{stem}.txt"

    summary_cn = {
        "回测日期": summary["date_local"],
        "交易对": summary["symbol"],
        "信号规则": "连续两根同向K线 + 中间价比较",
        "方向模式": "锁定方向，直到反向两根连线才翻转",
        "投注模式": "固定 base（不翻倍）",
        "每笔投注金额（USDT）": summary["base_stake"],
        "每张名义价值（USDT）": summary["contract_value"],
        "单边手续费率": summary["fee_rate_per_side"],
        "结算模式": summary["settle_mode"],
        "结算基准": summary["settle_basis"],
        "方向翻转次数": summary["direction_flips"],
        "总投注次数": summary["bets"],
        "盈利次数": summary["wins"],
        "亏损次数": summary["losses"],
        "平局次数": summary["pushes"],
        "胜率（不含平局）%": summary["win_rate_excluding_pushes_pct"],
        "总张数": summary["total_contracts"],
        "累计名义价值（USDT）": summary["total_staked"],
        "累计手续费（USDT）": summary["total_fee"],
        "$$$$$ 累计净盈亏 $$$$$（USDT）": summary["total_net_pnl"],
        "平均每笔净盈亏（USDT）": summary["avg_net_pnl_per_bet"],
        "最大单次名义价值（USDT）": summary["max_single_stake"],
        "最大单次张数": summary["max_single_contracts"],
        "最大连续亏损次数": summary["max_loss_streak"],
        "最大连续盈利次数": summary["max_win_streak"],
    }

    with txt_path.open("w", encoding="utf-8-sig") as f:
        f.write("OKX 5m 回测汇总（永续 T+0 单链 · 固定投注 · 方向锁定）\n")
        f.write("=" * 58 + "\n")
        for key, value in summary_cn.items():
            f.write(f"{key}：{value}\n")
        f.write("\n说明：信号规则为连续两根同向K线 + 中间价比较。\n")
        f.write("中间价 = (open + close) / 2。\n")
        f.write("两根阴线且 mid1<mid2 -> SHORT；两根阳线且 mid1>mid2 -> LONG。\n")
        f.write("方向一旦锁定，持续按该方向投注，直到出现反向两根连线才翻转。\n")
        f.write("每笔固定 base，不翻倍。T+1 open 开仓，T+2 close 平仓。\n")
        f.write("PnL = 张数 × 每张名义价值 × 价格变动比例，开平各收一次手续费。\n")

    print("\n回测完成")
    for k, v in summary.items():
        print(f"{k}: {v}")
    print(f"汇总文件：{txt_path.resolve()}")


if __name__ == "__main__":
    main()