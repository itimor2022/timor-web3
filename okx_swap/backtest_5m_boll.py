#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OKX 5m 布林线 + 倍投回测（永续合约 T+0 单链版）
—— 信号K线收盘后下一根开盘开仓，持有一个 5m 周期平仓，单链倍投

规则：
- 布林线：20周期，2倍标准差
- 实体完全在中轨上方 -> LONG；完全在中轨下方 -> SHORT；跨/贴中轨 -> 跳过
- --cooldown-after-cross N：穿线那根之后再跳过 N 根K线
- 信号K线 T，T+1 open 开仓，T+2 close 平仓
- LONG: 平仓价 > 开仓价 -> WIN；SHORT: 平仓价 < 开仓价 -> WIN；相等 PUSH
- 张数 = floor(stake / 每张名义价值)，至少 1 张
- 亏损：next_stake = stake * 2 + base
- 盈利：链重置为 base
- 连亏达到 --max-losses：链重置为 base

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
    p = argparse.ArgumentParser(description="OKX 5m 布林倍投回测（永续 T+0 单链）")
    p.add_argument("--date",
                   default=(datetime.now(LOCAL_TZ) - timedelta(days=1)).strftime("%Y-%m-%d"),
                   help="回测日期，柬埔寨时间，格式 YYYY-MM-DD；默认昨天")
    p.add_argument("--symbol", default="BTC/USDT",
                   help="永续交易对，ccxt 格式，默认 BTC/USDT")
    p.add_argument("--period", type=int, default=20, help="布林线周期，默认 20")
    p.add_argument("--std", type=float, default=2.0, help="布林线标准差倍数，默认 2.0")
    p.add_argument("--base", type=float, default=10.0, help="基础投注金额（USDT），默认 10")
    p.add_argument("--max-losses", type=int, default=10,
                   help="连续亏损达到该次数后链重置，默认 10")
    p.add_argument("--outdir", default="backtest_output", help="输出目录")
    p.add_argument("--fee", type=float, default=0.0005,
                   help="单边手续费率（taker），默认 0.0005 = 0.05%%，开平各收一次")
    p.add_argument("--cooldown-after-cross", type=int, default=0,
                   help="穿线K线之后再额外跳过 N 根K线；默认 0")
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


def compute_boll(df, period, std_mult):
    df["middle"] = df["close"].rolling(period).mean()
    df["std"] = df["close"].rolling(period).std(ddof=0)
    df["upper"] = df["middle"] + std_mult * df["std"]
    df["lower"] = df["middle"] - std_mult * df["std"]
    return df


def boll_signal(row):
    if pd.isna(row["middle"]):
        return None, False

    body_low = min(float(row["open"]), float(row["close"]))
    body_high = max(float(row["open"]), float(row["close"]))
    middle = float(row["middle"])

    is_above = body_low > middle
    is_below = body_high < middle
    is_cross = not is_above and not is_below

    if is_cross:
        return None, True
    if is_above:
        return "LONG", False
    if is_below:
        return "SHORT", False
    return None, False


def stake_to_contracts(stake_amount, contract_value):
    if stake_amount <= 0 or contract_value <= 0:
        return 0
    n = math.floor(stake_amount / contract_value)
    return max(n, 1)


class StakeChain:
    def __init__(self, base_stake):
        self.stake = base_stake
        self.losses = 0
        self.wins = 0

    def on_win(self, base_stake):
        self.losses = 0
        self.wins += 1
        self.stake = base_stake

    def on_loss(self, base_stake, max_losses):
        self.losses += 1
        self.wins = 0
        streak_before_reset = self.losses
        if self.losses >= max_losses:
            self.losses = 0
            self.stake = base_stake
        else:
            self.stake = self.stake * 2 + base_stake
        return streak_before_reset

    def on_push(self):
        pass


