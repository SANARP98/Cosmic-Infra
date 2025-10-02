#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stock Tradebook Summarizer (OpenAlgo)

What it does:
- Pulls today's tradebook + current holdings
- Filters to EQUITY only (NSE/BSE, product CNC)
- For every stock symbol prints:
    • # of BUY trades and # of SELL trades from today
    • Today's weighted average BUY price
    • Total quantity bought today
    • Current holdings quantity (includes carried forward)
    • Carried forward quantity (holdings - today's net)
    • Holdings average price (from broker)
    • Whether position is FRESH (all bought today) or PARTIAL (some carried forward)

Notes:
- No DB / file writes. Purely prints to console.
- Requires environment variables: API_KEY, optionally OPENALGO_HOST, OPENALGO_WS
- Install: pip install openalgo python-dotenv
"""

import os
from collections import defaultdict
from typing import Dict, List, Tuple
from dotenv import load_dotenv
from openalgo import api

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

EXCHANGES = {"NSE", "BSE"}
PRODUCT = "CNC"  # Delivery product


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def norm_execs(resp) -> List[dict]:
    """Normalize tradebook/orderhistory-like responses to a list of dicts."""
    if resp is None:
        return []
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        data = resp.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # common nested containers: { data: { trades: [...] }}
            for k in ("trades", "orders", "executions", "data"):
                v = data.get(k)
                if isinstance(v, list):
                    return v
        # some SDKs return at top-level
        for k in ("trades", "orders", "executions"):
            v = resp.get(k)
            if isinstance(v, list):
                return v
    return []


def weighted_avg(pairs: List[Tuple[float, float]]) -> float:
    """pairs: list of (qty, price) => returns weighted average"""
    num = sum(q * p for q, p in pairs)
    den = sum(q for q, _ in pairs)
    return round((num / den), 2) if den > 0 else 0.0


# ──────────────────────────────────────────────────────────────────────────────
# Core logic
# ──────────────────────────────────────────────────────────────────────────────

def summarize_stock_trades(rows: List[dict]) -> Dict[Tuple[str, str], dict]:
    """Aggregate BUY/SELL counts, today's average buy price, and net quantity per stock symbol."""
    agg: Dict[Tuple[str, str], dict] = defaultdict(lambda: {
        "buys": 0,
        "sells": 0,
        "first_side": None,
        "today_buy_qty": 0.0,
        "today_sell_qty": 0.0,
        "today_net_qty": 0.0,
        "buy_trades": [],  # list of (qty, price) for weighted avg
    })

    # Preserve order of first appearance per symbol
    first_seen_index: Dict[Tuple[str, str], int] = {}

    for i, r in enumerate(rows):
        exch = (r.get("exchange") or r.get("exch") or "").upper()
        sym = (r.get("symbol") or r.get("trading_symbol") or r.get("tradingsymbol") or "").upper().strip()
        side = (r.get("transaction_type") or r.get("side") or r.get("action") or "").upper()
        prod = (r.get("product") or "").upper()
        
        qty = float(r.get("qty") or r.get("filled_quantity") or r.get("quantity") or 0) or 0.0
        price = float(r.get("price") or r.get("average_price") or r.get("trade_price") or 0) or 0.0

        # Filter: only NSE/BSE equity (CNC product)
        if exch not in EXCHANGES or prod != PRODUCT:
            continue
        if side not in {"BUY", "SELL"} or qty <= 0:
            continue

        key = (exch, sym)
        if key not in first_seen_index:
            first_seen_index[key] = i
            agg[key]["first_side"] = side

        if side == "BUY":
            agg[key]["buys"] += 1
            agg[key]["today_buy_qty"] += qty
            agg[key]["today_net_qty"] += qty
            agg[key]["buy_trades"].append((qty, price))
        else:
            agg[key]["sells"] += 1
            agg[key]["today_sell_qty"] += qty
            agg[key]["today_net_qty"] -= qty

    return agg


def get_holdings(client) -> Dict[Tuple[str, str], dict]:
    """Return current holdings/positions: (exchange, symbol) -> {qty, avg_price}"""
    holdings = {}
    
    # Try positions first (for intraday CNC stocks)
    try:
        pos = client.positionbook()
        
        if pos.get("status") == "success":
            positions = pos.get("data", [])
            
            for p in positions:
                exch = p.get("exchange", "").upper()
                prod = p.get("product", "").upper()
                
                if exch in EXCHANGES and prod == PRODUCT:
                    sym = p.get("symbol", "").upper().strip()
                    qty = float(p.get("quantity", 0) or 0)
                    avg = float(p.get("average_price", 0) or 0)
                    
                    if qty != 0:  # Keep the sign (negative for sold positions)
                        holdings[(exch, sym)] = {"qty": qty, "avg_price": avg}
                        
    except Exception as e:
        print(f"[WARN] positionbook error: {e}")
    
    # Then try holdings (for carried forward stocks)
    try:
        h = client.holdings()
        
        if h.get("status") == "success":
            data = h.get("data", {})
            
            holdings_list = (
                data.get("holdings") or 
                data if isinstance(data, list) else 
                []
            )
            
            for row in holdings_list:                
                exch = row.get("exchange", "").upper()
                if exch in EXCHANGES:
                    sym = (row.get("trading_symbol") or row.get("symbol", "")).upper().strip()
                    qty = float(row.get("quantity", 0) or 0)
                    avg = float(row.get("average_price") or row.get("avg_price", 0) or 0)
                    
                    if qty > 0:
                        key = (exch, sym)
                        if key in holdings:
                            # Merge: add holding qty to position qty
                            pos_qty = holdings[key]["qty"]
                            pos_avg = holdings[key]["avg_price"]
                            
                            # Weighted average only for positive quantities
                            if pos_qty > 0:
                                total_cost = (pos_qty * pos_avg) + (qty * avg)
                                total_qty = pos_qty + qty
                                holdings[key] = {
                                    "qty": total_qty,
                                    "avg_price": total_cost / total_qty if total_qty > 0 else 0
                                }
                            else:
                                # If position is negative (short), just add the holding qty
                                holdings[key] = {
                                    "qty": pos_qty + qty,
                                    "avg_price": avg if (pos_qty + qty) > 0 else pos_avg
                                }
                        else:
                            holdings[key] = {"qty": qty, "avg_price": avg}
    except Exception as e:
        print(f"[WARN] holdings error: {e}")
    
    return holdings


def main():
    print("🔁 OpenAlgo Stock Tradebook Summarizer")

    load_dotenv()
    API_KEY = os.getenv("API_KEY", "").strip()
    HOST = os.getenv("OPENALGO_HOST", "https://openalgo.rpinj.shop").strip()
    WS_URL = os.getenv("OPENALGO_WS", "wss://openalgows.rpinj.shop").strip()

    if not API_KEY:
        print("[ERROR] API_KEY not set. Export API_KEY or create a .env file.")
        return

    client = api(api_key=API_KEY, host=HOST, ws_url=WS_URL)

    # 1) Pull today's tradebook
    try:
        raw = client.tradebook()
    except Exception as e:
        print(f"[ERROR] tradebook() failed: {e}")
        return

    trades = norm_execs(raw)
    if not trades:
        print("No trades returned by tradebook(). Nothing to summarize.")
        return

    # 2) Summarize today's stock trades
    summary = summarize_stock_trades(trades)

    # 3) Get current holdings
    holdings = get_holdings(client)

    # 4) Merge and analyze
    all_symbols = set(summary.keys()) | set(holdings.keys())

    if not all_symbols:
        print("No stock trades or holdings found.")
        return

    print("\n=== Stock Tradebook & Holdings Summary ===")
    print(f"{'Exchange:Symbol':<30} | {'Buys':<4} {'Sells':<5} | {'Today Avg Buy':<14} | {'Today Net':<10} | {'Current Pos':<11} | {'Carried Fwd':<12} | {'Position Avg':<12} | Status")
    print("-" * 165)

    fresh_count = 0
    partial_count = 0
    sold_count = 0
    short_count = 0

    for key in sorted(all_symbols):
        exch, sym = key
        s = summary.get(key, {
            "buys": 0,
            "sells": 0,
            "today_buy_qty": 0.0,
            "today_sell_qty": 0.0,
            "today_net_qty": 0.0,
            "buy_trades": [],
        })
        
        h = holdings.get(key, {"qty": 0.0, "avg_price": 0.0})

        today_avg_buy = weighted_avg(s["buy_trades"]) if s["buy_trades"] else 0.0
        today_net = s["today_net_qty"]
        current_pos = h["qty"]  # Keep sign: positive = long, negative = short
        position_avg = h["avg_price"]
        
        # Calculate carried forward quantity
        # For sold holdings: if current_pos is negative and today_net is also negative,
        # it means we sold carried-forward holdings
        if current_pos < 0 and today_net < 0:
            # Sold from yesterday's holdings
            carried_fwd = abs(current_pos)  # The quantity we owned yesterday
            status = f"SOLD CF ({carried_fwd:.0f})"
            sold_count += 1
        elif current_pos == 0 and today_net < 0:
            # Sold from yesterday, now squared off
            carried_fwd = abs(today_net)
            status = "SOLD CF (settled)"
            sold_count += 1
        elif current_pos == 0:
            status = "CLOSED"
            carried_fwd = 0
            sold_count += 1
        elif current_pos < 0:
            # Actual short position (sold without owning)
            status = f"SHORT ({abs(current_pos):.0f})"
            carried_fwd = 0
            short_count += 1
        elif carried_fwd <= 0.01 and current_pos > 0:  # Small tolerance for rounding
            status = "FRESH (today)"
            carried_fwd = 0
            fresh_count += 1
        elif carried_fwd > 0.01:
            status = "PARTIAL (CF)"
            partial_count += 1
        else:
            status = "-"
            carried_fwd = 0

        label = f"{exch}:{sym}"
        
        print(f"{label:<30} | {s['buys']:<4} {s['sells']:<5} | "
              f"₹{today_avg_buy:>12.2f} | {today_net:>9.0f} | "
              f"{current_pos:>10.0f} | {carried_fwd:>11.0f} | "
              f"₹{position_avg:>10.2f} | {status}")

    print("-" * 165)
    print(f"Total symbols: {len(all_symbols)}")
    print(f"  • Fresh (all bought today): {fresh_count}")
    print(f"  • Partial (some carried forward): {partial_count}")
    print(f"  • Sold CF (holdings sold today): {sold_count - (len([k for k, v in holdings.items() if v['qty'] == 0 and summary.get(k, {}).get('today_net_qty', 0) == 0]))}")
    print(f"  • Short positions: {short_count}")
    print(f"  • Closed/squared off: {len([k for k, v in holdings.items() if v['qty'] == 0 and summary.get(k, {}).get('today_net_qty', 0) == 0])}")


if __name__ == "__main__":
    main()