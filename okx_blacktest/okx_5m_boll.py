#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OKX 5m布林线 + 事件倍投回测（改进版）
默认模拟规则：
- 使用 OKX 公共现货 OHLCV 数据，不需要 API Key
- 布林线：20周期，2倍标准差（可用 --period / --std 调整）
- 【方向判断】只有信号K线实体"完整站在中轨一侧"才投注：
    实体完全在中轨上方 -> LONG
    实体完全在中轨下方 -> SHORT
    实体跨过/贴住中轨（上穿或下穿那根）-> 跳过，不投注
- 【可选】--cooldown-after-cross N：穿线那根之后再跳过 N 根K线，
  等价格真正站稳一侧才参与（默认 0，即穿线那根跳过、下一根即可参与）
- 下一根K线模拟事件结算：close > open 为上涨，close < open 为下跌
- 平局默认不输不赢（净盈亏 0，仅扣手续费），倍投状态不变
- 投注序列：1、3、7、15、31（按 max-losses 决定），连亏达到阈值后下一轮恢复基础投入
- 默认单日柬埔寨时区日期，可通过 --date 指定
输出：
- 每次投注记录 CSV（含真实连亏/连赢计数）
- 汇总 TXT（含最大单注、真实最大连亏/连赢、穿线跳过统计）
注意：这是对普通K线涨跌的代理模拟，不代表 OKX 特定预测产品的真实赔率/结算规则。
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
    p = argparse.ArgumentParser(description="OKX 5m布林倍投单日回测（改进版）")
    p.add_argument("--date",
                   default=(datetime.now(LOCAL_TZ) - timedelta(days=1)).strftime("%Y-%m-%d"),
                   help="回测日期，柬埔寨时间，格式 YYYY-MM-DD；默认昨天")
    p.add_argument("--symbol", default="BTC/USDT", help="交易对，默认 BTC/USDT")
    p.add_argument("--period", type=int, default=20, help="布林线周期，默认 20")
    p.add_argument("--std", type=float, default=2.0, help="布林线标准差倍数，默认 2.0")
    p.add_argument("--base", type=float, default=10.0, help="基础投注金额，默认 10 USDT")
    p.add_argument("--max-losses", type=int, default=10,
                   help="连续亏损达到该次数后下一轮重置，默认 10")
    p.add_argument("--payout", type=float, default=1.0,
                   help="获胜净利润倍率：投注1，赢时净赚 payout 倍投注；默认1.0（等额赔率净利润）")
    p.add_argument("--outdir", default="backtest_output", help="输出目录")
    p.add_argument("--fee", type=float, default=0.0,
                   help="每次投注按投注额比例扣除的费用，例如 0.01 表示 1%")

    # === 新增：穿线后冷静期 ===
    p.add_argument("--cooldown-after-cross", type=int, default=0,
                   help="穿线K线之后再额外跳过 N 根K线（等站稳再参与）；默认 0。"
                        "注意：穿线那根本身永远会被跳过")
    return p.parse_args()


def validate_date(s):
    try:
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
    except ValueError:
        raise SystemExit("--date 必须使用 YYYY-MM-DD 格式")
    return dt