def run_backtest(df, args, day_start_ms, day_end_ms):
    df = compute_boll(df.copy(), args.period, args.std)

    rows = []
    chain = StakeChain(args.base)

    max_chain_loss_streak = 0
    chain_reset_count = 0
    global_loss_streak = 0
    max_global_loss_streak = 0
    max_win_streak = 0
    max_single_stake = 0.0
    max_single_contracts = 0
    total_staked = 0.0
    total_contracts = 0
    total_pnl = 0.0
    total_fee = 0.0
    wins = losses = pushes = 0
    bet_no = 0

    crossed_skipped = 0
    cooldown_skipped = 0
    skipped_na = 0
    cooldown_left = 0

    for i in range(0, len(df) - 2):
        signal_candle = df.iloc[i]
        entry_candle = df.iloc[i + 1]
        exit_candle = df.iloc[i + 2]

        signal_ts = int(signal_candle["timestamp"])

        if signal_ts < day_start_ms or signal_ts >= day_end_ms:
            continue
        if pd.isna(signal_candle["middle"]):
            skipped_na += 1
            continue

        sig, is_cross = boll_signal(signal_candle)

        if is_cross:
            crossed_skipped += 1
            cooldown_left = args.cooldown_after_cross
            continue

        if cooldown_left > 0:
            cooldown_skipped += 1
            cooldown_left -= 1
            continue

        if sig is None:
            continue

        signal = sig
        entry_price = float(entry_candle["open"])
        exit_price = float(exit_candle["close"])

        if entry_price <= 0:
            continue

        bet_no += 1

        stake_this_bet = chain.stake
        contracts = stake_to_contracts(stake_this_bet, args.contract_value)
        notional = contracts * args.contract_value
        total_staked += notional
        total_contracts += contracts
        max_single_stake = max(max_single_stake, notional)
        max_single_contracts = max(max_single_contracts, contracts)

        chain_losses_before = chain.losses
        chain_wins_before = chain.wins

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
            chain.on_push()
            loss_streak_raw = chain.losses
            win_streak_raw = chain.wins
        elif gross_pnl > 0:
            result = "WIN"
            net_pnl = gross_pnl - fee
            wins += 1
            chain.on_win(args.base)
            loss_streak_raw = chain.losses
            win_streak_raw = chain.wins
            max_win_streak = max(max_win_streak, chain.wins)
            global_loss_streak = 0
        else:
            result = "LOSS"
            net_pnl = gross_pnl - fee
            losses += 1
            streak_peak = chain.on_loss(args.base, args.max_losses)
            if streak_peak >= args.max_losses:
                chain_reset_count += 1
            loss_streak_raw = streak_peak
            win_streak_raw = chain.wins
            max_chain_loss_streak = max(max_chain_loss_streak, streak_peak)
            global_loss_streak += 1
            max_global_loss_streak = max(max_global_loss_streak, global_loss_streak)

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
            "boll_middle": money(signal_candle["middle"]),
            "boll_upper": money(signal_candle["upper"]) if pd.notna(signal_candle["upper"]) else "",
            "boll_lower": money(signal_candle["lower"]) if pd.notna(signal_candle["lower"]) else "",
            "entry_price": money(entry_price),
            "exit_price": money(exit_price),
            "stake_this_bet": money(stake_this_bet),
            "contracts": contracts,
            "notional": money(notional),
            "result": result,
            "gross_pnl": money(gross_pnl),
            "fee": money(fee),
            "net_pnl": money(net_pnl),
            "chain_losses_before": chain_losses_before,
            "chain_wins_before": chain_wins_before,
            "chain_losses_after": chain.losses,
            "chain_wins_after": chain.wins,
            "loss_streak_raw": loss_streak_raw,
            "win_streak_raw": win_streak_raw,
            "global_loss_streak": global_loss_streak,
            "next_stake_amount": money(chain.stake),
            "cumulative_pnl": money(total_pnl),
        })

    summary = {
        "date_local": datetime.fromtimestamp(day_start_ms / 1000, LOCAL_TZ).strftime("%Y-%m-%d"),
        "symbol": args.symbol,
        "boll_period": args.period,
        "boll_std": args.std,
        "base_stake": args.base,
        "contract_value": args.contract_value,
        "max_consecutive_losses_param": args.max_losses,
        "fee_rate_per_side": args.fee,
        "cooldown_after_cross": args.cooldown_after_cross,
        "settle_mode": "perpetual_t0_single_chain",
        "settle_basis": "exit_price(T+2 close) vs entry_price(T+1 open)",
        "bets": len(rows),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_rate_excluding_pushes_pct": round(wins / (wins + losses) * 100, 2) if wins + losses else 0.0,
        "total_contracts": total_contracts,
        "total_staked": money(total_staked),
        "total_fee": money(total_fee),
        "total_net_pnl": money(total_pnl),
        "max_single_stake": money(max_single_stake),
        "max_single_contracts": max_single_contracts,
        "max_global_loss_streak": max_global_loss_streak,
        "max_chain_loss_streak": max_chain_loss_streak,
        "chain_reset_count": chain_reset_count,
        "actual_max_consecutive_wins": max_win_streak,
        "ending_next_stake_amount": money(chain.stake),
        "crossed_candles_skipped": crossed_skipped,
        "cooldown_candles_skipped": cooldown_skipped,
        "skipped_indicator_nan": skipped_na,
    }
    return rows, summary


