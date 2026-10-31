#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX 事件合约 BTC 5分钟布林线自动交易（买 T+1 轮）
UTC + 北京时间双显示版
—— pending 订单列表 + 双独立链版

策略：
1. 已收盘5分钟K线 T
2. 实体完全在布林中轨上方 -> UP -> YES
3. 实体完全在布林中轨下方 -> DOWN -> NO
4. 实体穿越或触碰中轨 -> 不交易

买 T+1 轮：
- 信号来自 T，但下单目标是 expTime > T+5min 的第二个合约（到期 T+10min）
- 结算用 T+2 close vs T+1 close（对应 T+1 合约的到期区间）
- 方向仍用 T 的布林信号

双独立链（与回测对齐）：
- 两个槽位 slot 0/1 交替分配，各自维护 stake / losses / wins
- 下单时从分配到的槽位继承 stake
- 结算时把结果写回该笔订单自己的槽位，不影响另一槽位
- 单链连亏达到 --max-losses 时，该链重置为 base（不是全局熔断）
- 所有槽位 stake<=0 或全部熔断时才停止下单

输赢判定：
- UP 信号：T+2 close > T+1 close -> 赢
- DOWN 信号：T+2 close < T+1 close -> 赢
- 相等视为输

资金管理（单链）：
- 盈利：该链 losses 清零，wins+1，stake = base
- 亏损：该链 losses+1，wins 清零，stake = stake*2 + base
- 该链 losses 达到 --max-losses：该链 losses=0，stake=base（重置）
- 投注张数超过 --max-stake：该笔不下单，跳过

