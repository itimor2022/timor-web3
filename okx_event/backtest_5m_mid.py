#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OKX 5m 布林线 + 事件倍投回测（买 T+1 轮，固定开仓价 0.52）
—— T+1 双单并存 + 每笔独立链结算版 + 逆势2连K冷却

本版新增：
- 中轨上方出现 2 连阴（close<open 连续两根）-> 跳过下一轮投注一次
- 中轨下方出现 2 连阳（close>open 连续两根）-> 跳过下一轮投注一次
- 跳过期间：不改 stake、不改 losses/wins、不占槽位、不计投注次数
- 跳过只触发一次，之后恢复正常信号 + 倍投逻辑

其余规则保持不变：
- 布林线：20周期，2倍标准差
- 实体完全在中轨上方 -> LONG；完全在中轨下方 -> SHORT；跨/贴中轨 -> 跳过
- T+1 双单并存，slot 0/1 交替，各自独立 StakeChain
- 固定开仓价 --entry-price 默认 0.52
- 信号K线 T，结算基准 = T+2 close vs T+1 close
- 下单张数 = floor(stake / entry_price)，至少 1 张
- 亏损：next_stake = 该笔 stake * 2 + base
- 盈利：该笔链重置为 base
- 连亏达到 --max-losses：该笔链重置为 base

