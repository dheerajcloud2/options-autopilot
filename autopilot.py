"""Autopilot: scanner signals -> risk checks -> paper orders or live proposals.

Paper mode places simulated bracket orders through paper_trading.
Live mode queues proposals that you confirm in the normal order ticket. Only when
AUTOPILOT_LIVE_AUTO=true does it send live OCO orders itself, through the
place_live callback supplied by app.py.
"""
import logging
import math
from datetime import datetime
import threading
import time
from datetime import time as local_time
from decimal import Decimal
from uuid import uuid4

import config
import paper_trading
import trading

_lock = threading.Lock()
_state = {
    "enabled": config.AUTOPILOT_ENABLED,
    "mode": "live" if config.AUTOPILOT_MODE == "live" else "paper",
    "halted_reason": None,
    "last_run": None,
    "last_actions": [],
}
_signal_counts = {}
_proposals = {}
_day = {"date": None, "budget": None, "symbols": set(), "live_spent": 0.0,
        "skipped": set(), "start_value": None}
_seed_pending = True


def _roll_day(today):
    global _seed_pending
    if _day["date"] != today:
        _day.update(date=today, budget=None, symbols=set(), live_spent=0.0, skipped=set(),
                    start_value=None)
        _seed_pending = True


def mark_submitted(option_symbol):
    """Called when a live order for a proposed contract is sent to Schwab."""
    with _lock:
        for proposal_id, proposal in list(_proposals.items()):
            if proposal["option_symbol"] == option_symbol:
                _day["live_spent"] += proposal["max_premium"]
                del _proposals[proposal_id]


def live_mode():
    with _lock:
        return _state["mode"] == "live"


def set_mode(mode):
    """Switch between paper and live. Stops autopilot so it must be restarted."""
    if mode not in ("paper", "live"):
        raise ValueError("Mode must be paper or live.")
    global _seed_pending
    with _lock:
        if _state["mode"] == mode:
            return
        _state["mode"] = mode
        _state["enabled"] = False
        _state["halted_reason"] = None
        _proposals.clear()
        _seed_pending = True


def live_blocker():
    """Why live proposals cannot be created, or None when they can."""
    if not live_mode():
        return None
    if config.AUTOPILOT_LIVE_AUTO and not config.AUTOPILOT_LIVE_AUTO_CONFIRM:
        return "Automatic live orders need AUTOPILOT_LIVE_AUTO_CONFIRM=I_ACCEPT_REAL_MONEY_RISK."
    if not config.has_trading_credentials():
        return "Live autopilot needs the Accounts and Trading app credentials."
    return None


def dismiss_proposal(proposal_id):
    with _lock:
        return _proposals.pop(proposal_id, None) is not None


def _expire_proposals(is_eligible):
    now = time.time()
    with _lock:
        for proposal_id, proposal in list(_proposals.items()):
            if now - proposal["created_at"] > config.AUTOPILOT_PROPOSAL_TTL_SECONDS:
                del _proposals[proposal_id]
    for proposal_id, proposal in list(_proposals.items()):
        if not is_eligible(proposal["option_symbol"]):
            with _lock:
                _proposals.pop(proposal_id, None)


def is_enabled():
    with _lock:
        return _state["enabled"]


def set_enabled(value):
    with _lock:
        _state["enabled"] = bool(value)
        if value:
            _state["halted_reason"] = None
            global _seed_pending
            _seed_pending = True


_WINDOWS = (
    (local_time(8, 30), local_time(9, 30), "8:30-9:30"),
    (local_time(9, 30), local_time(10, 30), "9:30-10:30"),
    (local_time(10, 30), local_time(12, 0), "10:30-12:00"),
    (local_time(12, 0), local_time(23, 59), "12:00-3:00"),
)


def _window_label(entered):
    for start, end, label in _WINDOWS:
        if start <= entered.time() < end:
            return label
    return "other"