def main():
    args = parse_args()
    if args.period < 2:
        raise SystemExit("--period 必须 >= 2")
    if args.std <= 0:
        raise SystemExit("--std 必须 > 0")
    if args.base <= 0 or args.max_losses < 1 or args.fee < 0:
        raise SystemExit("参数无效：base>0、max-losses>=1、fee>=0")
    if args.cooldown_after_cross < 0:
        raise SystemExit("--cooldown-after-cross 必须 >= 0")
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
    stem = f"5m_boll_{safe_symbol}_{args.date}"
    txt_path = outdir / f"{stem}.txt"

    summary_cn = {
        "回测日期": summary["date_local"],
        "交易对": summary["symbol"],
        "布林周期": summary["boll_period"],
        "布林标准差": summary["boll_std"],
        "基础投注金额（USDT）": summary["base_stake"],
        "每张名义价值（USDT）": summary["contract_value"],
        "连亏重置阈值（参数）": summary["max_consecutive_losses_param"],
        "单边手续费率": summary["fee_rate_per_side"],
        "穿线后冷静期（根）": summary["cooldown_after_cross"],
        "结算模式": summary["settle_mode"],
        "结算基准": summary["settle_basis"],
        "总投注次数": summary["bets"],
        "盈利次数": summary["wins"],
        "亏损次数": summary["losses"],
        "平局次数": summary["pushes"],
        "胜率（不含平局）%": summary["win_rate_excluding_pushes_pct"],
        "总张数": summary["total_contracts"],
        "累计名义价值（USDT）": summary["total_staked"],
        "累计手续费（USDT）": summary["total_fee"],
        "$$$$$ 累计净盈亏 $$$$$（USDT）": summary["total_net_pnl"],
        "最大单次名义价值（USDT）": summary["max_single_stake"],
        "最大单次张数": summary["max_single_contracts"],
        "全局最大连续亏损次数": summary["max_global_loss_streak"],
        "单链最大连续亏损次数（重置前）": summary["max_chain_loss_streak"],
        "触顶重置次数": summary["chain_reset_count"],
        "实际最大连续盈利次数": summary["actual_max_consecutive_wins"],
        "链末投注金额（USDT）": summary["ending_next_stake_amount"],
        "穿线K线跳过数": summary["crossed_candles_skipped"],
        "冷静期跳过K线数": summary["cooldown_candles_skipped"],
        "指标预热期跳过数": summary["skipped_indicator_nan"],
    }

    with txt_path.open("w", encoding="utf-8-sig") as f:
        f.write("OKX 5m 布林倍投回测汇总（永续 T+0 单链）\n")
        f.write("=" * 58 + "\n")
        for key, value in summary_cn.items():
            f.write(f"{key}：{value}\n")
        f.write("\n说明：永续 T+0 模型，信号K线收盘后下一根开盘开仓，\n")
        f.write("持有到再下一根收盘平仓。LONG 平仓价>开仓价算赢，SHORT 反之。\n")
        f.write("单链倍投：亏损 next_stake = stake*2+base，盈利或触顶重置为 base。\n")
        f.write("PnL = 张数 × 每张名义价值 × 价格变动比例，开平各收一次手续费。\n")

    print("\n回测完成")
    for k, v in summary.items():
        print(f"{k}: {v}")
    print(f"汇总文件：{txt_path.resolve()}")


if __name__ == "__main__":
    main()