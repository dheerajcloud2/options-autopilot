"""Persistent local paper execution for reviewed option orders."""
import json
import os
import sqlite3
import uuid
from datetime import datetime, time as local_time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


PAPER_ACCOUNT_ID = "paper"
PAPER_ACCOUNT_LABEL = "Paper Trading"
_ACTIVE_STATUSES = {"WORKING"}
_MARKET_TIMEZONE = ZoneInfo("America/Chicago")


def _database_path():
    configured = os.getenv("PAPER_TRADING_DB_PATH")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".schwab-options-scanner" / "paper_trading.sqlite3"


def _connect():
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 15000")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS paper_orders "
        "(order_id TEXT PRIMARY KEY, order_json TEXT NOT NULL)"
    )
    return connection


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _market_now():
    return datetime.now(_MARKET_TIMEZONE)


def _market_session_open(now):
    return now.weekday() < 5 and local_time(8, 30) <= now.time() < local_time(15, 0)


def _new_id():
    return "P-" + uuid.uuid4().hex[:12].upper()


def _read_all(connection):
    return [
        json.loads(row["order_json"])
        for row in connection.execute(
            "SELECT order_json FROM paper_orders ORDER BY rowid"
        )
    ]


def _save(connection, order):
    connection.execute(
        "INSERT OR REPLACE INTO paper_orders (order_id, order_json) VALUES (?, ?)",
        (order["order_id"], json.dumps(order, separators=(",", ":"))),
    )


def _find(connection, order_id):
    row = connection.execute(
        "SELECT order_json FROM paper_orders WHERE order_id = ?", (order_id,)
    ).fetchone()
    return json.loads(row["order_json"]) if row else None


def _active_exits(order):
    return [
        exit_order for exit_order in order.get("related_orders", [])
        if exit_order.get("status") == "WORKING"
    ]


def _available_quantity(orders, origin_order, exclude_order_id=None):
    return max(
        0,
        int(origin_order.get("open_quantity", 0))
        - sum(
            max(0, int(order["quantity"]) - int(order["filled_quantity"]))
            for order in orders
            if order.get("instruction") == "SELL_TO_CLOSE"
            and order.get("origin_order_id") == origin_order["order_id"]
            and order.get("status") in _ACTIVE_STATUSES
            and order.get("order_id") != exclude_order_id
        ),
    )


