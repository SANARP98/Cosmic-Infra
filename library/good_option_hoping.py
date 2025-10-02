#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auto-Seller (Options Only, Dual-Engine): Robust SELL LIMIT manager for *options*.

Design goals
- **Does NOT touch stocks**. Only NFO/BFO options (CE/PE).
- Exit price always reflects **current open entry only** (never polluted by past round-trips).
- Dual-engine costing:
  A) **Session Averaging** from live executions since the moment we went flat→long
  B) **Ledger Pairing (FIFO)** across a lookback window to reconstruct open inventory
- One-cycle confirmation before first placement to avoid false triggers.
- Respect manual order price overrides.
- Print clear, human-readable logs that explain what the bot is doing.

Usage
  pip install openalgo python-dotenv
  # Run alongside your existing equity bot safely.

Environment (defaults below can be overridden via .env)
  API_KEY
  OPENALGO_HOST (default https://openalgo.rpinj.shop)
  OPENALGO_WS   (default wss://openalgows.rpinj.shop)
  OPENALGO_OPTION_PRODUCTS   NRML,MIS
  OPENALGO_OPTION_TICK       0.05
  AUTO_SELL_MARGIN_OPT       0.35
  USE_EXECUTIONS             1   (enable dual-engine)
  LEDGER_LOOKBACK_DAYS       45
  LEDGER_USE_SYNTHETIC_BOOTSTRAP 1
  DISCREPANCY_PCT            0.5
  OPENALGO_WS_QUIET          0 (set 1 to silence library logs)

Notes
- We read **positionbook()** and **orderbook()**; for executions we try **tradebook()** then **orderhistory()**.
- No DB writes. No file writes. All in-memory. (SQLite can be added later.)
- LTP prints for tracked option symbols (as requested).
"""

import os
import time
import threading
import signal
from typing import Dict, Tuple, List, Set, Optional
import sys
import logging
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from openalgo import api

load_dotenv()

# ───────────────────────────────── CONFIG (with defaults) ─────────────────────────────────
API_KEY   = os.getenv("API_KEY", "").strip()
HOST      = os.getenv("OPENALGO_HOST", "https://openalgo.rpinj.shop").strip()
WS_URL    = os.getenv("OPENALGO_WS", "wss://openalgows.rpinj.shop").strip()

OPTION_PRODUCTS = [p.strip().upper() for p in os.getenv("OPENALGO_OPTION_PRODUCTS", "NRML,MIS").split(",") if p.strip()]
PLACE_PRODUCT   = OPTION_PRODUCTS[0] if OPTION_PRODUCTS else "NRML"

TICK_SIZE_OPT = float(os.getenv("OPENALGO_OPTION_TICK", "0.05"))
AUTO_SELL_MARGIN_OPT = float(os.getenv("AUTO_SELL_MARGIN_OPT", "0.25"))

USE_EXECUTIONS = (os.getenv("USE_EXECUTIONS", "1").strip() in ("1", "true", "yes"))
DEFER_ON_START_SEC = float(os.getenv("DEFER_ON_START_SEC", "2.0"))
LEDGER_LOOKBACK_DAYS = int(os.getenv("LEDGER_LOOKBACK_DAYS", "45"))
LEDGER_USE_SYNTHETIC_BOOTSTRAP = (os.getenv("LEDGER_USE_SYNTHETIC_BOOTSTRAP", "1").strip() in ("1","true","yes"))
DISCREPANCY_PCT = float(os.getenv("DISCREPANCY_PCT", "0.5"))  # percent

OPENALGO_WS_QUIET = os.getenv("OPENALGO_WS_QUIET", "0").strip() in ("1", "true", "yes")

# Limit scope strictly to options on derivative exchanges
EXCHANGES = ["NFO", "BFO"]

client = api(api_key=API_KEY, host=HOST, ws_url=WS_URL)

# ───────────────────────────────── WS logging ─────────────────────────────────
if OPENALGO_WS_QUIET:
    for lname in ("openalgo", "websocket", "websocket-client"):
        logging.getLogger(lname).setLevel(logging.ERROR)

# ───────────────────────────────── Helpers ─────────────────────────────────
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def round_to_tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 2)

def weighted_avg(pairs: List[Tuple[float, float]]) -> float:
    num = sum(q * a for q, a in pairs)
    den = sum(q for q, _ in pairs)
    return (num / den) if den > 0 else 0.0

def is_option_symbol(symbol: str) -> bool:
    s = (symbol or "").upper().strip()
    return s.endswith("CE") or s.endswith("PE")

# ───────────────────────────────── State ─────────────────────────────────
class SymbolState:
    def __init__(self, symbol: str, exchange: str):
        self.symbol = symbol
        self.exchange = exchange
        self.total_qty = 0.0
        self.avg_price = 0.0  # current chosen avg for placement
        self.open_sell_id: Optional[str] = None
        self.open_sell_px: Optional[float] = None
        self.open_sell_qty: Optional[float] = None
        self.tracking = False
        self.manual_override = False
        # Confirmation debounce
        self.armed = False
        # Session engine A
        self.session_id = 0
        self.session_start: Optional[datetime] = None
        self.session_buy_qty = 0.0
        self.session_buy_value = 0.0
        self.last_exec_id = None  # if API provides incremental id
        self.session_populated = False

    def reset_session(self):
        self.session_id += 1
        self.session_start = now_utc()
        self.session_buy_qty = 0.0
        self.session_buy_value = 0.0
        self.last_exec_id = None
        self.armed = True
        self.session_populated = False

    def clear_session(self):
        self.session_start = None
        self.session_buy_qty = 0.0
        self.session_buy_value = 0.0
        self.last_exec_id = None
        self.armed = False

    def session_avg(self) -> Optional[float]:
        return (self.session_buy_value / self.session_buy_qty) if self.session_buy_qty > 0 else None

SYMBOLS: Dict[Tuple[str, str], SymbolState] = {}
STATE_LOCK = threading.Lock()
STOP = {"flag": False}

# Execution de-duplication cache
PROCESSED_EXEC_IDS: Set[str] = set()
MAX_EXEC_ID_CACHE = 10000

# ─────────────────────────────── API wrappers ───────────────────────────────

def try_call(fn_name: str, **kwargs):
    fn = getattr(client, fn_name, None)
    if not fn:
        return None
    try:
        return fn(**kwargs) if kwargs else fn()
    except Exception as e:
        print(f"[WARN] {fn_name} error: {e}")
        return None

# Normalizers to handle providers that return either dict-with-data or raw lists

def norm_positions(resp) -> List[dict]:
    if resp is None:
        return []
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        if resp.get("status") == "success":
            data = resp.get("data")
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for k in ("positions", "data", "holdings"):
                    v = data.get(k)
                    if isinstance(v, list):
                        return v
        # some SDKs just return a dict list under 'positions' directly
        for k in ("positions", "data", "holdings", "orders"):
            v = resp.get(k)
            if isinstance(v, list):
                return v
    return []

def norm_orders(resp) -> List[dict]:
    if resp is None:
        return []
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        if resp.get("status") == "success":
            data = resp.get("data")
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for k in ("orders", "data"):
                    v = data.get(k)
                    if isinstance(v, list):
                        return v
        for k in ("orders", "data"):
            v = resp.get(k)
            if isinstance(v, list):
                return v
    return []

def norm_execs(resp) -> List[dict]:
    if resp is None:
        return []
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        data = resp.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("trades", "orders", "executions", "data"):
                v = data.get(k)
                if isinstance(v, list):
                    return v
        # some APIs return the list at top-level keys
        for k in ("trades", "orders", "executions"):
            v = resp.get(k)
            if isinstance(v, list):
                return v
    return []

# Returns list of dicts with keys: time (datetime), side ('BUY'/'SELL'), qty (float), price (float),
# symbol (str), exchange (str), product (str), exec_id (str or None)

def fetch_executions_lookback(days: int) -> List[dict]:
    """Aggregate executions from multiple sources: tradebook, orderhistory, trades(),
    and as a last resort, COMPLETED orders from orderbook() treated as fills.
    Normalizes to rows with: time, side, qty, price, symbol, exchange, product, exec_id.
    """
    rows: List[dict] = []

    # 1) tradebook
    raw = try_call("tradebook")
    items = norm_execs(raw)
    rows.extend(items)

    # 2) orderhistory (many brokers put fills here)
    raw2 = try_call("orderhistory")
    items2 = norm_execs(raw2)
    rows.extend(items2)

    # 3) trades() or executions() if the SDK exposes them
    for alt in ("trades", "executions"):
        raw3 = try_call(alt)
        items3 = norm_execs(raw3)
        rows.extend(items3)

    # 4) fallback: pull completed orders from orderbook and treat as fills
    raw4 = try_call("orderbook")
    orders = norm_orders(raw4)
    for o in orders:
        status = (o.get("order_status") or o.get("status") or "").upper()
        if status in ("COMPLETE", "COMPLETED", "FILLED", "TRADED", "EXECUTED"):
            # Construct a pseudo execution row
            rows.append({
                "time": o.get("exchange_time") or o.get("transaction_time") or o.get("order_time") or o.get("time") or o.get("updated_at") or o.get("created_at"),
                "side": (o.get("transaction_type") or o.get("side") or o.get("action") or "").upper(),
                "qty": float(o.get("filled_quantity") or o.get("quantity") or o.get("qty") or 0) or 0.0,
                "price": float(o.get("average_price") or o.get("price") or o.get("avg_price") or 0),
                "symbol": (o.get("symbol") or o.get("trading_symbol") or o.get("tradingsymbol") or "").upper().strip(),
                "exchange": (o.get("exchange") or o.get("exch") or "").upper(),
                "product": (o.get("product") or o.get("product_type") or "").upper(),
                "exec_id": str(o.get("exchange_order_id") or o.get("orderid") or o.get("order_id") or "") or None,
            })

    # Now normalize timestamps and filter by lookback
    cutoff = now_utc() - timedelta(days=days)
    normed: List[dict] = []
    for r in rows:
        try:
            exch = (r.get("exchange") or r.get("exch") or "").upper()
            sym  = (r.get("symbol") or r.get("trading_symbol") or r.get("tradingsymbol") or "").upper().strip()
            if not (exch and sym):
                continue
            ts = (
                r.get("time") or r.get("exchange_time") or r.get("transaction_time") or r.get("order_time")
                or r.get("created_at") or r.get("updated_at") or r.get("trade_time") or r.get("timestamp")
            )
            dt = None
            if isinstance(ts, (int, float)):
                tval = float(ts)
                if tval > 1e12:
                    dt = datetime.fromtimestamp(tval / 1000.0, tz=timezone.utc)
                else:
                    dt = datetime.fromtimestamp(tval, tz=timezone.utc)
            elif isinstance(ts, str):
                for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%SZ", "%d-%m-%Y %H:%M:%S"):
                    try:
                        if fmt.endswith("Z"):
                            dt = datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
                        else:
                            dt = datetime.strptime(ts, fmt)
                            if not dt.tzinfo:
                                dt = dt.replace(tzinfo=timezone.utc)
                        break
                    except Exception:
                        pass
            if not dt or dt < cutoff:
                continue
            side = (r.get("transaction_type") or r.get("side") or r.get("action") or "").upper()
            if side not in ("BUY", "SELL"):
                continue
            qty = float(r.get("qty") or r.get("filled_quantity") or r.get("quantity") or 0) or 0.0
            if qty <= 0:
                continue
            price = float(r.get("price") or r.get("average_price") or r.get("avg_price") or 0)
            product = (r.get("product") or r.get("product_type") or "").upper()
            exec_id = str(r.get("exec_id") or r.get("trade_id") or r.get("exchange_order_id") or r.get("orderid") or r.get("order_id") or "") or None

            # de-dup
            if exec_id and exec_id in PROCESSED_EXEC_IDS:
                continue
            if exec_id:
                PROCESSED_EXEC_IDS.add(exec_id)
                if len(PROCESSED_EXEC_IDS) > MAX_EXEC_ID_CACHE:
                    PROCESSED_EXEC_IDS.clear()

            normed.append({
                "time": dt, "side": side, "qty": qty, "price": price,
                "symbol": sym, "exchange": exch, "product": product, "exec_id": exec_id,
            })
        except Exception:
            continue

    normed.sort(key=lambda x: x["time"]) 
    return normed

# ─────────────────────────────── Snapshots ───────────────────────────────

def snapshot_option_positions() -> Dict[Tuple[str, str], Tuple[float, float]]:
    """Return (exchange, symbol) -> (net_long_qty, long_avg_from_positive_legs).
    Ignores shorts and ignores negative legs when computing avg.
    Accepts provider responses that are dicts or raw lists.
    """
    rows: Dict[Tuple[str, str], List[Tuple[float, float]]] = {}
    pos_raw = try_call("positionbook")
    pos_list = norm_positions(pos_raw)
    for p in pos_list:
        try:
            # normalize exchange to upper; some SDKs use 'exch'
            exch = (p.get("exchange") or p.get("exch") or "").upper()
            if exch not in EXCHANGES:
                continue
            product = (p.get("product", "") or "").upper()
            if product not in OPTION_PRODUCTS:
                continue
            sym = (p.get("symbol", "") or "").upper().strip()
            if not is_option_symbol(sym):
                continue
            qty = float(p.get("quantity", 0) or 0)
            if qty == 0:
                continue
            avg = float(p.get("average_price", 0) or 0)
            rows.setdefault((exch, sym), []).append((qty, avg))
        except Exception:
            continue

    merged: Dict[Tuple[str, str], Tuple[float, float]] = {}
    for key, pairs in rows.items():
        net_qty = sum(q for q, _ in pairs)
        if net_qty > 0:  # only track longs
            same_sign_legs = [(q, a) for (q, a) in pairs if q > 0]
            if not same_sign_legs:
                continue
            avg = weighted_avg(same_sign_legs)
            merged[key] = (net_qty, avg)
    return merged


def snapshot_open_orders_options() -> Dict[Tuple[str, str], List[dict]]:
    out: Dict[Tuple[str, str], List[dict]] = {}
    ob_raw = try_call("orderbook")
    orders = norm_orders(ob_raw)
    for o in orders:
        try:
            exch = (o.get("exchange", "") or "").upper()
            if exch not in EXCHANGES:
                continue
            product = (o.get("product", "") or "").upper()
            if product not in OPTION_PRODUCTS:
                continue
            status = (o.get("order_status", "") or o.get("status") or "").lower()
            if status != "open":
                continue
            sym = (o.get("symbol", "") or "").upper().strip()
            if not is_option_symbol(sym):
                continue
            out.setdefault((exch, sym), []).append(o)
        except Exception:
            continue
    return out

# ─────────────────────────────── Ledger Engine (B) ───────────────────────────────

def fifo_ledger_avg(execs: List[dict], exchange: str, symbol: str, opening_qty: float = 0.0, opening_avg: float = 0.0) -> Tuple[float, Optional[float]]:
    """Return (open_qty, open_avg) reconstructed with FIFO pairing.
    Optionally seed with synthetic opening lot if API lookback is limited.
    """
    # seed queue
    queue: List[Tuple[float, float]] = []
    if LEDGER_USE_SYNTHETIC_BOOTSTRAP and opening_qty > 0 and opening_avg > 0:
        queue.append((opening_qty, opening_avg))

    # process executions for this symbol/exchange/products
    for r in execs:
        if r["exchange"] != exchange or r["symbol"] != symbol or r["product"] not in OPTION_PRODUCTS:
            continue
        if r["side"] == "BUY":
            queue.append((r["qty"], r["price"]))
        elif r["side"] == "SELL":
            sell_qty = r["qty"]
            while sell_qty > 0 and queue:
                lot_qty, lot_price = queue[0]
                take = min(sell_qty, lot_qty)
                lot_qty -= take
                sell_qty -= take
                if lot_qty <= 0:
                    queue.pop(0)
                else:
                    queue[0] = (lot_qty, lot_price)
            # if sells exceed queue due to incomplete history, we end up empty

    open_qty = sum(q for q, _ in queue)
    open_avg = weighted_avg(queue) if open_qty > 0 else None
    return open_qty, open_avg

# ─────────────────────────────── Actions ───────────────────────────────

def cancel_existing_sells(exchange: str, symbol: str, open_orders: Dict[Tuple[str, str], List[dict]]):
    for o in open_orders.get((exchange, symbol), []):
        if (o.get("action", "") or "").upper() == "SELL":
            oid = o.get("orderid") or o.get("order_id")
            status = (o.get("order_status", "") or o.get("status") or "").lower()
            # Don't cancel if already filled/cancelled
            if status not in ("open", "pending", "trigger_pending"):
                print(f"[SKIP] Not cancelling {exchange}:{symbol} id={oid} (status={status})")
                continue
            try:
                client.cancelorder(order_id=str(oid))
                print(f"[ACTION] Cancelled SELL {exchange}:{symbol} (order_id={oid})")
                time.sleep(0.1)  # let cancellation propagate
            except Exception as e:
                print(f"[WARN] Cancel failed for {exchange}:{symbol} id={oid}: {e}")


def ensure_one_sell(exchange: str, symbol: str, qty: float, avg_price: float) -> Tuple[Optional[str], Optional[float], Optional[float]]:
    if qty <= 0:
        return (None, None, None)
    px = round_to_tick(avg_price + AUTO_SELL_MARGIN_OPT, TICK_SIZE_OPT)
    try:
        resp = client.placeorder(
            symbol=symbol,
            exchange=exchange,
            action="SELL",
            quantity=int(qty) if float(qty).is_integer() else float(qty),
            price=str(px),
            product=PLACE_PRODUCT,
            price_type="LIMIT",
        )
        oid = (
            resp.get("orderid")
            or resp.get("order_id")
            or resp.get("data", {}).get("orderid")
            or resp.get("data", {}).get("order_id")
        )
        print(f"[ACTION] Placed SELL {exchange}:{symbol} qty={qty} @ {px} (order_id={oid})")
        return (str(oid) if oid else None, px, qty)
    except Exception as e:
        print(f"[ERROR] Failed to place SELL {exchange}:{symbol} qty={qty}: {e}")
        return (None, None, None)

# ─────────────────────────────── WebSocket ───────────────────────────────

def on_tick(data: dict):
    try:
        exch = (data.get("exchange") or "").upper()
        sym  = (data.get("symbol") or "").upper()
        ltp  = data.get("ltp")
        if exch in EXCHANGES and is_option_symbol(sym) and ltp is not None:
            print(f"LTP {exch}:{sym} {ltp}")
    except Exception:
        pass

# ─────────────────────────────── Supervisor ───────────────────────────────

def supervisor():
    last_log_ts = 0
    last_qty: Dict[Tuple[str, str], float] = {}

    while not STOP["flag"]:
        try:
            totals = snapshot_option_positions()  # net long qty + pos-avg from positive legs
            open_orders = snapshot_open_orders_options()

            # Seed new symbols
            with STATE_LOCK:
                for key in totals.keys():
                    if key not in SYMBOLS:
                        exch, sym = key
                        SYMBOLS[key] = SymbolState(sym, exch)

            # Fetch executions once per loop if enabled
            execs: List[dict] = []
            if USE_EXECUTIONS:
                execs = fetch_executions_lookback(LEDGER_LOOKBACK_DAYS)

            # Per-symbol processing
            for key in list(SYMBOLS.keys()):
                exch, sym = key
                qty, pos_avg_pos_legs = totals.get(key, (0.0, 0.0))
                st = SYMBOLS[key]
                prev_qty = last_qty.get(key, 0.0)

                # Session boundaries
                if qty <= 0:
                    # update visible state before printing/continuing
                    st.total_qty = 0.0
                    st.avg_price = 0.0
                    if st.tracking:
                        cancel_existing_sells(exch, sym, open_orders)
                        st.open_sell_id = st.open_sell_px = st.open_sell_qty = None
                        st.manual_override = False
                        st.tracking = False
                        st.clear_session()
                        print(f"[INFO] Flat → stop tracking {exch}:{sym}")
                    last_qty[key] = 0.0
                    continue

                # Start tracking if needed
                if not st.tracking:
                    st.tracking = True
                    st.reset_session()  # new session for 0→>0
                    print(f"[SESSION] Start {exch}:{sym} qty={qty}")

                # Qty increase → extend session, (re)compute avg from executions if available
                if qty > prev_qty:
                    qty_increase = qty - prev_qty
                    st.armed = True
                    print(f"[FLOW] Qty increased {exch}:{sym} +{qty_increase} (session extended)")

                # ── Engine B: Ledger across lookback (bootstrap + carry-forward) ──
                ledger_open_qty, ledger_open_avg = (0.0, None)
                if USE_EXECUTIONS:
                    ledger_open_qty, ledger_open_avg = fifo_ledger_avg(
                        execs, exch, sym,
                        opening_qty=qty if st.session_start is None else 0.0,  # use pos qty only for first loop after restart
                        opening_avg=pos_avg_pos_legs if st.session_start is None else 0.0,
                    )

                # ── Engine A: Session averaging via mini-ledger since session start ──
                session_avg = None
                if USE_EXECUTIONS and st.session_start is not None:
                    # Build a mini-ledger from executions since session start to isolate only the open buys of this session
                    sess_execs = [r for r in execs
                                  if r["exchange"] == exch and r["symbol"] == sym and r["product"] in OPTION_PRODUCTS
                                  and r["time"] >= st.session_start]
                    # FIFO within session window
                    queue: List[Tuple[float, float]] = []
                    for r in sess_execs:
                        if r["side"] == "BUY":
                            queue.append((r["qty"], r["price"]))
                        else:  # SELL
                            sell_qty = r["qty"]
                            while sell_qty > 0 and queue:
                                lot_qty, lot_price = queue[0]
                                take = min(sell_qty, lot_qty)
                                lot_qty -= take
                                sell_qty -= take
                                if lot_qty <= 0:
                                    queue.pop(0)
                                else:
                                    queue[0] = (lot_qty, lot_price)
                    sess_open_qty = sum(q for q, _ in queue)
                    if sess_open_qty > 0:
                        session_avg = weighted_avg(queue)
                        st.session_buy_qty = sess_open_qty  # FIX 1: track session open qty
                        st.session_populated = True
                        print(f"[SESSION] {exch}:{sym} session_open_qty={sess_open_qty} session_avg={session_avg}")


                # Choose avg according to rules
                chosen_avg = None
                source = None
                if session_avg is not None and st.session_buy_qty > 0:
                    coverage_ratio = (st.session_buy_qty / qty) if qty > 0 else 0
                    if coverage_ratio >= 0.95:
                        chosen_avg = session_avg
                        source = "Session"
                    elif ledger_open_avg is not None and ledger_open_qty >= qty * 0.95:
                        chosen_avg = ledger_open_avg
                        source = "Ledger"
                    else:
                        chosen_avg = session_avg
                        source = "Session-Partial"
                        print(f"[WARN] {exch}:{sym} session covers only {coverage_ratio:.1%} of position")
                elif ledger_open_avg is not None and ledger_open_qty >= qty * 0.95:
                    chosen_avg = ledger_open_avg
                    source = "Ledger"
                else:
                    # Defer if too early in session
                    if USE_EXECUTIONS and st.session_start and (now_utc() - st.session_start).total_seconds() < DEFER_ON_START_SEC:
                        print(f"[DEFER] {exch}:{sym} waiting for executions")
                        last_qty[key] = qty
                        continue
                    # Last resort: use position avg but flag as uncertain
                    chosen_avg = pos_avg_pos_legs
                    source = "Position-UNSAFE"
                    print(f"[WARN] {exch}:{sym} using position avg (execution tracking failed!)")

                st.total_qty = qty
                st.avg_price = chosen_avg

                # Decide/maintain order
                target_px = round_to_tick(chosen_avg + AUTO_SELL_MARGIN_OPT, TICK_SIZE_OPT)

                # inspect existing orders
                found_open = False
                matched_target = False
                for o in open_orders.get(key, []):
                    if (o.get("action", "") or "").upper() == "SELL" and float(o.get("quantity", 0) or 0) == qty:
                        found_open = True
                        actual_px = float(o.get("price", 0) or 0)
                        st.open_sell_id = o.get("orderid") or o.get("order_id")
                        st.open_sell_px = actual_px
                        st.open_sell_qty = qty
                        if abs(actual_px - target_px) < 1e-6:
                            st.manual_override = False
                            matched_target = True
                        else:
                            st.manual_override = True
                        break

                if matched_target:
                    st.armed = False
                    print(f"[DECIDE] {exch}:{sym} keeping SELL id={st.open_sell_id} @ {st.open_sell_px} from {source} avg={chosen_avg}")
                elif found_open and st.manual_override:
                    st.armed = False
                    print(f"[DECIDE] {exch}:{sym} user override detected; leaving order as-is (target {target_px}, source {source})")
                else:
                    if st.armed:
                        print(f"[DECIDE] {exch}:{sym} placing SELL (armed) qty={qty} target={target_px} source={source} avg={chosen_avg}; ledger=({ledger_open_qty},{ledger_open_avg}) session={st.session_buy_qty if st.session_start else 0}")
                        oid, px, oq = ensure_one_sell(exch, sym, qty, chosen_avg)
                        st.open_sell_id, st.open_sell_px, st.open_sell_qty = oid, px, oq
                        st.armed = False
                    else:
                        print(f"[DECIDE] {exch}:{sym} enforce/reprice SELL qty={qty} target={target_px} source={source} avg={chosen_avg}; ledger=({ledger_open_qty},{ledger_open_avg}) session={st.session_buy_qty if st.session_start else 0}")
                        cancel_existing_sells(exch, sym, open_orders)
                        st.open_sell_id = st.open_sell_px = st.open_sell_qty = None
                        oid, px, oq = ensure_one_sell(exch, sym, qty, chosen_avg)
                        st.open_sell_id, st.open_sell_px, st.open_sell_qty = oid, px, oq

                last_qty[key] = qty

            # periodic summary
            now = time.time()
            if now - last_log_ts >= 60:
                print("---- SUMMARY (60s) ----")
                for _, st in SYMBOLS.items():
                    print(
                        f"{st.exchange}:{st.symbol} qty={st.total_qty} avg={st.avg_price} "
                        f"sell={'None' if not st.open_sell_id else f'id={st.open_sell_id}@{st.open_sell_px}x{st.open_sell_qty}'} "
                        f"tracking={st.tracking} override={st.manual_override} session_start={st.session_start}"
                    )
                print("-----------------------")
                last_log_ts = now

            time.sleep(3)
        except Exception as e:
            print("[ERROR] supervisor loop:", e)
            time.sleep(3)


# ─────────────────────────────── Main ───────────────────────────────

def main():
    print("🔁 OpenAlgo Python Bot is running.")

    # Seed state
    _ = snapshot_option_positions()

    # Start supervisor thread
    t = threading.Thread(target=supervisor, daemon=True)
    t.start()

    # WS connect + manage subscriptions to tracked symbols
    try:
        client.connect()
    except Exception as e:
        print("[ERROR] WebSocket connect failed:", e)

    subscribed: Set[Tuple[str, str]] = set()

    try:
        while not STOP["flag"]:
            with STATE_LOCK:
                desired = {(st.exchange, st.symbol) for st in SYMBOLS.values() if st.tracking}

            # subscribe new
            to_add = desired - subscribed
            if to_add:
                instruments = [{"exchange": ex, "symbol": sym} for (ex, sym) in to_add]
                try:
                    client.subscribe_ltp(instruments, on_data_received=on_tick)
                except Exception as e:
                    print("[WARN] subscribe_ltp error:", e)
                subscribed |= to_add

            # unsubscribe removed
            to_remove = subscribed - desired
            if to_remove:
                instruments = [{"exchange": ex, "symbol": sym} for (ex, sym) in to_remove]
                try:
                    client.unsubscribe_ltp(instruments)
                except Exception:
                    pass
                subscribed -= to_remove

            time.sleep(10)
    except KeyboardInterrupt:
        pass
    finally:
        if subscribed:
            instruments = [{"exchange": ex, "symbol": sym} for (ex, sym) in subscribed]
            try:
                client.unsubscribe_ltp(instruments)
            except Exception:
                pass
        try:
            client.disconnect()
        except Exception:
            pass
        STOP["flag"] = True
        t.join(timeout=5)
        print("[INFO] Exited cleanly.")


if __name__ == "__main__":
    def _sigint(_sig, _frame):
        STOP["flag"] = True
    signal.signal(signal.SIGINT, _sigint)
    main()