def _summarize(trades):
    wins = [t["net"] for t in trades if t["net"] > 0]
    losses = [t["net"] for t in trades if t["net"] <= 0]
    count = len(trades)
    return {
        "trades": count,
        "wins": len(wins),
        "win_rate": round(len(wins) / count * 100, 1) if count else None,
        "net": round(sum(t["net"] for t in trades), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "expectancy": round(sum(t["net"] for t in trades) / count, 2) if count else None,
    }


def _entry_context(contract, quote, trend):
    """Filter values at the moment of entry, stored with the paper order for later analysis."""
    context = {k: v for k, v in contract.items()
               if isinstance(v, (str, int, float, bool)) or v is None}
    context["momentum"] = trend
    for key in ("bid", "ask", "mark", "last", "volume", "iv", "volatility"):
        if quote.get(key) is not None:
            context["quote_" + key] = quote.get(key)
    context["settings"] = {
        "take_profit_pct": config.AUTOPILOT_TAKE_PROFIT_PCT,
        "stop_loss_pct": config.AUTOPILOT_STOP_LOSS_PCT,
    }
    return context


def export_trades():
    """One row per paper autopilot entry (open or closed) with entry context and exit details."""
    orders = paper_trading.raw_orders()
    tz = paper_trading._MARKET_TIMEZONE
    rows = []
    for order in orders:
        if order.get("source") != "autopilot" or order.get("instruction") != "BUY_TO_OPEN":
            continue
        if not order.get("fill_price"):
            continue
        entry = float(order["fill_price"])
        exit_price = exit_time = reason = None
        quantity = int(order.get("filled_quantity") or 0)
        closed = 0
        gross = 0.0
        for child in order.get("related_orders", []):
            if child.get("status") == "FILLED" and child.get("fill_price"):
                q = int(child.get("filled_quantity", 0))
                closed += q
                gross += (float(child["fill_price"]) - entry) * q * 100
                exit_price, reason = float(child["fill_price"]), child["type"]
                exit_time = child.get("filled_time")
        for sell in orders:
            if (sell.get("instruction") == "SELL_TO_CLOSE" and sell.get("status") == "FILLED"
                    and sell.get("origin_order_id") == order["order_id"] and sell.get("fill_price")):
                q = int(sell.get("filled_quantity", 0))
                closed += q
                gross += (float(sell["fill_price"]) - entry) * q * 100
                exit_price, reason, exit_time = float(sell["fill_price"]), "MANUAL_SELL", sell.get("filled_time")
        entered = datetime.fromisoformat(order["entered_time"]).astimezone(tz)
        row = {
            "order_id": order["order_id"],
            "date": entered.date().isoformat(),
            "entered_time": entered.strftime("%H:%M:%S"),
            "window": _window_label(entered),
            "symbol": order["symbol"],
            "underlying": _underlying(order["symbol"]),
            "side": "CALL" if order["symbol"][12:13] == "C" else "PUT",
            "quantity": quantity,
            "entry_price": entry,
            "exit_price": exit_price,
            "exit_reason": reason or ("OPEN" if order.get("open_quantity", 0) > 0 else "EXPIRED"),
            "exit_time": exit_time,
            "gross_pnl": round(gross, 2) if closed else None,
            "net_pnl": round(gross - config.AUTOPILOT_COST_PER_CONTRACT * closed, 2) if closed else None,
        }
        for key, value in (order.get("entry_context") or {}).items():
            if key != "settings":
                row["ctx_" + key] = value
        for key, value in ((order.get("entry_context") or {}).get("settings") or {}).items():
            row["ctx_" + key] = value
        rows.append(row)
    return rows


def export_csv():
    import csv, io
    rows = export_trades()
    columns = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, restval="")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def results():
    """Closed paper autopilot trades: P/L after assumed costs, by window and side."""
    orders = paper_trading.raw_orders()
    by_id = {order["order_id"]: order for order in orders}
    tz = paper_trading._MARKET_TIMEZONE
    cost = config.AUTOPILOT_COST_PER_CONTRACT
    trades, open_count = [], 0
    for order in orders:
        if order.get("source") != "autopilot" or order.get("instruction") != "BUY_TO_OPEN":
            continue
        if not order.get("fill_price"):
            continue
        entry = float(order["fill_price"])
        quantity = int(order.get("filled_quantity") or order.get("quantity") or 0)
        exit_fills = [
            (float(c["fill_price"]), int(c.get("filled_quantity", 0)))
            for c in order.get("related_orders", [])
            if c.get("status") == "FILLED" and c.get("fill_price")]
        exit_fills += [
            (float(o["fill_price"]), int(o.get("filled_quantity", 0))) for o in orders
            if o.get("instruction") == "SELL_TO_CLOSE" and o.get("status") == "FILLED"
            and o.get("origin_order_id") == order["order_id"] and o.get("fill_price")]
        closed_quantity = sum(q for _, q in exit_fills)
        if closed_quantity < 1:
            if order.get("open_quantity", 0) > 0:
                open_count += 1
            continue
        gross = sum((price - entry) * q * 100 for price, q in exit_fills)
        entered = datetime.fromisoformat(order["entered_time"]).astimezone(tz)
        symbol = order["symbol"]
        trades.append({
            "symbol": symbol,
            "date": entered.date().isoformat(),
            "window": _window_label(entered),
            "side": "CALL" if symbol[12:13] == "C" else "PUT",
            "gross": gross,
            "net": gross - cost * closed_quantity,
        })
    trades.sort(key=lambda t: t["date"])
    summary = _summarize(trades)
    summary["gross"] = round(sum(t["gross"] for t in trades), 2)
    summary["costs"] = round(summary["gross"] - summary["net"], 2)
    total_win = sum(t["net"] for t in trades if t["net"] > 0)
    total_loss = -sum(t["net"] for t in trades if t["net"] <= 0)
    summary["profit_factor"] = round(total_win / total_loss, 2) if total_loss else None
    equity = peak = drawdown = 0.0
    days = {}
    for trade in trades:
        equity += trade["net"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        days[trade["date"]] = days.get(trade["date"], 0.0) + trade["net"]
    summary["max_drawdown"] = round(drawdown, 2)
    summary["open_positions"] = open_count
    running, daily = 0.0, []
    for date, pnl in sorted(days.items()):
        running += pnl
        daily.append({"date": date, "net": round(pnl, 2), "cumulative": round(running, 2)})
    return {
        "summary": summary,
        "by_window": {label: _summarize([t for t in trades if t["window"] == label])
                      for _, _, label in _WINDOWS},
        "by_side": {side: _summarize([t for t in trades if t["side"] == side])
                    for side in ("CALL", "PUT")},
        "daily": daily[-30:],
        "cost_per_contract": cost,
    }


def status():
    with _lock:
        snapshot = dict(_state)
        snapshot["last_actions"] = list(_state["last_actions"])
        now = time.time()
        snapshot["proposals"] = [
            dict(p, expires_in=max(0, int(
                config.AUTOPILOT_PROPOSAL_TTL_SECONDS - (now - p["created_at"]))))
            for p in sorted(_proposals.values(), key=lambda p: p["created_at"])
        ]
    snapshot["mode"] = "LIVE" if live_mode() else "PAPER"
    snapshot["live_blocker"] = live_blocker()
    snapshot["live_auto"] = bool(live_mode() and config.AUTOPILOT_LIVE_AUTO)
    snapshot["limits"] = {
        "max_open_positions": config.AUTOPILOT_MAX_OPEN_POSITIONS,
        "risk_per_trade": config.AUTOPILOT_RISK_PER_TRADE,
        "daily_budget_pct": config.AUTOPILOT_DAILY_BUDGET_PCT,
        "daily_budget": _day["budget"],
        "max_contracts": config.AUTOPILOT_MAX_CONTRACTS,
        "max_daily_loss": config.AUTOPILOT_MAX_DAILY_LOSS,
        "daily_profit_target_pct": config.AUTOPILOT_DAILY_PROFIT_TARGET_PCT,
        "exit_plan": "custom",
        "take_profit_pct": config.AUTOPILOT_TAKE_PROFIT_PCT,
        "stop_loss_pct": config.AUTOPILOT_STOP_LOSS_PCT,
        "no_new_entries_after": config.AUTOPILOT_LAST_ENTRY_TIME.strftime("%H:%M"),
    }
    return snapshot


def position_size(entry, stop, budget):
    """Contracts that fit the remaining budget (and the optional per-trade risk cap)."""
    cost = Decimal(entry) * 100
    if cost <= 0:
        return 0
    quantity = min(math.floor(Decimal(str(budget)) / cost), config.AUTOPILOT_MAX_CONTRACTS)
    risk_per_contract = (Decimal(entry) - Decimal(stop)) * 100
    if config.AUTOPILOT_RISK_PER_TRADE > 0 and risk_per_contract > 0:
        quantity = min(quantity, math.floor(
            Decimal(str(config.AUTOPILOT_RISK_PER_TRADE)) / risk_per_contract))
    return max(0, quantity)


def _entry_window_open(now):
    return (now.weekday() < 5
            and local_time(8, 30) <= now.time() < config.AUTOPILOT_LAST_ENTRY_TIME)


def _underlying(option_symbol):
    return option_symbol.split()[0] if option_symbol else ""


def _profit_target_reason(live, live_account_value):
    """Message once today's gain reaches the daily profit target, else None.
    Also enforces the daily loss limit in live mode. Paper uses realized P/L; live uses the account value change since the first
    pass of the day (so it includes open positions)."""
    pct = config.AUTOPILOT_DAILY_PROFIT_TARGET_PCT
    if live:
        value = live_account_value() if live_account_value else None
        if not value:
            return None
        if _day["start_value"] is None:
            _day["start_value"] = value
        base, gain = _day["start_value"], value - _day["start_value"]
    else:
        base, gain = config.AUTOPILOT_PAPER_ACCOUNT_SIZE, _risk_snapshot()[1]
    if live and config.AUTOPILOT_MAX_DAILY_LOSS and gain <= -config.AUTOPILOT_MAX_DAILY_LOSS:
        return (f"Daily loss limit reached (${gain:.2f}). No new entries today.")
    if pct and base and gain >= base * pct / 100:
        return (f"Daily profit target of {pct:g}% reached (+${gain:.2f}). "
                "No new entries today.")
    return None


def _risk_snapshot():
    """Open/working paper entries and today's realized P/L."""
    orders = paper_trading.raw_orders()
    held = [
        order for order in orders
        if order.get("instruction") == "BUY_TO_OPEN"
        and (order.get("status") == "WORKING"
             or (order.get("status") == "FILLED" and order.get("open_quantity", 0) > 0))
    ]
    return held, paper_trading.realized_pnl_today(orders)


_WINDOW_FIELDS = (
    "option_pct_830_to_930",
    "option_pct_930_to_1030",
    "option_pct_1030_to_noon",
    "option_pct_noon_to_3pm",
)


def _window_contracts(per_symbol):
    """Options listed in any of the dashboard window tables (8:30-9:30, 9:30-10:30,
    10:30-12, 12-3): positive in the window, priced under $2, one per underlying
    and direction per table."""
    found = {}
    for outcome in per_symbol.values():
        seen = set()
        for option in outcome.get("weekly_spread") or []:
            price = option.get("current_price")
            if price is None or not 0 < price < 2:
                continue
            for field in _WINDOW_FIELDS:
                change = option.get(field)
                key = (field, option.get("underlying"), option.get("put_call"))
                if change is None or change <= 0 or key in seen:
                    continue
                seen.add(key)
                symbol = option["option_symbol"]
                if symbol not in found or change > found[symbol]["_change"]:
                    found[symbol] = dict(option, _change=change, _ema=(
                        (outcome.get("day_trade_context") or {}).get("stock_ema20_relation")))
    return found


def _contract_quality_ok(contract):
    """Delta, days to expiry and open interest checks from the scanner data.
    Missing data fails a check that is enabled."""
    low, high = config.AUTOPILOT_MIN_ABS_DELTA, config.AUTOPILOT_MAX_ABS_DELTA
    if low or high:
        delta = contract.get("delta")
        try:
            delta = abs(float(delta))
        except (TypeError, ValueError):
            return False
        if delta != delta or (low and delta < low) or (high and delta > high):
            return False
    if config.AUTOPILOT_MIN_DAYS_TO_EXPIRY:
        days = contract.get("days_to_expiration")
        if days is None or days < config.AUTOPILOT_MIN_DAYS_TO_EXPIRY:
            return False
    if config.AUTOPILOT_MIN_OPEN_INTEREST:
        if (contract.get("open_interest") or 0) < config.AUTOPILOT_MIN_OPEN_INTEREST:
            return False
    return True


def _ask_close_to_mid(quote):
    limit = config.AUTOPILOT_MAX_ASK_ABOVE_MID_PCT
    if not limit:
        return True
    try:
        bid, ask = float(quote.get("bid")), float(quote.get("ask"))
    except (TypeError, ValueError):
        return False
    mid = (bid + ask) / 2
    return mid > 0 and ask <= mid * (1 + limit / 100)


def _stable_signals(per_symbol):
    """Window-table options that have been listed for the required number of scans."""
    current = _window_contracts(per_symbol)
    for symbol in list(_signal_counts):
        if symbol not in current:
            del _signal_counts[symbol]
    stable = []
    for symbol, contract in current.items():
        _signal_counts[symbol] = _signal_counts.get(symbol, 0) + 1
        if _signal_counts[symbol] >= config.AUTOPILOT_STABILITY_PASSES:
            stable.append(contract)
    stable.sort(key=lambda c: c["_change"], reverse=True)
    return stable


def _build_plan(quote, budget):
    """Entry, exits and size for a quote, or None when it does not qualify."""
    ask = Decimal(str(quote.get("ask")))
    if not ask.is_finite() or not (
            Decimal(str(config.AUTOPILOT_MIN_PRICE)) <= ask <= trading.MAX_ORDER_PRICE):
        return None
    entry = trading.suggest_bracket_entry_price(ask)
    take_profit, stop, _, _, _ = trading.resolve_bracket_exit_plan(
        entry, "custom", config.AUTOPILOT_TAKE_PROFIT_PCT, config.AUTOPILOT_STOP_LOSS_PCT)
    quantity = position_size(entry, stop, budget)
    if quantity < 1:
        return None
    return trading.validate_bracket_order(quantity, entry, take_profit, stop)


def run_cycle(per_symbol, fetch_quotes, is_eligible, live_position_count=None,
              live_account_value=None, place_live=None, momentum_status=None):
    """One autopilot pass. Returns a list of human-readable actions taken.

    live_position_count: callable returning open live option contracts held,
    used to enforce the position cap in live mode.
    place_live: callable(option_symbol, quantity, entry, take_profit, stop) that
    sends the OCO order to Schwab; used only when AUTOPILOT_LIVE_AUTO is true.
    momentum_status: callable(option_symbol) -> "RISING" etc. With
    AUTOPILOT_REQUIRE_UP an option is bought only while its trend is UP; it is
    rechecked on later passes while it stays listed.
    """
    actions = []
    try:
        if paper_trading.market_open():
            symbols = paper_trading.active_symbols()
            paper_trading.orders(fetch_quotes(symbols) if symbols else {})
        signals = _stable_signals(per_symbol)
        _expire_proposals(is_eligible)

        with _lock:
            enabled = _state["enabled"]
        if not enabled:
            return actions

        blocker = live_blocker()
        if blocker:
            with _lock:
                _state["halted_reason"] = blocker
            return actions

        live = live_mode()
        if live:
            slots = config.AUTOPILOT_MAX_OPEN_POSITIONS - len(_proposals) - (
                live_position_count() if live_position_count else 0)
        else:
            held, realized = _risk_snapshot()
            if realized <= -config.AUTOPILOT_MAX_DAILY_LOSS:
                reason = f"Daily loss limit reached (${realized:.2f}). No new entries today."
                with _lock:
                    _state["halted_reason"] = reason
                return actions
            slots = config.AUTOPILOT_MAX_OPEN_POSITIONS - len(held)
        with _lock:
            _state["halted_reason"] = None

        now = paper_trading.market_now()
        _roll_day(now.date())
        reason = _profit_target_reason(live, live_account_value)
        if reason:
            with _lock:
                _state["halted_reason"] = reason
            return actions
        global _seed_pending
        if _seed_pending:
            # Options already listed when autopilot is switched on are not bought.
            _seed_pending = False
            if config.AUTOPILOT_SKIP_EXISTING:
                _day["skipped"].update(c["option_symbol"] for c in signals)
                return actions
        if not _entry_window_open(now):
            return actions

        if _day["budget"] is None:
            value = (live_account_value() if live and live_account_value
                     else config.AUTOPILOT_PAPER_ACCOUNT_SIZE)
            if not value or value <= 0:
                return actions
            _day["budget"] = value * config.AUTOPILOT_DAILY_BUDGET_PCT / 100
        if live:
            ordered_today = set(_day["symbols"]) | {
                p["option_symbol"] for p in _proposals.values()}
            spent = _day["live_spent"] + sum(p["max_premium"] for p in _proposals.values())
        else:
            todays = paper_trading.entries_today(paper_trading.raw_orders())
            ordered_today = {symbol for symbol, _ in todays}
            spent = sum(premium for _, premium in todays)

        for contract in signals:
            if slots < 1:
                break
            option_symbol = contract["option_symbol"]
            underlying = _underlying(option_symbol)
            if (option_symbol in ordered_today or option_symbol in _day["skipped"]
                    or not is_eligible(option_symbol)):
                continue
            remaining = _day["budget"] - spent
            if remaining < 1:
                break
            if not _contract_quality_ok(contract):
                continue
            if config.AUTOPILOT_REQUIRE_EMA:
                wanted = "ABOVE" if contract.get("put_call") == "CALL" else "BELOW"
                if contract.get("_ema") != wanted:
                    continue
            trend = None
            if config.AUTOPILOT_REQUIRE_UP:
                try:
                    trend = momentum_status(option_symbol) if momentum_status else None
                except Exception as exc:
                    logging.info("Autopilot momentum lookup failed for %s: %s", option_symbol, exc)
                    trend = None
                if trend != "RISING":
                    continue
            quote = fetch_quotes([option_symbol]).get(option_symbol)
            if not quote or not _ask_close_to_mid(quote):
                continue
            try:
                plan = _build_plan(quote, remaining)
                if plan is None:
                    continue
                quantity, entry, take_profit, stop = plan
                if live and config.AUTOPILOT_LIVE_AUTO and place_live:
                    # Marked first so an unconfirmed submission is never retried.
                    _day["symbols"].add(option_symbol)
                    _day["live_spent"] += float(entry * quantity * 100)
                    order_id = place_live(option_symbol, quantity, entry, take_profit, stop)
                    label = f"LIVE {order_id}: BUY {quantity} {option_symbol}"
                elif live:
                    proposal_id = "A-" + uuid4().hex[:8].upper()
                    with _lock:
                        _proposals[proposal_id] = {
                            "id": proposal_id,
                            "created_at": time.time(),
                            "option_symbol": option_symbol,
                            "underlying": underlying,
                            "put_call": contract.get("put_call"),
                            "strike": contract.get("strike"),
                            "expiration": contract.get("expiration"),
                            "quantity": quantity,
                            "entry_price": format(entry, ".2f"),
                            "take_profit_price": format(take_profit, ".2f"),
                            "stop_price": format(stop, ".2f"),
                            "max_loss": float((entry - stop) * quantity * 100),
                            "max_premium": float(entry * quantity * 100),
                        }
                    _day["symbols"].add(option_symbol)
                    label = f"{proposal_id}: PROPOSE BUY {quantity} {option_symbol}"
                else:
                    order_id = paper_trading.create_buy(
                        option_symbol, quantity, entry, take_profit, stop, quote,
                        source="autopilot",
                        context=_entry_context(contract, quote, trend))
                    label = f"{order_id}: BUY {quantity} {option_symbol}"
            except (ValueError, ArithmeticError) as exc:
                logging.info("Autopilot skipped %s: %s", option_symbol, exc)
                continue
            ordered_today.add(option_symbol)
            spent += float(entry * quantity * 100)
            slots -= 1
            actions.append(f"{label} @ {entry} (TP {take_profit} / SL {stop})")
    except Exception:
        logging.exception("Autopilot cycle failed")
        actions.append("Autopilot cycle failed; see the application log.")
    finally:
        from datetime import datetime
        with _lock:
            _state["last_run"] = datetime.now().strftime("%H:%M:%S")
            if actions:
                _state["last_actions"] = (actions + _state["last_actions"])[:20]
    return actions
