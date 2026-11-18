#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX 事件合约 BTC 5分钟布林线自动交易（T+1 第3分30秒确认方向后市价下单）
UTC + 北京时间双显示版
—— 单链 + 同时仅允许 1 笔未结算订单版

策略：
1. 已收盘 5 分钟 K 线 T，计算布林中轨
2. 实体完全在布林中轨上方 -> 预期 UP
   实体完全在布林中轨下方 -> 预期 DOWN
   实体穿越或触碰中轨 -> 不交易
3. 等到 T+1 这根 K 线的第 3 分 30 秒（即 T 收盘后 +3.5 分钟），
   取当前正在进行的 5 分钟 K 线（T+1），计算当前涨跌：
       当前 close > T close -> 当前为涨
       当前 close < T close -> 当前为跌
4. 只有当"当前涨跌方向"与布林预期方向一致时，才按该方向市价下单
5. 市价单成交后，等待 T+1 收盘结算（T+1 close vs T close）

单链 + 单订单：
- 只有一条 StakeChain，记录 stake / losses / wins
- 同一时间最多只允许 1 笔未结算订单
- 上一笔未结算时，后续信号一律跳过
- 上一笔结算完成后，才允许下一笔下单
- 单链连亏达到 --max-losses 时，链重置为 base

翻倍规则：
- 亏损时：下一笔 stake = 上笔成交张数 * 2 + base
- 连亏达到 max-losses 时链重置为 base

结算（T+0）：
- T+1 close vs T close
- UP: T+1 close > T close -> 赢
- DOWN: T+1 close < T close -> 赢
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
STATE_FILE = Path(__file__).resolve().parent / "5m_boll_t1_market_state.json"

API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

BEIJING_TZ = timezone(timedelta(hours=8))

BAR_MS = 5 * 60 * 1000
ENTRY_OFFSET_MS = 3 * 60 * 1000 + 30 * 1000  # T+1 第 3 分 30 秒


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


def chain_on_loss(chain, base_stake, max_losses, last_contracts=None):
    chain["losses"] += 1
    chain["wins"] = 0
    peak = chain["losses"]
    if chain["losses"] >= max_losses:
        chain["losses"] = 0
        chain["stake"] = float(base_stake)
    else:
        if last_contracts is not None and last_contracts > 0:
            chain["stake"] = float(last_contracts * 2 + base_stake)
        else:
            chain["stake"] = chain["stake"] * 2 + float(base_stake)
    return peak


def print_status(tag, state, extra=None):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    c = state["chain"]
    chain_str = f"stake={c['stake']:.2f},L={c['losses']},W={c['wins']}"
    parts = [
        f"[状态] {ts}",
        f"事件={tag}",
        f"链={{{chain_str}}}",
        f"待结算={len(state['pending_orders'])}",
    ]
    if extra:
        for k, v in extra.items():
            parts.append(f"{k}={v}")
    print("  ".join(parts))


# ============================================================
# OKX API
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


def match_contract_by_signal_time(markets, signal_ts_ms, skip=0):
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


def place_event_order_market(inst_id, outcome, contracts):
    """市价单"""
    if outcome not in ("yes", "no"):
        raise ValueError(f"非法 outcome: {outcome}")
    if contracts <= 0:
        raise ValueError("下单数量必须大于0")

    path = "/api/v5/trade/order"
    cl_ord_id = hashlib.md5(f"{inst_id}_{outcome}_{contracts}_{now_ms()}".encode()).hexdigest()[:32]

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

    print("\n========== 市价下单 ==========")
    data = _request("POST", path, payload)
    result = data["data"][0]
    if result.get("sCode") != "0":
        raise RuntimeError(f"下单失败: sCode={result.get('sCode')}, sMsg={result.get('sMsg')}")
    return result


def get_event_order(inst_id, ord_id):
    """原生查询事件合约订单状态。"""
    path = f"/api/v5/trade/order?instId={inst_id}&ordId={ord_id}"
    data = _request("GET", path).get("data", [])
    if not data:
        return None
    return data[0]


# ============================================================
# 布林信号 & 实时方向
# ============================================================