def fetch_day(exchange, symbol, start_ms, end_ms):
    """分页获取日期范围内的5mK线，额外向前取数据用于布林线预热。"""
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
    df["middle"] = df["close"].rolling(args.period).mean()
    df["std"] = df["close"].rolling(args.period).std(ddof=0)
    df["upper"] = df["middle"] + args.std * df["std"]
    df["lower"] = df["middle"] - args.std * df["std"]
    df["middle_prev"] = df["middle"].shift(1)

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

    # 统计：穿线跳过的K线数、冷静期跳过的K线数、指标未预热跳过数
    crossed_skipped = 0
    cooldown_skipped = 0
    skipped_na = 0

    # 冷静期剩余计数
    cooldown_left = 0

    for i in range(0, len(df) - 1):
        signal_candle = df.iloc[i]
        settle_candle = df.iloc[i + 1]
        signal_ts = int(signal_candle["timestamp"])

        if signal_ts < day_start_ms or signal_ts >= day_end_ms:
            continue
        if pd.isna(signal_candle["middle"]) or pd.isna(signal_candle["middle_prev"]):
            skipped_na += 1
            continue

        body_low = min(float(signal_candle["open"]), float(signal_candle["close"]))
        body_high = max(float(signal_candle["open"]), float(signal_candle["close"]))
        middle = float(signal_candle["middle"])
        middle_prev = float(signal_candle["middle_prev"])

        # 判断实体与中轨的关系
        is_above = body_low > middle            # 实体完全在中轨上方
        is_below = body_high < middle           # 实体完全在中轨下方
        is_cross = not is_above and not is_below  # 实体跨过/贴住中轨（穿线那根）

        # 1) 穿线那根：永远跳过，不投注，并重置冷静期
        if is_cross:
            crossed_skipped += 1
            cooldown_left = args.cooldown_after_cross
            continue

        # 2) 冷静期：穿线后额外跳过的K线
        if cooldown_left > 0:
            cooldown_skipped += 1
            cooldown_left -= 1
            continue

        # 3) 到这里，实体完整在中轨一侧，可以定方向
        if is_above:
            raw_signal = "LONG"
        elif is_below:
            raw_signal = "SHORT"
        else:
            continue  # 理论上不会到这里

        signal = raw_signal

        next_open = float(settle_candle["open"])
        next_close = float(settle_candle["close"])
        if next_close > next_open:
            outcome = "UP"
        elif next_close < next_open:
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
            "settle_time_local": datetime.fromtimestamp(int(settle_candle["timestamp"]) / 1000, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": args.symbol,
            "signal": signal,
            "signal_open": money(signal_candle["open"]),
            "signal_close": money(signal_candle["close"]),
            "boll_middle": money(middle),
            "boll_middle_prev": money(middle_prev),
            "boll_upper": money(signal_candle["upper"]) if pd.notna(signal_candle["upper"]) else "",
            "boll_lower": money(signal_candle["lower"]) if pd.notna(signal_candle["lower"]) else "",
            "settle_open": money(next_open),
            "settle_close": money(next_close),
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
        "period": args.period,
        "std_multiplier": args.std,
        "base_stake": args.base,
        "max_consecutive_losses_param": args.max_losses,
        "payout_net_multiplier": args.payout,
        "fee_rate": args.fee,
        "cooldown_after_cross": args.cooldown_after_cross,
        "bets": len(rows),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_rate_excluding_pushes_pct": round(wins / (wins + losses) * 100, 2) if wins + losses else 0.0,
        "total_staked": money(total_staked),
        "total_fee": money(total_fee),
        "total_net_pnl": money(total_pnl),
        "max_single_stake": money(max_single_stake),
        "actual_max_consecutive_losses": max_loss_streak,
        "actual_max_consecutive_wins": max_win_streak,
        "ending_next_stake": money(stake),
        "crossed_candles_skipped": crossed_skipped,
        "cooldown_candles_skipped": cooldown_skipped,
        "skipped_indicator_nan": skipped_na,
    }
    return rows, summary


def main():
    args = parse_args()
    if args.period < 2 or args.base <= 0 or args.max_losses < 1 or args.payout < 0 or args.fee < 0:
        raise SystemExit("参数无效：period>=2、base>0、max-losses>=1、payout>=0、fee>=0")
    if args.cooldown_after_cross < 0:
        raise SystemExit("--cooldown-after-cross 必须 >= 0")

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
    stem = f"5m_boll_{safe_symbol}_{args.date}"

    csv_path = outdir / f"{stem}.csv"
    txt_path = outdir / f"{stem}.txt"

    fieldnames = list(rows[0].keys()) if rows else [
        "bet_no", "signal_time_local", "settle_time_local", "symbol", "signal",
        "result", "stake", "net_pnl", "loss_streak_raw", "win_streak_raw",
        "next_stake", "cumulative_pnl"
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    summary_cn = {
        "回测日期": summary["date_local"],
        "交易对": summary["symbol"],
        "布林线周期": summary["period"],
        "标准差倍数": summary["std_multiplier"],
        "基础投注金额（USDT）": summary["base_stake"],
        "连亏重置阈值（参数）": summary["max_consecutive_losses_param"],
        "中奖净利润倍数": summary["payout_net_multiplier"],
        "手续费率": summary["fee_rate"],
        "穿线后冷静期（根）": summary["cooldown_after_cross"],
        "总投注次数": summary["bets"],
        "盈利次数": summary["wins"],
        "亏损次数": summary["losses"],
        "平局次数": summary["pushes"],
        "胜率（不含平局）%": summary["win_rate_excluding_pushes_pct"],
        "累计投注金额（USDT）": summary["total_staked"],
        "累计手续费（USDT）": summary["total_fee"],
        "$$$$$ 累计净盈亏 $$$$$（USDT）": summary["total_net_pnl"],
        "最大单次投注额（USDT）": summary["max_single_stake"],
        "实际最大连续亏损次数": summary["actual_max_consecutive_losses"],
        "实际最大连续盈利次数": summary["actual_max_consecutive_wins"],
        "下一轮投注金额（USDT）": summary["ending_next_stake"],
        "穿线K线跳过数": summary["crossed_candles_skipped"],
        "冷静期跳过K线数": summary["cooldown_candles_skipped"],
        "指标预热期跳过数": summary["skipped_indicator_nan"],
    }

    with txt_path.open("w", encoding="utf-8-sig") as f:
        f.write("OKX 5m布林倍投回测汇总（改进版）\n")
        f.write("=" * 44 + "\n")
        for key, value in summary_cn.items():
            f.write(f"{key}：{value}\n")
        f.write("\n说明：本回测使用普通5mK线模拟涨跌，不代表OKX特定事件的真实结算结果。\n")

    print("\n回测完成")
    for k, v in summary.items():
        print(f"{k}: {v}")
    print(f"\n投注明细：{csv_path.resolve()}")
    print(f"汇总文件：{txt_path.resolve()}")


if __name__ == "__main__":
    main()