输出：TXT 汇总
"""

import argparse
import math
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
    p = argparse.ArgumentParser(description="OKX 5m 布林倍投单日回测（买T+1轮，固定开仓价，双单并存独立链，逆势2连K冷却）")
    p.add_argument("--date",
                   default=(datetime.now(LOCAL_TZ) - timedelta(days=1)).strftime("%Y-%m-%d"),
                   help="回测日期，柬埔寨时间，格式 YYYY-MM-DD；默认昨天")
    p.add_argument("--symbol", default="BTC/USDT", help="交易对，默认 BTC/USDT")

    p.add_argument("--period", type=int, default=20, help="布林线周期，默认 20")
    p.add_argument("--std", type=float, default=2.0, help="布林线标准差倍数，默认 2.0")

    p.add_argument("--base", type=float, default=10.0, help="基础投注金额（USDT），默认 10")
    p.add_argument("--entry-price", type=float, default=0.52,
                   help="固定开仓价（0~1），默认 0.52")
    p.add_argument("--max-losses", type=int, default=10,
                   help="连续亏损达到该次数后该链重置，默认 10")
    p.add_argument("--outdir", default="backtest_output", help="输出目录")
    p.add_argument("--fee", type=float, default=0.022,
                   help="每次投注按投注额比例扣除的费用，例如 0.01 表示 1%%，默认 0.022")
    p.add_argument("--cooldown-after-cross", type=int, default=0,
                   help="穿线K线之后再额外跳过 N 根K线；默认 0")
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
        time.sleep(exchange.rateLimit / 1000.0)

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


def is_yin(row):
    return float(row["close"]) < float(row["open"])


def is_yang(row):
    return float(row["close"]) > float(row["open"])


def counter_trend_two_candle(df, i, signal):
    """
    判断第 i 根信号 K 线是否构成“逆势 2 连 K”。
    - signal == LONG（中轨上方）：看最近两根是否 2 连阴
    - signal == SHORT（中轨下方）：看最近两根是否 2 连阳
    返回 True 表示应跳过下一轮投注
    """
    if i < 1:
        return False
    prev = df.iloc[i - 1]
    cur = df.iloc[i]

    if signal == "LONG":
        return is_yin(prev) and is_yin(cur)
    if signal == "SHORT":
        return is_yang(prev) and is_yang(cur)
    return False


def stake_to_contracts(stake_amount, entry_price):
    if stake_amount <= 0:
        return 0
    n = math.floor(stake_amount / entry_price)
    return max(n, 1)


class StakeChain:
    """一条独立的投注链，记录 stake 与连亏/连盈状态"""
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
    ep = args.entry_price

    chains = [StakeChain(args.base), StakeChain(args.base)]
    slot_use_count = [0, 0]

    max_chain_loss_streak = 0
    max_chain_loss_streak_per_slot = [0, 0]
    chain_reset_count = [0, 0]
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

    # 新增：逆势2连K冷却
    counter_trend_skipped = 0
    pending_skip = 0          # >0 表示下一次命中信号时要跳过的次数

    for i in range(0, len(df) - 2):
        signal_candle = df.iloc[i]
        t1_candle = df.iloc[i + 1]
        t2_candle = df.iloc[i + 2]

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

        # === 逆势2连K检查：命中则设置 pending_skip，跳过下一轮 ===
        if counter_trend_two_candle(df, i, signal):
            pending_skip = 1
            counter_trend_skipped += 1
            # 注意：这里不改变链状态，也不占用槽位，只标记下一轮跳过
            continue

        # === 若存在待跳过标记，跳过本轮，清掉标记 ===
        if pending_skip > 0:
            pending_skip -= 1
            counter_trend_skipped += 1
            continue

        t1_close = float(t1_candle["close"])
        t2_close = float(t2_candle["close"])

        if t2_close > t1_close:
            outcome = "UP"
        elif t2_close < t1_close:
            outcome = "DOWN"
        else:
            outcome = "PUSH"

        bet_no += 1
        slot_idx = bet_no % 2
        chain = chains[slot_idx]
        slot_use_count[slot_idx] += 1

        stake_this_bet = chain.stake
        contracts = stake_to_contracts(stake_this_bet, ep)
        cost = contracts * ep

        total_staked += cost
        total_contracts += contracts
        max_single_stake = max(max_single_stake, cost)
        max_single_contracts = max(max_single_contracts, contracts)
        fee = cost * args.fee
        total_fee += fee

        chain_losses_before = chain.losses
        chain_wins_before = chain.wins

        if outcome == "PUSH":
            result = "PUSH"
            gross_pnl = 0.0
            net_pnl = -fee
            pushes += 1
            chain.on_push()
            loss_streak_raw = chain.losses
            win_streak_raw = chain.wins
        else:
            won = (signal == "LONG" and outcome == "UP") or (signal == "SHORT" and outcome == "DOWN")
            if won:
                result = "WIN"
                gross_pnl = contracts * (1 - ep)
                net_pnl = gross_pnl - fee
                wins += 1
                chain.on_win(args.base)
                loss_streak_raw = chain.losses
                win_streak_raw = chain.wins
                max_win_streak = max(max_win_streak, chain.wins)
                global_loss_streak = 0
            else:
                result = "LOSS"
                gross_pnl = -cost
                net_pnl = gross_pnl - fee
                losses += 1
                streak_peak = chain.on_loss(args.base, args.max_losses)
                if streak_peak >= args.max_losses:
                    chain_reset_count[slot_idx] += 1
                loss_streak_raw = streak_peak
                win_streak_raw = chain.wins
                max_chain_loss_streak = max(max_chain_loss_streak, streak_peak)
                max_chain_loss_streak_per_slot[slot_idx] = max(
                    max_chain_loss_streak_per_slot[slot_idx], streak_peak)
                global_loss_streak += 1
                max_global_loss_streak = max(max_global_loss_streak, global_loss_streak)

        total_pnl += net_pnl
        rows.append({
            "bet_no": bet_no,
            "slot": slot_idx,
            "signal_time_local": datetime.fromtimestamp(signal_ts / 1000, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "settle_time_local": datetime.fromtimestamp(int(t2_candle["timestamp"]) / 1000, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": args.symbol,
            "signal": signal,
            "signal_open": money(signal_candle["open"]),
            "signal_close": money(signal_candle["close"]),
            "boll_middle": money(signal_candle["middle"]),
            "boll_upper": money(signal_candle["upper"]) if pd.notna(signal_candle["upper"]) else "",
            "boll_lower": money(signal_candle["lower"]) if pd.notna(signal_candle["lower"]) else "",
            "t1_close": money(t1_close),
            "t2_close": money(t2_close),
            "event_outcome": outcome,
            "entry_price": money(ep),
            "stake_this_bet": money(stake_this_bet),
            "contracts": contracts,
            "cost": money(cost),
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

    ending_stakes = {f"slot_{i}": money(c.stake) for i, c in enumerate(chains)}

    summary = {
        "date_local": datetime.fromtimestamp(day_start_ms / 1000, LOCAL_TZ).strftime("%Y-%m-%d"),
        "symbol": args.symbol,
        "boll_period": args.period,
        "boll_std": args.std,
        "base_stake": args.base,
        "entry_price": ep,
        "win_net_per_contract": round(1 - ep, 8),
        "max_consecutive_losses_param": args.max_losses,
        "fee_rate": args.fee,
        "cooldown_after_cross": args.cooldown_after_cross,
        "settle_mode": "dual_slot_independent_chain_counter_trend_skip",
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
        "max_chain_loss_streak_per_slot": max_chain_loss_streak_per_slot,
        "chain_reset_count": chain_reset_count,
        "actual_max_consecutive_wins": max_win_streak,
        "slot_use_count": slot_use_count,
        "ending_next_stake_amount": ending_stakes,
        "crossed_candles_skipped": crossed_skipped,
        "cooldown_candles_skipped": cooldown_skipped,
        "skipped_indicator_nan": skipped_na,
        "counter_trend_skipped": counter_trend_skipped,
        "settle_basis": "T+2 close vs T+1 close",
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
    if not (0.01 <= args.entry_price <= 0.99):
        raise SystemExit("--entry-price 必须在 0.01~0.99 之间")
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
    stem = f"5m_boll_t1_ctskip_{safe_symbol}_{args.date}"
    txt_path = outdir / f"{stem}.txt"

    summary_cn = {
        "回测日期": summary["date_local"],
        "交易对": summary["symbol"],
        "布林周期": summary["boll_period"],
        "布林标准差": summary["boll_std"],
        "基础投注金额（USDT）": summary["base_stake"],
        "固定开仓价": summary["entry_price"],
        "每张赢净赚（USDT）": summary["win_net_per_contract"],
        "连亏重置阈值（参数）": summary["max_consecutive_losses_param"],
        "手续费率": summary["fee_rate"],
        "穿线后冷静期（根）": summary["cooldown_after_cross"],
        "结算模式": summary["settle_mode"],
        "结算基准": summary["settle_basis"],
        "总投注次数": summary["bets"],
        "盈利次数": summary["wins"],
        "亏损次数": summary["losses"],
        "平局次数": summary["pushes"],
        "胜率（不含平局）%": summary["win_rate_excluding_pushes_pct"],
        "总张数": summary["total_contracts"],
        "累计投注金额（USDT）": summary["total_staked"],
        "累计手续费（USDT）": summary["total_fee"],
        "$$$$$ 累计净盈亏 $$$$$（USDT）": summary["total_net_pnl"],
        "最大单次投注额（USDT）": summary["max_single_stake"],
        "最大单次张数": summary["max_single_contracts"],
        "全局最大连续亏损次数（按下注顺序）": summary["max_global_loss_streak"],
        "单链最大连续亏损次数（重置前）": summary["max_chain_loss_streak"],
        "各槽位最大连亏": summary["max_chain_loss_streak_per_slot"],
        "各槽位触顶重置次数": summary["chain_reset_count"],
        "实际最大连续盈利次数（单链）": summary["actual_max_consecutive_wins"],
        "槽位使用次数": summary["slot_use_count"],
        "链末投注金额（USDT）": summary["ending_next_stake_amount"],
        "穿线K线跳过数": summary["crossed_candles_skipped"],
        "冷静期跳过K线数": summary["cooldown_candles_skipped"],
        "指标预热期跳过数": summary["skipped_indicator_nan"],
        "逆势2连K跳过次数": summary["counter_trend_skipped"],
    }

    with txt_path.open("w", encoding="utf-8-sig") as f:
        f.write("OKX 5m 布林倍投回测汇总（买T+1轮，固定开仓价，T+1双单并存独立链，逆势2连K冷却）\n")
        f.write("=" * 58 + "\n")
        for key, value in summary_cn.items():
            f.write(f"{key}：{value}\n")
        f.write("\n说明：T+1 方案下同时存在 2 笔未结算订单，\n")
        f.write("回测用两个独立 StakeChain 槽位模拟，各自独立翻倍、独立连亏计数。\n")
        f.write("逆势2连K冷却：中轨上方出现2连阴 / 中轨下方出现2连阳时，\n")
        f.write("跳过下一轮投注一次，连亏计数与 stake 保持不变，之后恢复倍投。\n")
        f.write("结算基准：T+2 close vs T+1 close（对应 T+1 轮合约到期区间）。\n")
        f.write(f"开仓价固定 {summary['entry_price']}，每张赢净赚 {summary['win_net_per_contract']}。\n")

    print("\n回测完成")
    for k, v in summary.items():
        print(f"{k}: {v}")
    print(f"汇总文件：{txt_path.resolve()}")


if __name__ == "__main__":
    main()