def get_boll_signal(exchange, symbol, period):
    """基于已收盘 T 计算布林方向，返回 (signal, closed_ts, closed_close, T开盘价)"""
    limit = max(period + 30, 100)
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe="5m", limit=limit)
    if len(ohlcv) < period + 2:
        return None, 0, None, None

    df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
    df["middle"] = df["c"].rolling(period).mean()
    idx = len(df) - 2  # 已收盘的那根
    row = df.iloc[idx]
    closed_ts = int(row["ts"])
    closed_close = float(row["c"])
    closed_open = float(row["o"])
    if pd.isna(row["middle"]):
        return None, closed_ts, closed_close, closed_open

    body_low = min(closed_open, closed_close)
    body_high = max(closed_open, closed_close)
    middle = float(row["middle"])

    if body_low > middle:
        return "UP", closed_ts, closed_close, closed_open
    elif body_high < middle:
        return "DOWN", closed_ts, closed_close, closed_open
    return None, closed_ts, closed_close, closed_open


def get_live_bar_direction(exchange, symbol, t_close_price):
    """
    取当前正在进行的 5 分钟 K 线（T+1），与 T 收盘价比较得出当前方向。
    返回 "UP" / "DOWN" / None（未取到数据）
    """
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe="5m", limit=3)
    if not ohlcv:
        return None
    last = ohlcv[-1]
    live_close = float(last[4])
    if live_close > t_close_price:
        return "UP"
    elif live_close < t_close_price:
        return "DOWN"
    return None


# ============================================================
# 状态
# ============================================================

def load_state(base_stake):
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                s = json.load(f)
            pending = s.get("pending_orders", [])
            if len(pending) > 1:
                pending = pending[:1]
            if "chain" in s and isinstance(s["chain"], dict):
                chain = s["chain"]
            else:
                chain = new_chain(float(s.get("stake", base_stake)))
            return {"chain": chain, "pending_orders": pending, "last_processed_ts": s.get("last_processed_ts")}
        except Exception as e:
            print(f"读取状态失败: {e}")
    return {"chain": new_chain(base_stake), "pending_orders": [], "last_processed_ts": None}


def save_state(state):
    out = dict(state)
    out["updated_at"] = datetime.now(timezone.utc).isoformat()
    temp = STATE_FILE.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    temp.replace(STATE_FILE)


# ============================================================
# 市价单成交查询
# ============================================================

def check_order_filled(state):
    """查询 pending 中订单是否已成交（市价单一般立即成交，最多查一次）"""
    if not state["pending_orders"]:
        return False
    order = state["pending_orders"][0]
    if order.get("_filled") or order.get("_dry_filled"):
        return False

    ord_id = order.get("ord_id")
    if not ord_id or ord_id == "DRY_RUN":
        order["_dry_filled"] = True
        order["filled_contracts"] = order["contracts"]
        print(f"  [DRY RUN] 模拟成交 {order['filled_contracts']} 张")
        save_state(state)
        return True

    try:
        raw = get_event_order(order["inst_id"], ord_id)
    except Exception as e:
        print(f"  [查询异常] {e}")
        return False

    if raw is None:
        return False

    state_str = raw.get("state")
    filled = raw.get("accFillSz") or raw.get("fillSz") or "0"

    if state_str in ("filled", "partially_filled") and float(filled) > 0:
        order["filled_contracts"] = int(float(filled))
        order["_filled"] = True
        print(f"  [成交] ordId={ord_id}, 成交 {order['filled_contracts']} 张，等待 T+1 收盘结算")
        save_state(state)
        return True

    # 市价单如被拒绝/取消，移出 pending
    if state_str in ("canceled", "filled_canceled"):
        print(f"  [订单结束] ordId={ord_id} state={state_str}")
        state["pending_orders"] = []
        save_state(state)
        return True

    return False


# ============================================================
# 结算：T+1 close vs T close
# ============================================================

def _find_idx(klines, target_ts):
    for i, k in enumerate(klines):
        if int(k[0]) == int(target_ts):
            return i
    return None