状态日志：
- 每笔订单下单成功输出 [状态] 已下单
- 每笔订单结算完成输出 [状态] 已结算
"""

import argparse
import hashlib
import hmac
import base64
import json
import os
import sys
import time

from datetime import datetime, timezone, timedelta
from pathlib import Path

import ccxt
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://www.okx.com"
STATE_FILE = Path(__file__).resolve().parent / "5m_boll_t1_2_state.json"

API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

BEIJING_TZ = timezone(timedelta(hours=8))


# ============================================================
# 工具
# ============================================================

def dual_str(ms):
    utc_part = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    bj_part = datetime.fromtimestamp(ms / 1000, tz=BEIJING_TZ).strftime("%H:%M:%S 北京")
    return f"{utc_part} / {bj_part}"


def now_ms():
    return int(time.time() * 1000)


def new_chain(base_stake):
    return {"stake": float(base_stake), "losses": 0, "wins": 0}


def chain_on_win(chain, base_stake):
    chain["losses"] = 0
    chain["wins"] += 1
    chain["stake"] = float(base_stake)


def chain_on_loss(chain, base_stake, max_losses):
    """返回重置前的连亏 peak，与回测 on_loss 对齐"""
    chain["losses"] += 1
    chain["wins"] = 0
    peak = chain["losses"]
    if chain["losses"] >= max_losses:
        chain["losses"] = 0
        chain["stake"] = float(base_stake)
    else:
        chain["stake"] = chain["stake"] * 2 + float(base_stake)
    return peak


def chain_on_push(chain):
    pass


def print_status(tag, state, extra=None):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    chains_str = ", ".join(
        f"slot{i}(stake={c['stake']:.2f},L={c['losses']},W={c['wins']})"
        for i, c in enumerate(state["chains"])
    )
    parts = [
        f"[状态] {ts}",
        f"事件={tag}",
        f"链={{{chains_str}}}",
        f"待结算={len(state['pending_orders'])}",
    ]
    if extra:
        for k, v in extra.items():
            parts.append(f"{k}={v}")
    print("  ".join(parts))


# ============================================================
# OKX API（与原脚本一致）
# ============================================================

def _timestamp():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _sign(ts, method, path, body=""):
    mac = hmac.new(SECRET_KEY.encode(), (ts + method.upper() + path + body).encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def _headers(method, path, body=""):
    ts = _timestamp()
    return {
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": _sign(ts, method, path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
    }


def _request(method, path, payload=None):
    method = method.upper()
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if payload is not None else ""
    headers = _headers(method, path, body)
    url = BASE_URL + path

    if method == "GET":
        resp = requests.get(url, headers=headers, timeout=15)
    else:
        resp = requests.post(url, headers=headers, data=body.encode("utf-8"), timeout=15)

    resp.raise_for_status()
    data = resp.json()

    if data.get("code") != "0":
        detail = ""
        if data.get("data"):
            item = data["data"][0]
            detail = f" | sCode={item.get('sCode')} | sMsg={item.get('sMsg')}"
        raise RuntimeError(f"API错误 [{data.get('code')}]: {data.get('msg')}{detail}")
    return data


def get_live_markets(series_id):
    path = f"/api/v5/public/event-contract/markets?seriesId={series_id}&state=live"
    return _request("GET", path).get("data", [])


def match_contract_by_signal_time(markets, signal_ts_ms, skip=1):
    candidates = []
    for m in markets:
        exp = m.get("expTime")
        if not exp:
            continue
        try:
            exp_ms = int(exp)
        except (ValueError, TypeError):
            continue
        if exp_ms > signal_ts_ms:
            candidates.append((exp_ms, m))

    candidates.sort(key=lambda x: x[0])

    if len(candidates) <= skip:
        return None, None

    return candidates[skip][1], candidates[skip][0]


def place_event_order(inst_id, outcome, contracts):
    if outcome not in ("yes", "no"):
        raise ValueError(f"非法 outcome: {outcome}")
    if contracts <= 0:
        raise ValueError("下单数量必须大于0")

    path = "/api/v5/trade/order"
    cl_ord_id = hashlib.md5(f"{inst_id}_{outcome}_{contracts}".encode()).hexdigest()[:32]

    payload = {
        "instId": inst_id,
        "tdMode": "isolated",
        "clOrdId": cl_ord_id,
        "side": "buy",
        "ordType": "market",
        "sz": str(contracts),
        "outcome": outcome,
        "speedBump": "1",
    }

    print("\n========== 下单结果 ==========")
    data = _request("POST", path, payload)
    result = data["data"][0]

    if result.get("sCode") != "0":
        raise RuntimeError(f"下单失败: sCode={result.get('sCode')}, sMsg={result.get('sMsg')}")
    return result


# ============================================================
# 布林信号（与原脚本一致）
# ============================================================

def get_boll_signal(exchange, symbol, period, std_mult):
    limit = max(period + 30, 100)
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe="5m", limit=limit)

    if len(ohlcv) < period + 2:
        return None, (int(ohlcv[-1][0]) if ohlcv else 0), None

    df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
    df["middle"] = df["c"].rolling(period).mean()

    idx = len(df) - 2
    row = df.iloc[idx]
    closed_ts = int(row["ts"])
    closed_close = float(row["c"])

    if pd.isna(row["middle"]):
        return None, closed_ts, closed_close

    body_low = min(float(row["o"]), float(row["c"]))
    body_high = max(float(row["o"]), float(row["c"]))
    middle = float(row["middle"])

    if body_low > middle:
        return "UP", closed_ts, closed_close
    elif body_high < middle:
        return "DOWN", closed_ts, closed_close
    return None, closed_ts, closed_close


def get_klines(exchange, symbol, limit=20):
    return exchange.fetch_ohlcv(symbol, timeframe="5m", limit=limit)


# ============================================================
# 状态（新结构）
# ============================================================

def load_state(base_stake, max_losses_unused=None):
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                s = json.load(f)

            # 兼容旧版：pending 布尔 -> pending_orders 列表
            if "pending_orders" not in s:
                legacy_pending = bool(s.get("pending"))
                legacy_signal = s.get("last_signal")
                legacy_signal_ts = s.get("last_signal_ts")
                legacy_stake = s.get("last_stake")

                orders = []
                if legacy_pending and legacy_signal and legacy_signal_ts is not None:
                    # 旧版没有 inst_id/ord_id，用占位符标记，等待人工处理
                    orders.append({
                        "slot": 0,
                        "inst_id": None,
                        "ord_id": None,
                        "outcome": "yes" if legacy_signal == "UP" else "no",
                        "signal": legacy_signal,
                        "signal_ts": int(legacy_signal_ts),
                        "stake": float(legacy_stake or base_stake),
                        "contracts": int(legacy_stake or base_stake),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "_legacy": True,
                    })
                    print(f"[警告] 检测到旧版 pending，已迁移为 1 条 pending_orders（slot=0, 无 inst_id）")

                s["pending_orders"] = orders

            chains = s.get("chains")
            if not isinstance(chains, list) or len(chains) != 2:
                # 迁移：旧的单链 stake 作为 slot0，slot1 用 base
                old_stake = float(s.get("stake", base_stake))
                chains = [new_chain(old_stake), new_chain(base_stake)]
                print(f"[警告] 旧状态迁移为双链：slot0.stake={old_stake}, slot1.stake={base_stake}")

            return {
                "chains": chains,
                "next_slot": int(s.get("next_slot", 0)) % 2,
                "pending_orders": s.get("pending_orders", []),
                "last_processed_ts": s.get("last_processed_ts"),
                "last_signal": s.get("last_signal"),
                "last_signal_ts": s.get("last_signal_ts"),
            }
        except Exception as e:
            print(f"读取状态失败: {e}")

    return {
        "chains": [new_chain(base_stake), new_chain(base_stake)],
        "next_slot": 0,
        "pending_orders": [],
        "last_processed_ts": None,
        "last_signal": None,
        "last_signal_ts": None,
    }


def save_state(state):
    out = dict(state)
    out["updated_at"] = datetime.now(timezone.utc).isoformat()
    temp = STATE_FILE.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    temp.replace(STATE_FILE)


# ============================================================
# 结算：遍历 pending_orders，按各自 signal_ts 独立结算
# ============================================================

def _find_idx(klines, target_ts):
    for i, k in enumerate(klines):
        if int(k[0]) == int(target_ts):
            return i
    return None


def settle_pending_orders(state, exchange, symbol, base_stake, max_losses, max_stake):
    if not state["pending_orders"]:
        return

    klines = get_klines(exchange, symbol, limit=40)
    if len(klines) < 3:
        return

    # klines[-1] 是当前未收盘，可用已收盘区间 = klines[0..len-2]
    closed_count = len(klines) - 1

    still_pending = []
    for order in state["pending_orders"]:
        signal_ts = order.get("signal_ts")
        if signal_ts is None:
            print(f"[结算跳过] 订单缺少 signal_ts，丢弃: {order}")
            continue

        idx = _find_idx(klines, signal_ts)
        if idx is None:
            # 信号 K 线已滑出窗口；若 order 创建已超过 30 分钟，丢弃避免永久挂起
            age_ms = now_ms() - int(order.get("created_at_ms", now_ms()))
            if age_ms > 30 * 60 * 1000:
                print(f"[结算丢弃] 信号K线不在窗口且已超30分钟: {order.get('inst_id')}")
            else:
                still_pending.append(order)
            continue

        # 需要 T+1 和 T+2 都已收盘（T+2 索引必须 < closed_count）
        if idx + 2 >= closed_count:
            still_pending.append(order)
            continue

        t1_close = float(klines[idx + 1][4])
        t2_close = float(klines[idx + 2][4])

        signal = order["signal"]
        slot = int(order["slot"])
        last_stake = float(order["stake"])
        contracts = int(order["contracts"])

        if signal == "UP":
            win = t2_close > t1_close
        elif signal == "DOWN":
            win = t2_close < t1_close
        else:
            print(f"[结算跳过] 非法 signal={signal}")
            continue

        chain = state["chains"][slot]

        print(f"\n[结算] slot={slot} 信号={signal} inst={order.get('inst_id')}")
        print(f"  T+1 close={t1_close}, T+2 close={t2_close} -> {'盈利' if win else '亏损'}")

        if win:
            chain_on_win(chain, base_stake)
            result_str = "WIN"
        else:
            peak = chain_on_loss(chain, base_stake, max_losses)
            result_str = "LOSS"
            if peak >= max_losses:
                print(f"  [slot{slot}] 连亏 {peak} 次触顶，该链重置为 base={base_stake}")
            else:
                print(f"  [slot{slot}] 连亏={chain['losses']}, 下一笔 stake={chain['stake']:.2f}")

        print_status("已结算", state, {
            "slot": slot,
            "信号": signal,
            "T+1价": t1_close,
            "T+2价": t2_close,
            "结果": result_str,
            "上笔stake": last_stake,
            "该链新stake": chain["stake"],
        })

    state["pending_orders"] = still_pending
    save_state(state)


# ============================================================
# 下单：分配槽位、从该槽位继承 stake
# ============================================================

def try_place_order(state, exchange, args, signal, closed_ts):
    """
    返回 True 表示已下单（或 dry run 已记录），False 表示跳过
    """
    # 分配槽位（交替）
    slot = state["next_slot"]
    chain = state["chains"][slot]

    stake = float(chain["stake"])
    if stake <= 0:
        print(f"  [slot{slot}] stake={stake} <= 0，跳过")
        return False

    if stake > args.max_stake:
        print(f"  [slot{slot}] stake={stake} 超过 --max-stake={args.max_stake}，跳过")
        return False

    outcome = args.up_outcome if signal == "UP" else args.down_outcome
    print(f"  slot={slot}, stake={stake}, outcome={outcome}")

    markets = get_live_markets(args.series_id)
    if not markets:
        print("  无 live 市场")
        return False

    matched, exp_ms = match_contract_by_signal_time(markets, closed_ts, skip=1)
    if matched is None:
        print("  无 T+1 轮合约（可能只有 1 个 live 合约）")
        return False

    inst_id = matched["instId"]
    print(f"  合约(T+1轮): {inst_id}, 到期: {dual_str(exp_ms)}")

    if exp_ms <= now_ms() or matched.get("state") != "live":
        print("  合约非live/已到期")
        return False

    # 张数：回测是 floor(stake / entry_price) 至少 1；实盘这里直接按 stake 张数
    # 若你的合约 1 张 = 1 USDT 成本，则 contracts = int(stake) 即等价
    contracts = int(stake)
    if contracts <= 0:
        print("  张数为0")
        return False

    ord_id = None
    if not args.live:
        print(f"  [DRY RUN] 将买入 {outcome}, {contracts}张")
        ord_id = "DRY_RUN"
    else:
        print(f"  [LIVE] 买入 {outcome}, {contracts}张")
        result = place_event_order(inst_id, outcome, contracts)
        ord_id = result.get("ordId")
        print(f"  <下单成功> ordId={ord_id}")

    order = {
        "slot": slot,
        "inst_id": inst_id,
        "ord_id": ord_id,
        "outcome": outcome,
        "signal": signal,
        "signal_ts": int(closed_ts),
        "stake": stake,
        "contracts": contracts,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_at_ms": now_ms(),
        "exp_time_ms": int(exp_ms),
    }
    state["pending_orders"].append(order)
    state["next_slot"] = 1 - slot

    print_status("已下单", state, {
        "slot": slot,
        "合约": inst_id,
        "方向": outcome,
        "张数": contracts,
        "信号": signal,
        "订单号": ord_id,
    })
    save_state(state)
    return True


# ============================================================
# 主程序
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="OKX事件合约5分钟布林线自动交易（买T+1轮，双独立链）")
    parser.add_argument("--series-id", default="BTC-UPDOWN-5MIN")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--period", type=int, default=20)
    parser.add_argument("--std", type=float, default=2.0)
    parser.add_argument("--base", type=float, default=10.0)
    parser.add_argument("--max-losses", type=int, default=10,
                        help="单链连亏达此值时该链重置为 base（与回测一致）")
    parser.add_argument("--max-stake", type=float, default=1000.0,
                        help="单笔 stake 上限，超过则跳过该笔")
    parser.add_argument("--up-outcome", choices=["yes", "no"], default="yes")
    parser.add_argument("--down-outcome", choices=["yes", "no"], default="no")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--poll-interval", type=int, default=15)
    args = parser.parse_args()

    if args.period < 2:
        print("错误：布林周期必须>=2"); sys.exit(1)
    if args.base <= 0:
        print("错误：基础投注必须>0"); sys.exit(1)
    if args.poll_interval < 1:
        print("错误：轮询间隔必须>0"); sys.exit(1)
    if args.max_losses < 1:
        print("错误：--max-losses 必须>=1"); sys.exit(1)
    if args.max_stake <= 0:
        print("错误：--max-stake 必须>0"); sys.exit(1)
    if args.live and not all([API_KEY, SECRET_KEY, PASSPHRASE]):
        print("错误：LIVE需要配置API密钥"); sys.exit(1)

    # 启动前清空状态文件，从全新状态开始
    if STATE_FILE.exists():
        try:
            STATE_FILE.unlink()
            print(f"[启动] 已清空状态文件: {STATE_FILE}")
        except Exception as e:
            print(f"[启动] 清空状态文件失败: {e}")
            
    exchange = ccxt.okx({"enableRateLimit": True})
    state = load_state(args.base)

    print("=" * 65)
    print(f"模式: {'LIVE' if args.live else 'DRY RUN'}")
    print(f"系列: {args.series_id}, 品种: {args.symbol}")
    print(f"布林: 周期={args.period}, 标准差={args.std}")
    print(f"基础投注: {args.base}")
    print(f"双链: slot0.stake={state['chains'][0]['stake']:.2f}, slot1.stake={state['chains'][1]['stake']:.2f}")
    print(f"下一槽位: {state['next_slot']}")
    print(f"待结算订单数: {len(state['pending_orders'])}")
    print(f"单链连亏重置阈值: {args.max_losses}, 单笔上限: {args.max_stake}")
    print("策略: 买 T+1 轮（跳过最近合约）")
    print("结算: T+2 close vs T+1 close")
    print("=" * 65)

    while True:
        try:
            # 1) 先结算所有可结算的 pending 订单
            settle_pending_orders(state, exchange, args.symbol, args.base, args.max_losses, args.max_stake)

            # 2) 判断是否还有可下单的槽位
            any_chain_alive = any(float(c["stake"]) > 0 and float(c["stake"]) <= args.max_stake
                                  for c in state["chains"])
            if not any_chain_alive:
                print("所有槽位均熔断/超限，跳过下单")
                time.sleep(args.poll_interval)
                continue

            # 3) 取信号
            signal, closed_ts, closed_close = get_boll_signal(exchange, args.symbol, args.period, args.std)
            if not closed_ts:
                time.sleep(args.poll_interval)
                continue
            if closed_ts == state["last_processed_ts"]:
                time.sleep(args.poll_interval)
                continue

            state["last_processed_ts"] = closed_ts
            save_state(state)

            if signal is None:
                print(f"[{dual_str(closed_ts)}] 无信号")
                time.sleep(args.poll_interval)
                continue

            print(f"\n[{dual_str(closed_ts)}] 信号: {signal}")

            # 4) 尝试下单（内部处理槽位分配、stake 校验、合约匹配）
            try_place_order(state, exchange, args, signal, closed_ts)

        except KeyboardInterrupt:
            print("\n停止")
            break
        except Exception as e:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 异常: {e}")

        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()