def _valid_price(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _cancel_exits(order):
    for exit_order in order.get("related_orders", []):
        if exit_order.get("status") == "WORKING":
            exit_order["status"] = "CANCELED"


def _process_quotes(connection, quotes):
    orders = _read_all(connection)
    by_id = {order["order_id"]: order for order in orders}
    now = _market_now()
    market_open = _market_session_open(now)
    previously_filled = {
        order["order_id"] for order in orders
        if order.get("instruction") == "BUY_TO_OPEN"
        and order.get("status") == "FILLED"
    }

    for order in orders:
        entered = datetime.fromisoformat(order["entered_time"]).astimezone(_MARKET_TIMEZONE)
        if (order.get("status") == "WORKING"
                and (entered.date() < now.date()
                     or (entered.date() == now.date() and now.time() >= local_time(15, 0)))):
            order["status"] = "EXPIRED"
            for child in order.get("related_orders", []):
                if child.get("status") in {"WORKING", "AWAITING_ENTRY"}:
                    child["status"] = "EXPIRED"
        if order.get("instruction") == "BUY_TO_OPEN" and order.get("open_quantity", 0) > 0:
            for child in order.get("related_orders", []):
                if (child.get("status") == "WORKING"
                        and (entered.date() < now.date()
                             or (entered.date() == now.date()
                                 and now.time() >= local_time(15, 0)))):
                    child["status"] = "EXPIRED"

    for order in orders:
        if not market_open:
            continue
        quote = quotes.get(order.get("symbol")) or {}
        if order.get("status") != "WORKING":
            continue
        if order.get("instruction") == "BUY_TO_OPEN":
            ask = _valid_price(quote.get("ask"))
            if ask is not None and ask <= float(order["price"]):
                order["status"] = "FILLED"
                order["filled_quantity"] = order["quantity"]
                order["fill_price"] = format(ask, ".2f")
                order["filled_time"] = _now()
                order["open_quantity"] = order["quantity"]
                for exit_order in order["related_orders"]:
                    exit_order["status"] = "WORKING"
        elif order.get("instruction") == "SELL_TO_CLOSE":
            bid = _valid_price(quote.get("bid"))
            if bid is None or bid < float(order["price"]):
                continue
            origin = by_id.get(order.get("origin_order_id"))
            if not origin:
                continue
            quantity = min(
                int(order["quantity"]) - int(order["filled_quantity"]),
                _available_quantity(orders, origin, order["order_id"]),
            )
            if quantity < 1:
                order["status"] = "CANCELED"
                continue
            order["status"] = "FILLED"
            order["filled_quantity"] = quantity
            order["fill_price"] = format(float(order["price"]), ".2f")
            order["filled_time"] = _now()
            origin["open_quantity"] = max(0, int(origin["open_quantity"]) - quantity)
            if origin["open_quantity"] == 0:
                _cancel_exits(origin)

    for order in orders:
        if not market_open:
            break
        if (order["order_id"] not in previously_filled
                or order.get("open_quantity", 0) < 1):
            continue
        bid = _valid_price((quotes.get(order.get("symbol")) or {}).get("bid"))
        if bid is None:
            continue
        take_profit = next(
            (child for child in order["related_orders"]
             if child["type"] == "TAKE_PROFIT" and child["status"] == "WORKING"),
            None,
        )
        stop_loss = next(
            (child for child in order["related_orders"]
             if child["type"] == "STOP_LOSS" and child["status"] == "WORKING"),
            None,
        )
        triggered = None
        if take_profit and bid >= float(take_profit["price"]):
            triggered = take_profit
            fill_price = float(take_profit["price"])
        elif stop_loss and bid <= float(stop_loss["stop_price"]):
            triggered = stop_loss
            fill_price = bid
        if triggered:
            triggered["status"] = "FILLED"
            triggered["filled_quantity"] = order["open_quantity"]
            triggered["fill_price"] = format(fill_price, ".2f")
            triggered["filled_time"] = _now()
            order["open_quantity"] = 0
            for child in order["related_orders"]:
                if child is not triggered and child["status"] == "WORKING":
                    child["status"] = "CANCELED"

    for order in orders:
        _save(connection, order)


def active_symbols():
    connection = _connect()
    try:
        orders = _read_all(connection)
        symbols = {
            order["symbol"] for order in orders
            if order.get("status") in _ACTIVE_STATUSES
            or (order.get("instruction") == "BUY_TO_OPEN"
                and order.get("open_quantity", 0) > 0
                and _active_exits(order))
        }
        return sorted(symbols)
    finally:
        connection.close()


def orders(quotes=None):
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        if quotes is not None:
            _process_quotes(connection, quotes)
        stored = _read_all(connection)
        by_id = {order["order_id"]: order for order in stored}
        results = []
        for order in reversed(stored[-50:]):
            origin_id = order.get("origin_order_id")
            origin = by_id.get(origin_id) if origin_id else order
            position = int(origin.get("open_quantity", 0)) if origin else 0
            available = _available_quantity(stored, origin) if origin else 0
            results.append({
                "order_id": order["order_id"],
                "account_id": PAPER_ACCOUNT_ID,
                "account_label": PAPER_ACCOUNT_LABEL,
                "symbol": order["symbol"],
                "instruction": order["instruction"],
                "status": order["status"],
                "cancelable": order["status"] in _ACTIVE_STATUSES,
                "quantity": order["quantity"],
                "filled_quantity": order["filled_quantity"],
                "order_type": order["order_type"],
                "price": order["price"],
                "stop_price": None,
                "entered_time": order["entered_time"],
                "related_orders": order.get("related_orders", []),
                "position_quantity": position if order["instruction"] == "BUY_TO_OPEN" else 0,
                "available_quantity": available if order["instruction"] == "BUY_TO_OPEN" else 0,
                "closeable": (
                    order["instruction"] == "BUY_TO_OPEN"
                    and order["status"] == "FILLED"
                    and available > 0
                ),
                "paper": True,
                "fill_price": order.get("fill_price"),
            })
        connection.commit()
        return results
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_order(order_id):
    connection = _connect()
    try:
        return _find(connection, order_id)
    finally:
        connection.close()


def close_setup(order_id, quote):
    connection = _connect()
    try:
        orders = _read_all(connection)
        origin = next(
            (order for order in orders if order["order_id"] == order_id), None)
        if not origin or origin.get("instruction") != "BUY_TO_OPEN":
            raise ValueError("Select a valid paper buy order to close.")
        if origin.get("status") != "FILLED":
            raise ValueError("The paper entry must fill before it can be closed.")
        available = _available_quantity(orders, origin)
        if available < 1:
            raise ValueError("No unreserved paper contracts remain available to close.")
        bid = _valid_price(quote.get("bid"))
        if bid is None:
            raise ValueError("No valid option bid is available for a paper sell.")
        active_exits = len(_active_exits(origin))
        return {
            "account_label": PAPER_ACCOUNT_LABEL,
            "option_symbol": origin["symbol"],
            "position_quantity": int(origin.get("open_quantity", 0)),
            "available_quantity": available,
            "quantity": min(available, 100),
            "limit_price": bid,
            "quote": quote,
            "active_exit_count": active_exits,
        }
    finally:
        connection.close()


def market_now():
    return _market_now()


def market_open():
    return _market_session_open(_market_now())


def raw_orders():
    connection = _connect()
    try:
        return _read_all(connection)
    finally:
        connection.close()


def realized_pnl_today(orders):
    """Realized P/L in dollars from today's closed paper contracts."""
    today = _market_now().date()

    def entered_today(order):
        return datetime.fromisoformat(order["entered_time"]).astimezone(
            _MARKET_TIMEZONE).date() == today

    by_id = {order["order_id"]: order for order in orders}
    total = 0.0
    for order in orders:
        if order.get("instruction") == "BUY_TO_OPEN" and order.get("fill_price"):
            if not entered_today(order):
                continue
            entry = float(order["fill_price"])
            for child in order.get("related_orders", []):
                if child.get("status") == "FILLED" and child.get("fill_price"):
                    total += (float(child["fill_price"]) - entry) * int(
                        child.get("filled_quantity", 0)) * 100
        elif (order.get("instruction") == "SELL_TO_CLOSE"
                and order.get("status") == "FILLED" and entered_today(order)):
            origin = by_id.get(order.get("origin_order_id"))
            if origin and origin.get("fill_price"):
                total += (float(order["fill_price"]) - float(origin["fill_price"])) * int(
                    order.get("filled_quantity", 0)) * 100
    return total


def entries_today(orders):
    """(option symbol, premium dollars) for each paper entry placed today."""
    today = _market_now().date()
    entries = []
    for order in orders:
        if order.get("instruction") != "BUY_TO_OPEN":
            continue
        if order.get("status") in {"CANCELED", "REJECTED"}:
            continue
        placed = datetime.fromisoformat(order["entered_time"]).astimezone(
            _MARKET_TIMEZONE).date()
        if placed == today:
            entries.append((order["symbol"],
                            float(order["price"]) * int(order["quantity"]) * 100))
    return entries


def create_buy(symbol, quantity, price, take_profit, stop_price, quote, source="manual",
               context=None):
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        order = {
            "order_id": _new_id(),
            "source": source,
            "account_id": PAPER_ACCOUNT_ID,
            "account_label": PAPER_ACCOUNT_LABEL,
            "symbol": symbol,
            "instruction": "BUY_TO_OPEN",
            "status": "WORKING",
            "quantity": quantity,
            "filled_quantity": 0,
            "open_quantity": 0,
            "order_type": "LIMIT",
            "price": format(float(price), ".2f"),
            "entered_time": _now(),
            "entry_context": context or {},
            "related_orders": [
                {
                    "type": "TAKE_PROFIT",
                    "status": "AWAITING_ENTRY",
                    "price": format(float(take_profit), ".2f"),
                    "filled_quantity": 0,
                },
                {
                    "type": "STOP_LOSS",
                    "status": "AWAITING_ENTRY",
                    "stop_price": format(float(stop_price), ".2f"),
                    "filled_quantity": 0,
                },
            ],
        }
        _save(connection, order)
        _process_quotes(connection, {symbol: quote})
        connection.commit()
        return order["order_id"]
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def create_sell(origin_order_id, quantity, price, quote):
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        orders = _read_all(connection)
        origin = next(
            (order for order in orders if order["order_id"] == origin_order_id), None)
        if not origin or origin.get("instruction") != "BUY_TO_OPEN":
            raise ValueError("This paper position is no longer available.")
        available = _available_quantity(orders, origin)
        if quantity > available:
            raise ValueError("The available paper position changed; review the sell again.")
        _cancel_exits(origin)
        sell = {
            "order_id": _new_id(),
            "account_id": PAPER_ACCOUNT_ID,
            "account_label": PAPER_ACCOUNT_LABEL,
            "symbol": origin["symbol"],
            "instruction": "SELL_TO_CLOSE",
            "origin_order_id": origin_order_id,
            "status": "WORKING",
            "quantity": quantity,
            "filled_quantity": 0,
            "order_type": "LIMIT",
            "price": format(float(price), ".2f"),
            "entered_time": _now(),
            "related_orders": [],
        }
        _save(connection, origin)
        _save(connection, sell)
        _process_quotes(connection, {origin["symbol"]: quote})
        connection.commit()
        return sell["order_id"]
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def cancel(order_id):
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        order = _find(connection, order_id)
        if not order:
            raise LookupError("This paper order is no longer available.")
        if order.get("status") not in _ACTIVE_STATUSES:
            raise ValueError("This paper order is no longer working and cannot be canceled.")
        order["status"] = "CANCELED"
        order["canceled_time"] = _now()
        if order.get("instruction") == "BUY_TO_OPEN":
            _cancel_exits(order)
        _save(connection, order)
        connection.commit()
        return order
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