def settle_pending_orders(state, exchange, symbol, base_stake, max_losses):
    if not state["pending_orders"]:
        return
    order = state["pending_orders"][0]
    if not (order.get("_filled") or order.get("_dry_filled")):
        return

    klines = get_klines(exchange, symbol, limit=40)
    if len(klines) < 2:
        return
    closed_count = len(klines) - 1

    signal_ts = order.get("signal_ts")
    idx = _find_idx(klines, signal_ts)
    if idx is None:
        age_ms = now_ms() - int(order.get("created_at_ms", now_ms()))
        if age_ms > 30 * 60 * 1000:
            print(f"[结算丢弃] 信号K线不在窗口: {order.get('inst_id')}")
            state["pending_orders"] = []
            save_state(state)
        return

    if idx + 1 >= closed_count:
        return

    t_close = float(klines[idx][4])
    t1_close = float(klines[idx + 1][4])
    signal = order["signal"]

    if signal == "UP":
        win = t1_close > t_close
    elif signal == "DOWN":
        win = t1_close < t_close
    else:
        state["pending_orders"] = []
        save_state(state)
        return

    chain = state["chain"]
    last_stake = float(order["stake"])

    print(f"\n[结算] 信号={signal} inst={order.get('inst_id')}")
    print(f"  T close={t_close}, T+1 close={t1_close} -> {'盈利' if win else '亏损'}")

    if win:
        chain_on_win(chain, base_stake)
        result_str = "WIN"
    else:
        last_contracts = int(order.get("filled_contracts") or order["contracts"])
        peak = chain_on_loss(chain, base_stake, max_losses, last_contracts=last_contracts)
        result_str = "LOSS"
        print(f"  上笔张数={last_contracts}，下一笔 stake = {last_contracts}*2 + {base_stake}")
        if peak >= max_losses:
            print(f"  连亏 {peak} 次触顶，链重置为 base={base_stake}")
        else:
            print(f"  连亏={chain['losses']}, 下一笔 stake={chain['stake']:.2f}")

    print_status("已结算", state, {
        "信号": signal, "T价": t_close, "T+1价": t1_close,
        "结果": result_str, "上笔stake": last_stake, "新stake": chain["stake"],
    })

    state["pending_orders"] = []
    save_state(state)


def get_klines(exchange, symbol, limit=20):
    return exchange.fetch_ohlcv(symbol, timeframe="5m", limit=limit)


# ============================================================
# 下单：确认方向后下市价单
# ============================================================

def try_place_order(state, exchange, args, signal, closed_ts):
    if state["pending_orders"]:
        print(f"  [占用] 已有未结算订单，跳过")
        return False

    chain = state["chain"]
    stake = float(chain["stake"])
    if stake <= 0 or stake > args.max_stake:
        print(f"  stake={stake} 不可下单")
        return False

    outcome = args.up_outcome if signal == "UP" else args.down_outcome
    print(f"  stake={stake}, outcome={outcome}")

    markets = get_live_markets(args.series_id)
    if not markets:
        print("  无 live 市场")
        return False

    matched, exp_ms = match_contract_by_signal_time(markets, closed_ts, skip=0)
    if matched is None:
        print("  无 T 轮合约")
        return False

    inst_id = matched["instId"]
    print(f"  合约(T轮): {inst_id}, 到期: {dual_str(exp_ms)}")

    if exp_ms <= now_ms() or matched.get("state") != "live":
        print("  合约非live/已到期")
        return False

    contracts = int(stake)
    if contracts <= 0:
        print("  张数为0")
        return False

    ord_id = None
    if not args.live:
        print(f"  [DRY RUN] 市价 {outcome}, {contracts}张")
        ord_id = "DRY_RUN"
    else:
        print(f"  [LIVE] 市价 {outcome}, {contracts}张")
        result = place_event_order_market(inst_id, outcome, contracts)
        ord_id = result.get("ordId")
        print(f"  <下单成功> ordId={ord_id}")

    order = {
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
        "_filled": False,
        "_dry_filled": False,
    }
    state["pending_orders"].append(order)

    print_status("已下单", state, {
        "合约": inst_id, "方向": outcome, "张数": contracts, "订单号": ord_id,
    })
    save_state(state)
    return True


