#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OKX 5m MACD + 事件倍投回测（对齐事件结算规则的修复版）
核心修复：
- 原版用"下一根K线 close > open"判断涨跌，与OKX事件合约结算规则不符
- OKX 5分钟涨跌事件结算价 = 到期前30秒内秒级指数K线收盘价的算术平均值
- 本版用"信号K线收盘价"与"结算窗口近似价"比较来模拟，但仍为近似
- 如需精确回测，需获取秒级 OKX 指数数据，普通 OHLCV 无法完全复现
"""

import argparse
import csv
import sys
import time
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
    p = argparse.ArgumentParser(description="OKX 5m MACD 事件倍投回测（结算规则修复版）")
    p.add_argument("--date",
                   default=(datetime.now(LOCAL_TZ) - timedelta(days=1)).strftime("%Y-%m-%d"),
                   help="回测日期，柬埔寨时间，格式 YYYY-MM-DD；默认昨天")
    p.add_argument("--symbol", default="BTC/USDT", help="交易对，默认 BTC/USDT")
    p.add_argument("--fast", type=int, default=6, help="MACD 快线周期，默认 6")
    p.add_argument("--slow", type=int, default=13, help="MACD 慢线周期，默认 13")
    p.add_argument("--signal", type=int, default=5, help="MACD 信号线周期，默认 5")
    p.add_argument("--base", type=float, default=10.0, help="基础投注金额，默认 10 USDT")
    p.add_argument("--max-losses", type=int, default=10,
                   help="连续亏损达到该次数后下一轮重置，默认 10")
    p.add_argument("--payout", type=float, default=1.0,
                   help="获胜净利润倍率，默认 1.0")
    p.add_argument("--outdir", default="backtest_output", help="输出目录")
    p.add_argument("--fee", type=float, default=0.0, help="每次投注按投注额比例扣除的费用")

    return p.parse_args()


def validate_date(s):
    try:
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
    except ValueError:
        raise SystemExit("--date 必须使用 YYYY-MM-DD 格式")
    return dt


def fetch_day(exchange, symbol, start_ms, end_ms):
    """分页获取5mK线，额外向前取数据用于MACD预热。"""
    timeframe_ms = 5 * 60 * 1000
    since = start_ms - 200 * timeframe_ms
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
        time.sleep(exchange.rateLimit / 1000.0)

    if not rows:
        raise SystemExit("没有获取到行情数据，请检查日期、交易对或网络。")

    rows.sort(key=lambda x: x[0])
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    return df


def money(x):
    return round(float(x), 8)


def run_backtest(df, args, day_start_ms, day_end_ms):
    df = df.copy()

    # ---- MACD ----
    ema_fast = df["close"].ewm(span=args.fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=args.slow, adjust=False).mean()
    df["macd_dif"] = ema_fast - ema_slow
    df["macd_dea"] = df["macd_dif"].ewm(span=args.signal, adjust=False).mean()
    df["macd_hist"] = df["macd_dif"] - df["macd_dea"]

    # ---- 事件结算近似 ----
    # OKX 5分钟涨跌事件：结算窗口为到期前30秒，结算价=30个秒级指数K线收盘价均值
    # 用5m OHLCV无法精确复现，这里用"信号K线收盘价"作为入场锚点，
    # 用"下一根K线收盘价"作为结算价的近似（因为结算窗口在下一根K线末尾）
    # 真正的结算价应该用指数数据，而非USDT现货K线，这是本回测的已知局限
    df["settle_ref"] = df["close"].shift(-1)  # 用下一根K线收盘价近似结算参考

    rows = []
    stake = args.base
    consecutive_losses = 0
    consecutive_wins = 0
    max_loss_streak = 0
    max_win_streak = 0
    max_single_stake = 0.0
    total_staked = 0.0
    total_pnl = 0.0
    total_fee = 0.0
    wins = losses = pushes = 0
    bet_no = 0
    skipped_no_signal = 0
    skipped_no_settle = 0

    for i in range(0, len(df) - 1):
        signal_candle = df.iloc[i]
        signal_ts = int(signal_candle["timestamp"])

        if signal_ts < day_start_ms or signal_ts >= day_end_ms:
            continue
        if pd.isna(signal_candle["macd_dif"]) or pd.isna(signal_candle["macd_dea"]) or pd.isna(signal_candle["macd_hist"]):
            continue
        if pd.isna(signal_candle["settle_ref"]):
            skipped_no_settle += 1
            continue

        macd_hist = float(signal_candle["macd_hist"])
        if macd_hist > 0:
            signal = "LONG"
        elif macd_hist < 0:
            signal = "SHORT"
        else:
            skipped_no_signal += 1
            continue

        # ---- 结算判断：信号K线收盘价 vs 近似结算参考价 ----
        # 涨跌方向事件：比较结算价与"事件开始时的价格"（通常用信号K线收盘或下一根开盘）
        # 这里用信号K线收盘价作为事件开始锚点，用近似结算价比较
        entry_price = float(signal_candle["close"])
        settle_price_approx = float(signal_candle["settle_ref"])

        if settle_price_approx > entry_price:
            outcome = "UP"
        elif settle_price_approx < entry_price:
            outcome = "DOWN"
        else:
            outcome = "PUSH"

        bet_no += 1
        stake_this = stake
        total_staked += stake_this
        max_single_stake = max(max_single_stake, stake_this)
        fee = stake_this * args.fee
        total_fee += fee

        if outcome == "PUSH":
            result = "PUSH"
            gross_pnl = 0.0
            net_pnl = -fee
            pushes += 1
            next_stake = stake_this
            next_losses = consecutive_losses
            loss_streak_raw = consecutive_losses
            win_streak_raw = consecutive_wins
        else:
            won = (signal == "LONG" and outcome == "UP") or (signal == "SHORT" and outcome == "DOWN")
            if won:
                result = "WIN"
                gross_pnl = stake_this * args.payout
                net_pnl = gross_pnl - fee
                wins += 1
                next_losses = 0
                next_stake = args.base
                consecutive_wins += 1
                loss_streak_raw = 0
                win_streak_raw = consecutive_wins
                max_win_streak = max(max_win_streak, win_streak_raw)
            else:
                result = "LOSS"
                gross_pnl = -stake_this
                net_pnl = gross_pnl - fee
                losses += 1
                next_losses = consecutive_losses + 1
                loss_streak_raw = next_losses
                win_streak_raw = 0
                consecutive_wins = 0
                max_loss_streak = max(max_loss_streak, loss_streak_raw)
                if next_losses >= args.max_losses:
                    next_losses = 0
                    next_stake = args.base
                else:
                    next_stake = stake_this * 2 + args.base

            consecutive_losses = next_losses
            stake = next_stake

        total_pnl += net_pnl
        rows.append({
            "bet_no": bet_no,
            "signal_time_local": datetime.fromtimestamp(signal_ts / 1000, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": args.symbol,
            "signal": signal,
            "entry_price": money(entry_price),
            "settle_price_approx": money(settle_price_approx),
            "macd_dif": money(signal_candle["macd_dif"]),
            "macd_dea": money(signal_candle["macd_dea"]),
            "macd_hist": money(signal_candle["macd_hist"]),
            "event_outcome": outcome,
            "result": result,
            "stake": money(stake_this),
            "gross_pnl": money(gross_pnl),
            "fee": money(fee),
            "net_pnl": money(net_pnl),
            "consecutive_losses_after": consecutive_losses,
            "loss_streak_raw": loss_streak_raw,
            "win_streak_raw": win_streak_raw,
            "next_stake": money(next_stake),
            "cumulative_pnl": money(total_pnl),
        })

    summary = {
        "date_local": datetime.fromtimestamp(day_start_ms / 1000, LOCAL_TZ).strftime("%Y-%m-%d"),
        "symbol": args.symbol,
        "macd_fast": args.fast,
        "macd_slow": args.slow,
        "macd_signal": args.signal,
        "base_stake": args.base,
        "max_consecutive_losses_param": args.max_losses,
        "payout_net_multiplier": args.payout,
        "fee_rate": args.fee,
        "bets": len(rows),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_rate_excluding_pushes_pct": round(wins / (wins + losses) * 100, 2) if wins + losses else 0.0,
        "skipped_no_signal": skipped_no_signal,
        "skipped_no_settle": skipped_no_settle,
        "total_staked": money(total_staked),
        "total_fee": money(total_fee),
        "total_net_pnl": money(total_pnl),
        "max_single_stake": money(max_single_stake),
        "actual_max_consecutive_losses": max_loss_streak,
        "actual_max_consecutive_wins": max_win_streak,
        "ending_next_stake": money(stake),
    }
    return rows, summary


def main():
    args = parse_args()
    if args.fast < 1 or args.slow < 1 or args.signal < 1 or args.fast >= args.slow \
            or args.base <= 0 or args.max_losses < 1 or args.payout < 0 or args.fee < 0:
        raise SystemExit("参数无效")

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
    stem = f"5m_macd_fixed_{safe_symbol}_{args.date}"

    csv_path = outdir / f"{stem}.csv"
    txt_path = outdir / f"{stem}.txt"

    fieldnames = list(rows[0].keys()) if rows else []
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    summary_cn = {
        "回测日期": summary["date_local"],
        "交易对": summary["symbol"],
        "MACD参数": f"{summary['macd_fast']}/{summary['macd_slow']}/{summary['macd_signal']}",
        "基础投注金额（USDT）": summary["base_stake"],
        "总投注次数": summary["bets"],
        "盈利次数": summary["wins"],
        "亏损次数": summary["losses"],
        "平局次数": summary["pushes"],
        "胜率（不含平局）%": summary["win_rate_excluding_pushes_pct"],
        "因无信号跳过": summary["skipped_no_signal"],
        "因无结算参考跳过": summary["skipped_no_settle"],
        "累计投注金额（USDT）": summary["total_staked"],
        "累计手续费（USDT）": summary["total_fee"],
        "累计净盈亏（USDT）": summary["total_net_pnl"],
        "最大单次投注额（USDT）": summary["max_single_stake"],
        "实际最大连续亏损次数": summary["actual_max_consecutive_losses"],
        "实际最大连续盈利次数": summary["actual_max_consecutive_wins"],
        "下一轮投注金额（USDT）": summary["ending_next_stake"],
        "结算规则说明": "近似模拟：用下一根5mK线收盘价替代OKX 30秒指数均价，存在偏差",
    }

    with txt_path.open("w", encoding="utf-8-sig") as f:
        f.write("OKX 5m MACD 事件倍投回测汇总（结算规则修复版）\n")
        f.write("=" * 44 + "\n")
        for key, value in summary_cn.items():
            f.write(f"{key}：{value}\n")
        f.write("\n重要说明：本回测的结算价用普通5mK线收盘价近似，"
                "OKX实际结算价为到期前30秒秒级指数K线收盘均价，"
                "两者存在偏差，回测结果仅供参考。\n")

    print("\n回测完成")
    for k, v in summary.items():
        print(f"{k}: {v}")
    print(f"\n投注明细：{csv_path.resolve()}")
    print(f"汇总文件：{txt_path.resolve()}")


if __name__ == "__main__":
    main()