# ============================================================
# 主程序
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="OKX事件合约5分钟布林线（T+1 3分30秒确认方向后市价下单）")
    parser.add_argument("--series-id", default="BTC-UPDOWN-5MIN")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--period", type=int, default=20)
    parser.add_argument("--std", type=float, default=2.0)
    parser.add_argument("--base", type=float, default=1.0)
    parser.add_argument("--max-losses", type=int, default=10)
    parser.add_argument("--max-stake", type=float, default=1000.0)
    parser.add_argument("--up-outcome", choices=["yes", "no"], default="yes")
    parser.add_argument("--down-outcome", choices=["yes", "no"], default="no")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--poll-interval", type=int, default=15, help="主循环轮询间隔秒")
    args = parser.parse_args()

    if args.period < 2: sys.exit("周期>=2")
    if args.base <= 0: sys.exit("base>0")
    if args.live and not all([API_KEY, SECRET_KEY, PASSPHRASE]):
        sys.exit("LIVE需要API密钥")

    # 启动前清空状态文件
    if STATE_FILE.exists():
        try:
            STATE_FILE.unlink()
            print(f"[启动] 已清空状态文件: {STATE_FILE}")
        except Exception as e:
            print(f"[启动] 清空状态文件失败: {e}")

    exchange = ccxt.okx({
        "enableRateLimit": True,
        "apiKey": API_KEY,
        "secret": SECRET_KEY,
        "password": PASSPHRASE,
        "options": {"defaultType": "spot"},
    })
    state = load_state(args.base)

    print("=" * 65)
    print(f"模式: {'LIVE' if args.live else 'DRY RUN'}")
    print(f"系列: {args.series_id}, 品种: {args.symbol}")
    print(f"布林: 周期={args.period}, 标准差={args.std}")
    print(f"基础投注: {args.base}")
    print(f"入场时机: T+1 第 3 分 30 秒，需当前方向与布林方向一致")
    print(f"下单方式: 市价单")
    print(f"待结算: {len(state['pending_orders'])}（最多 1 笔）")
    print(f"连亏重置: {args.max_losses}, 单笔上限: {args.max_stake}")
    print(f"翻倍规则: 上笔张数*2+base，连亏{args.max_losses}次重置")
    print("=" * 65)

    while True:
        try:
            # 1) 检查 pending 市价单是否成交
            check_order_filled(state)

            # 2) 结算已成交且 T+1 已收盘的订单
            settle_pending_orders(state, exchange, args.symbol, args.base, args.max_losses)

            # 3) 仍有 pending -> 不下新单
            if state["pending_orders"]:
                time.sleep(args.poll_interval)
                continue

            # 4) 链可下注检查
            stake_now = float(state["chain"]["stake"])
            if stake_now <= 0 or stake_now > args.max_stake:
                print(f"当前 stake={stake_now} 不可下单")
                time.sleep(args.poll_interval)
                continue

            # 5) 计算布林信号（基于已收盘 T）
            signal, closed_ts, closed_close, closed_open = get_boll_signal(
                exchange, args.symbol, args.period
            )
            if not closed_ts:
                time.sleep(args.poll_interval)
                continue
            if closed_ts == state["last_processed_ts"]:
                time.sleep(args.poll_interval)
                continue

            if signal is None:
                print(f"[{dual_str(closed_ts)}] 布林无信号（穿越/触碰中轨或数据不足）")
                # 无信号也标记已处理，避免重复打印
                state["last_processed_ts"] = closed_ts
                save_state(state)
                time.sleep(args.poll_interval)
                continue

            # 6) 等待到 T+1 第 3 分 30 秒
            entry_ms = closed_ts + BAR_MS + ENTRY_OFFSET_MS
            now = now_ms()
            if now < entry_ms:
                wait_s = (entry_ms - now) // 1000
                print(f"[{dual_str(closed_ts)}] 布林方向={signal}，等待 {wait_s}s 至入场时刻 {dual_str(entry_ms)}")
                time.sleep(min(args.poll_interval, max(1, wait_s)))
                continue

            # 已过入场时刻仍未下单（例如进程刚启动），直接检查是否可入场
            # 当前仍在 T+1 这根 K 线内才有效
            if now >= closed_ts + 2 * BAR_MS:
                # 已进入 T+2，本信号过期
                print(f"[{dual_str(closed_ts)}] 信号已过期（当前已超过 T+1 区间）")
                state["last_processed_ts"] = closed_ts
                save_state(state)
                time.sleep(args.poll_interval)
                continue

            # 7) 计算当前 5 分钟实时方向
            live_dir = get_live_bar_direction(exchange, args.symbol, closed_close)
            if live_dir is None:
                print(f"  [方向未确定] 当前 close 与 T close 相等或数据缺失，跳过")
                time.sleep(args.poll_interval)
                continue

            if live_dir != signal:
                print(f"[{dual_str(closed_ts)}] 方向不一致: 布林={signal}, 当前={live_dir} -> 跳过")
                state["last_processed_ts"] = closed_ts
                save_state(state)
                time.sleep(args.poll_interval)
                continue

            # 8) 方向一致，市价下单
            print(f"\n[{dual_str(closed_ts)}] 方向确认: 布林={signal}, 当前={live_dir} -> 市价 {signal}")
            ok = try_place_order(state, exchange, args, signal, closed_ts)
            if ok:
                state["last_processed_ts"] = closed_ts
                save_state(state)
            else:
                # 下失败也标记已处理，避免同一根反复重试
                state["last_processed_ts"] = closed_ts
                save_state(state)

        except KeyboardInterrupt:
            print("\n停止")
            break
        except Exception as e:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 异常: {e}")

        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()