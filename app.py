"""
Schwab Options Scanner - Web Dashboard
========================================

Runs the scanner in the background and serves a live-updating page in your
browser at http://127.0.0.1:5000

Usage:
    python app.py
    python app.py --watchlist my_tickers.txt --interval 10
    python app.py --weekly-universe    # scan every Schwab weekly-optionable stock instead
"""
import argparse
import asyncio
import copy
import datetime as dt
import logging
import math
import threading
import time
from uuid import uuid4

from flask import Flask, Response, jsonify, render_template, request
from schwab.streaming import StreamClient

import config
import scanner_core as sc
import email_notify
from auth import get_client, get_trading_client
import trading
import paper_trading
import autopilot
app = Flask(__name__)

state_lock = threading.Lock()
state = {
    "per_symbol": {},       # symbol -> scan_symbol() output
    "updated_at": None,
    "interval": 10,
    "error": None,
    "symbol_count": 0,      # size of the weekly-options universe being scanned
    "universe_progress": None,  # {"checked": int, "total": int, "found": int} while building, else None
    "weekly_universe": False,
    "scan_progress": None,  # {"completed": int, "total": int, "running": bool}
}

# Shared Schwab client, set once the background thread creates it, so the
# /api/history route (used by the click-to-chart feature) can reuse the
# same authenticated session instead of logging in again.
_client_holder = {"client": None}
_trading_client_holder = {"client": None}
_trading_client_lock = threading.Lock()
_stream_state = {"loop": None, "client": None, "ready": False, "error": None}
_stream_quotes = {}
_stream_lock = threading.Lock()
_stream_subscriptions = set()
_equity_stream_subscriptions = set()
_stream_history = {}
_STREAM_SIGNAL_SECONDS = 10 * 60
_option_momentum_cache = {}
_option_momentum_lock = threading.Lock()
_OPTION_MOMENTUM_CACHE_SECONDS = 60

# Tracks which contracts were qualifying last pass, so we can email only
# when something NEW appears (not every refresh for the same one).
_previously_qualifying = set()
_previously_ema = {"calls": None, "puts": None}
_EMAIL_STABILITY_PASSES = 2
_pending_email_contracts = {}
_email_sent_contracts = set()
_order_confirmation_lock = threading.Lock()
_pending_order_confirmations = {}
_ORDER_CONFIRMATION_TTL_SECONDS = 120
_OPEN_ORDER_LOOKUP_MAX_RESULTS = 3000
_CANCELLABLE_ORDER_STATUSES = {
    "ACCEPTED",
    "AWAITING_CONDITION",
    "AWAITING_PARENT_ORDER",
    "AWAITING_RELEASE_TIME",
    "AWAITING_STOP_CONDITION",
    "AWAITING_UR_OUT",
    "NEW",
    "PENDING_ACKNOWLEDGEMENT",
    "PENDING_ACTIVATION",
    "PENDING_RECALL",
    "QUEUED",
    "WORKING",
}


class OrderPreflightError(RuntimeError):
    pass


def _current_order_candidate(option_symbol: str):
    with state_lock:
        for outcome in state["per_symbol"].values():
            contracts = [outcome.get("call"), outcome.get("put")]
            contracts.extend(outcome.get("weekly_spread", []) or [])
            for contract in contracts:
                if contract and contract.get("option_symbol") == option_symbol:
                    candidate = copy.deepcopy(contract)
                    break
            else:
                continue
            break
        else:
            return None

    expiration = str(candidate.get("expiration") or "").split("T")[0]
    try:
        expiration_date = dt.date.fromisoformat(expiration)
    except ValueError:
        return None
    if expiration_date <= dt.datetime.now(sc.tz()).date():
        return None
    if candidate.get("put_call") not in ("CALL", "PUT"):
        return None
    return candidate


def _trading_accounts(client):
    response = client.get_account_numbers()
    if response.status_code != 200:
        if response.status_code == 401:
            raise RuntimeError(
                "Schwab denied account access (HTTP 401). Confirm the Accounts and Trading "
                "Production app is approved and its credentials are set in "
                "SCHWAB_TRADING_APP_KEY and SCHWAB_TRADING_APP_SECRET. Then move its token "
                f"file ({config.TRADING_TOKEN_PATH}) aside and sign in again. No order was submitted."
            )
        raise RuntimeError(f"Schwab account lookup failed (HTTP {response.status_code}).")
    accounts = response.json()
    if not isinstance(accounts, list):
        raise RuntimeError("Schwab returned an unexpected account list.")
    return accounts


def _account_label(account):
    number = str(account.get("accountNumber") or "")
    if not number:
        return "Account"
    return f"Account ending {number[-4:]}"


def _fetch_option_quote(client, option_symbol):
    response = client.get_quotes([option_symbol])
    if response.status_code != 200:
        raise RuntimeError(f"Schwab quote lookup failed (HTTP {response.status_code}).")
    quotes = response.json()
    entry = quotes.get(option_symbol) or quotes.get(option_symbol.strip())
    quote = (entry or {}).get("quote") or {}
    if not quote:
        raise RuntimeError("Schwab did not return a current quote for this contract.")
    return {
        "bid": quote.get("bidPrice"),
        "ask": quote.get("askPrice"),
        "last": quote.get("lastPrice"),
        "mark": quote.get("mark"),
    }


def _fetch_option_quotes(client, option_symbols):
    if not option_symbols:
        return {}
    response = client.get_quotes(option_symbols)
    if response.status_code != 200:
        raise RuntimeError(f"Schwab quote lookup failed (HTTP {response.status_code}).")
    quotes = response.json()
    if not isinstance(quotes, dict):
        raise RuntimeError("Schwab returned an unexpected option-quote response.")
    result = {}
    for symbol in option_symbols:
        entry = quotes.get(symbol) or quotes.get(symbol.strip()) or {}
        quote = entry.get("quote") or {}
        if quote:
            result[symbol] = {
                "bid": quote.get("bidPrice"),
                "ask": quote.get("askPrice"),
                "last": quote.get("lastPrice"),
                "mark": quote.get("mark"),
            }
    return result


def _resolve_account(client, account_id):
    if not isinstance(account_id, str) or not account_id.isdigit():
        raise ValueError("Select a Schwab account.")
    accounts = _trading_accounts(client)
    index = int(account_id)
    if index >= len(accounts):
        raise ValueError("The selected Schwab account is no longer available. Reload the ticket.")
    account = accounts[index]
    account_hash = account.get("hashValue")
    if not account_hash:
        raise RuntimeError("Schwab did not return an account authorization value.")
    return account_hash, _account_label(account)


def _resolve_order_account(client, account_id):
    if account_id == paper_trading.PAPER_ACCOUNT_ID:
        return paper_trading.PAPER_ACCOUNT_ID, paper_trading.PAPER_ACCOUNT_LABEL
    if config.PAPER_TRADING:
        raise ValueError(
            "The order is set to Paper, but its selected account is not Paper Trading. "
            "Reload the ticket and choose Paper Trading, or select Live trading."
        )
    return _resolve_account(client, account_id)


def _order_related_legs(order):
    related = []
    for child in order.get("childOrderStrategies") or []:
        if not isinstance(child, dict):
            continue
        related.append({
            "type": child.get("orderType"),
            "status": child.get("status"),
            "price": child.get("price"),
            "stop_price": child.get("stopPrice"),
        })
        related.extend(_order_related_legs(child))
    return related


def _order_option_symbols(order, instruction=None):
    symbols = set()
    for leg in order.get("orderLegCollection") or []:
        if not isinstance(leg, dict):
            continue
        if instruction is not None and leg.get("instruction") != instruction:
            continue
        instrument = leg.get("instrument") or {}
        symbol = instrument.get("symbol")
        if symbol and instrument.get("assetType") == "OPTION":
            symbols.add(str(symbol))
    return symbols


def _account_option_positions(account_data):
    account = account_data.get("securitiesAccount") or account_data
    positions = account.get("positions") or []
    quantities = {}
    for position in positions:
        if not isinstance(position, dict):
            continue
        instrument = position.get("instrument") or {}
        symbol = instrument.get("symbol")
        if not symbol or instrument.get("assetType") != "OPTION":
            continue
        try:
            long_quantity = float(position.get("longQuantity") or 0)
            short_quantity = float(position.get("shortQuantity") or 0)
        except (TypeError, ValueError):
            continue
        quantity = max(0, int(long_quantity - short_quantity))
        if quantity:
            quantities[str(symbol)] = quantity
    return quantities


def _open_single_sell_quantity(orders, option_symbol, exclude_order_id=None):
    reserved = 0
    for order in orders:
        if not isinstance(order, dict):
            continue
        if (exclude_order_id is not None
                and str(order.get("orderId")) == str(exclude_order_id)):
            continue
        if (str(order.get("status") or "").upper() not in _CANCELLABLE_ORDER_STATUSES
                or order.get("orderStrategyType") != "SINGLE"
                or option_symbol not in _order_option_symbols(order, "SELL_TO_CLOSE")):
            continue
        try:
            total = float(order.get("quantity") or 0)
            filled = float(order.get("filledQuantity") or 0)
        except (TypeError, ValueError):
            continue
        reserved += max(0, int(total - filled))
    return reserved


def _available_to_sell_quantity(client, account_hash, option_symbol, position_quantity,
                                exclude_order_id=None):
    response = client.get_orders_for_account(
        account_hash, max_results=_OPEN_ORDER_LOOKUP_MAX_RESULTS)
    if response.status_code != 200:
        raise RuntimeError(
            f"Schwab open-order lookup failed (HTTP {response.status_code}); "
            "cannot safely calculate available contracts to sell."
        )
    orders = response.json()
    if not isinstance(orders, list):
        raise RuntimeError("Schwab returned an unexpected open-order list.")
    reserved = _open_single_sell_quantity(
        orders, option_symbol, exclude_order_id=exclude_order_id)
    return max(0, position_quantity - reserved)


def _live_option_position_quantity(client, account_hash, option_symbol):
    positions = _fetch_account_option_positions(client, account_hash)
    return positions.get(option_symbol, 0)


def _fetch_account_option_positions(client, account_hash):
    response = client.get_account(account_hash, fields=[client.Account.Fields.POSITIONS])
    if response.status_code == 401:
        raise RuntimeError(
            "Schwab denied position access (HTTP 401). Reauthorize the Accounts "
            "and Trading app, then refresh the order ticket."
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"Schwab position lookup failed (HTTP {response.status_code}).")
    account_data = response.json()
    if not isinstance(account_data, dict):
        raise RuntimeError("Schwab returned an unexpected account positions response.")
    return _account_option_positions(account_data)


def _active_sell_exit_orders(order):
    exits = []
    for child in order.get("childOrderStrategies") or []:
        if not isinstance(child, dict):
            continue
        child_legs = child.get("orderLegCollection") or []
        is_sell_to_close = any(
            isinstance(leg, dict) and leg.get("instruction") == "SELL_TO_CLOSE"
            for leg in child_legs
        )
        if child.get("childOrderStrategies"):
            exits.extend(_active_sell_exit_orders(child))
        elif is_sell_to_close:
            status = str(child.get("status") or "UNKNOWN").upper()
            if status in _CANCELLABLE_ORDER_STATUSES:
                exits.append({
                    "order_id": str(child["orderId"]) if child.get("orderId") is not None else None,
                    "status": status,
                })
    return exits


def _get_schwab_order(client, order_id, account_hash):
    response = client.get_order(order_id, account_hash)
    if response.status_code == 401:
        raise RuntimeError(
            "Schwab denied order access (HTTP 401). Reauthorize the Accounts "
            "and Trading app before proceeding."
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"Schwab order lookup failed (HTTP {response.status_code}).")
    order = response.json()
    if not isinstance(order, dict) or str(order.get("orderId")) != str(order_id):
        raise RuntimeError("Schwab returned an unexpected order response.")
    return order


def _validate_filled_buy_order(order, option_symbol=None):
    if str(order.get("status") or "").upper() not in {
            "FILLED", "CANCELED", "EXPIRED"}:
        raise ValueError(
            "Only a filled or canceled option buy order can be closed from this action.")
    symbols = _order_option_symbols(order, "BUY_TO_OPEN")
    if option_symbol is not None and option_symbol not in symbols:
        raise ValueError("This order is not a buy-to-open for the selected option.")
    if len(symbols) != 1:
        raise ValueError("This order does not identify one unambiguous option contract.")
    try:
        filled_quantity = int(float(order.get("filledQuantity") or 0))
    except (TypeError, ValueError):
        filled_quantity = 0
    if filled_quantity < 1:
        raise ValueError("Schwab reports no filled contracts to close.")
    return next(iter(symbols)), filled_quantity


def _close_order_details(trading_client, market_data_client, order_id, account_id):
    account_hash, account_label = _resolve_account(trading_client, account_id)
    order = _get_schwab_order(trading_client, order_id, account_hash)
    option_symbol, filled_quantity = _validate_filled_buy_order(order)
    position_quantity = _live_option_position_quantity(
        trading_client, account_hash, option_symbol)
    available_quantity = _available_to_sell_quantity(
        trading_client, account_hash, option_symbol, position_quantity,
        exclude_order_id=order_id)
    quantity = min(filled_quantity, available_quantity, trading.MAX_ORDER_QUANTITY)
    if quantity < 1:
        raise ValueError(
            "No unreserved long contracts remain available to close in this Schwab account.")
    quote = _fetch_option_quote(market_data_client, option_symbol)
    limit_price = trading.select_sell_limit_reference(quote)
    if limit_price is None:
        raise RuntimeError(
            "Schwab did not return a usable option price for a sell limit.")
    return {
        "account_hash": account_hash,
        "account_label": account_label,
        "order": order,
        "option_symbol": option_symbol,
        "position_quantity": position_quantity,
        "available_quantity": available_quantity,
        "quantity": quantity,
        "quote": quote,
        "limit_price": limit_price,
        "active_exit_count": len(_active_sell_exit_orders(order)),
    }


def _cancel_and_confirm_active_exits(client, order, account_hash):
    exits = _active_sell_exit_orders(order)
    for exit_order in exits:
        exit_order_id = exit_order.get("order_id")
        if not exit_order_id:
            raise OrderPreflightError(
                "Schwab reports an active attached exit without an order ID. "
                "No sell was submitted; review the OCO legs in Schwab."
            )
        response = client.cancel_order(exit_order_id, account_hash)
        if not 200 <= response.status_code < 300:
            raise OrderPreflightError(
                f"Schwab did not accept cancellation of attached exit "
                f"{exit_order_id} (HTTP {response.status_code}). No sell was submitted."
            )
        for _ in range(10):
            time.sleep(0.5)
            current_exit = _get_schwab_order(client, exit_order_id, account_hash)
            status = str(current_exit.get("status") or "UNKNOWN").upper()
            if status in {"CANCELED", "EXPIRED", "REJECTED"}:
                break
            if status == "FILLED":
                raise OrderPreflightError(
                    "An attached exit filled while cancellation was pending. "
                    "No additional sell was submitted; refresh the position."
                )
        else:
            raise OrderPreflightError(
                f"Schwab has not confirmed cancellation of attached exit "
                f"{exit_order_id}. No sell was submitted; refresh and check the order."
            )


def _order_monitor_entry(order, account_id, account_label, positions,
                         available_to_sell):
    legs = order.get("orderLegCollection") or []
    symbols = []
    instructions = []
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        instrument = leg.get("instrument") or {}
        symbol = instrument.get("symbol") or instrument.get("description")
        if symbol and symbol not in symbols:
            symbols.append(str(symbol))
        instruction = leg.get("instruction")
        if instruction and instruction not in instructions:
            instructions.append(str(instruction))

    order_id = order.get("orderId")
    status = str(order.get("status") or "UNKNOWN").upper()
    buy_to_open_symbols = _order_option_symbols(order, "BUY_TO_OPEN")
    position_quantity = max(
        (positions.get(symbol, 0) for symbol in buy_to_open_symbols),
        default=0,
    )
    available_quantity = max(
        (available_to_sell.get(symbol, 0) for symbol in buy_to_open_symbols),
        default=0,
    )
    try:
        filled_quantity = float(order.get("filledQuantity") or 0)
    except (TypeError, ValueError):
        filled_quantity = 0
    return {
        "order_id": str(order_id) if order_id is not None else None,
        "account_id": str(account_id),
        "account_label": account_label,
        "symbol": ", ".join(symbols) or "Option order",
        "instruction": ", ".join(instructions),
        "status": status,
        "cancelable": status in _CANCELLABLE_ORDER_STATUSES and order_id is not None,
        "quantity": order.get("quantity"),
        "filled_quantity": order.get("filledQuantity"),
        "order_type": order.get("orderType"),
        "price": order.get("price"),
        "stop_price": order.get("stopPrice"),
        "entered_time": order.get("enteredTime"),
        "related_orders": _order_related_legs(order),
        "position_quantity": position_quantity,
        "available_quantity": available_quantity,
        "closeable": (
            status in {"FILLED", "CANCELED", "EXPIRED"}
            and filled_quantity > 0
            and available_quantity > 0
            and bool(buy_to_open_symbols)
        ),
    }


def _get_trading_client():
    with _trading_client_lock:
        if _trading_client_holder["client"] is None:
            _trading_client_holder["client"] = get_trading_client()
            _start_stream(_trading_client_holder["client"])
        return _trading_client_holder["client"]


@app.get("/api/trading/setup")
def trading_setup():
    option_symbol = request.args.get("symbol", "")
    candidate = _current_order_candidate(option_symbol)
    if not candidate:
        return jsonify({"error": "This contract is no longer eligible. Select a current scanner row."}), 409

    market_data_client = _client_holder["client"]
    if market_data_client is None:
        return jsonify({"error": "Market data is not ready yet. Try again after the first scan."}), 503

    try:
        accounts = [{"id": paper_trading.PAPER_ACCOUNT_ID,
                     "label": paper_trading.PAPER_ACCOUNT_LABEL,
                     "mode": "PAPER"}]
        if config.has_trading_credentials():
            try:
                trading_client = _get_trading_client()
                accounts.extend({"id": str(index), "label": _account_label(account),
                                 "mode": "LIVE"}
                                for index, account in enumerate(_trading_accounts(trading_client))
                                if account.get("hashValue"))
            except Exception:
                logging.exception("Unable to load live Schwab accounts for the order ticket")
        quote = _fetch_option_quote(market_data_client, option_symbol)
        reference_price = trading.select_bracket_entry_reference(
            quote, candidate.get("current_price"))
        entry_price = trading.suggest_bracket_entry_price(reference_price)
        take_profit, stop_price = trading.suggest_bracket_exit_prices(entry_price)
    except (RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception:
        logging.exception("Unable to prepare the option order ticket")
        return jsonify({"error": "The order ticket could not be prepared. Check the terminal and try again."}), 502

    return jsonify({
        "candidate": {
            "underlying": candidate.get("underlying"),
            "option_symbol": option_symbol,
            "put_call": candidate.get("put_call"),
            "strike": candidate.get("strike"),
            "expiration": candidate.get("expiration"),
            "current_price": candidate.get("current_price"),
        },
        "quote": quote,
        "order_defaults": {
            "entry_price": format(entry_price, ".2f"),
            "take_profit_price": format(take_profit, ".2f"),
            "stop_price": format(stop_price, ".2f"),
            "take_profit_pct": 30,
            "stop_loss_pct": 20,
        },
        "oco_presets": {
            key: {
                "label": preset["label"],
                "take_profit_pct": int(preset["take_profit_pct"]),
                "stop_loss_pct": int(preset["stop_loss_pct"]),
            }
            for key, preset in trading.OCO_EXIT_PRESETS.items()
        },
        "accounts": accounts,
        "trading_mode": "PAPER",
    })


@app.post("/api/trading/prepare")
def prepare_option_order():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Provide the order details as JSON."}), 400

    option_symbol = data.get("option_symbol")
    candidate = _current_order_candidate(option_symbol) if isinstance(option_symbol, str) else None
    if not candidate:
        return jsonify({"error": "This contract is no longer eligible. Select a current scanner row."}), 409

    quantity = data.get("quantity")
    limit_price = data.get("limit_price")
    try:
        quantity, price = trading.validate_order(quantity, limit_price)
        take_profit, stop_price, take_profit_pct, stop_loss_pct, exit_plan = (
            trading.resolve_bracket_exit_plan(
                price,
                data.get("exit_plan"),
                data.get("custom_take_profit_pct"),
                data.get("custom_stop_loss_pct"),
            )
        )
        quantity, price, take_profit, stop_price = trading.validate_bracket_order(
            quantity, price, take_profit, stop_price)
        market_data_client = _client_holder["client"]
        if market_data_client is None:
            return jsonify({"error": "Market data is not ready yet. Try again after the first scan."}), 503
        requested_mode = data.get("trading_mode", "PAPER")
        if requested_mode not in {"PAPER", "LIVE"}:
            raise ValueError("Choose Paper or Live trading.")
        is_paper = requested_mode == "PAPER"
        if is_paper:
            account_hash, account_label = _resolve_order_account(
                None, data.get("account_id"))
        else:
            if not config.has_trading_credentials():
                raise ValueError("Configure Schwab Accounts and Trading credentials to enable live orders.")
            trading_client = _get_trading_client()
            account_hash, account_label = _resolve_account(trading_client, data.get("account_id"))
        quote = _fetch_option_quote(market_data_client, option_symbol)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception:
        logging.exception("Unable to prepare an option order")
        return jsonify({"error": "The order could not be prepared. Check the terminal and try again."}), 502

    token = uuid4().hex
    now = time.time()
    with _order_confirmation_lock:
        expired = [
            key for key, value in _pending_order_confirmations.items()
            if now - value["created_at"] > _ORDER_CONFIRMATION_TTL_SECONDS
        ]
        for key in expired:
            del _pending_order_confirmations[key]
        _pending_order_confirmations[token] = {
            "created_at": now,
            "account_hash": account_hash,
            "option_symbol": option_symbol,
            "quantity": quantity,
            "limit_price": format(price, ".2f"),
            "take_profit_price": format(take_profit, ".2f"),
            "stop_price": format(stop_price, ".2f"),
            "take_profit_pct": format(take_profit_pct, "f"),
            "stop_loss_pct": format(stop_loss_pct, "f"),
            "exit_plan": exit_plan,
            "paper": is_paper,
        }

    return jsonify({
        "confirmation_token": token,
        "action": "BUY_TO_OPEN",
        "account": account_label,
        "underlying": candidate.get("underlying"),
        "put_call": candidate.get("put_call"),
        "strike": candidate.get("strike"),
        "expiration": candidate.get("expiration"),
        "quantity": quantity,
        "limit_price": format(price, ".2f"),
        "take_profit_price": format(take_profit, ".2f"),
        "stop_price": format(stop_price, ".2f"),
        "take_profit_pct": format(take_profit_pct, "f"),
        "stop_loss_pct": format(stop_loss_pct, "f"),
        "exit_plan": exit_plan,
        "max_premium": float(price * quantity * 100),
        "quote": quote,
        "duration": "DAY",
        "session": "NORMAL",
        "trading_mode": requested_mode,
    })


@app.get("/api/trading/close/setup")
def setup_option_close():
    order_id = request.args.get("origin_order_id")
    account_id = request.args.get("account_id")
    valid_order_id = (
        isinstance(order_id, str)
        and (order_id.startswith("P-") or order_id.isdigit())
    )
    if not valid_order_id:
        return jsonify({"error": "Select a valid filled order to close."}), 400

    market_data_client = _client_holder["client"]
    if market_data_client is None:
        return jsonify({"error": "Market data is not ready yet."}), 503

    try:
        if order_id.startswith("P-"):
            _resolve_order_account(None, account_id)
            paper_order = paper_trading.get_order(order_id)
            if not paper_order:
                raise ValueError("This paper order is no longer available.")
            quote = _fetch_option_quote(market_data_client, paper_order["symbol"])
            details = paper_trading.close_setup(order_id, quote)
            details["order"] = paper_order
        else:
            trading_client = _get_trading_client()
            details = _close_order_details(
                trading_client, market_data_client, order_id, account_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception:
        logging.exception("Unable to prepare a sell-to-close order")
        return jsonify({
            "The sell-to-close ticket could not be prepared. Check the terminal and try again."
        }), 502

    order = details["order"]
    return jsonify({
        "origin_order_id": order_id,
        "account": details["account_label"],
        "option_symbol": details["option_symbol"],
        "quantity": details["quantity"],
        "position_quantity": details["position_quantity"],
        "available_quantity": details["available_quantity"],
        "limit_price": format(details["limit_price"], ".2f"),
        "quote": details["quote"],
        "active_exit_count": details["active_exit_count"],
        "duration": "DAY",
        "session": "NORMAL",
    })


@app.post("/api/trading/close/prepare")
def prepare_option_close():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Provide the close-order details as JSON."}), 400
    order_id = data.get("origin_order_id")
    valid_order_id = (
        isinstance(order_id, str)
        and (order_id.startswith("P-") or order_id.isdigit())
    )
    if not valid_order_id:
        return jsonify({"error": "Select a valid filled order to close."}), 400
    market_data_client = _client_holder["client"]
    if market_data_client is None:
        return jsonify({"error": "Schwab market data is not ready yet."}), 503

    try:
        is_paper_order = order_id.startswith("P-")
        if is_paper_order:
            _resolve_order_account(None, data.get("account_id"))
            paper_order = paper_trading.get_order(order_id)
            if not paper_order:
                raise ValueError("This paper order is no longer available.")
            quote = _fetch_option_quote(market_data_client, paper_order["symbol"])
            details = paper_trading.close_setup(order_id, quote)
            details["account_hash"] = paper_trading.PAPER_ACCOUNT_ID
        else:
            trading_client = _get_trading_client()
            details = _close_order_details(
                trading_client, market_data_client, order_id, data.get("account_id"))
        quantity, limit_price = trading.validate_sell_to_close_order(
            data.get("quantity"),
            data.get("limit_price"),
            min(details["available_quantity"], trading.MAX_ORDER_QUANTITY),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception:
        logging.exception("Unable to prepare a sell-to-close order")
        return jsonify({
            "error": "The sell-to-close ticket could not be prepared. Check the terminal and try again."
        }), 502

    token = uuid4().hex
    now = time.time()
    with _order_confirmation_lock:
        expired = [
            key for key, value in _pending_order_confirmations.items()
            if now - value["created_at"] > _ORDER_CONFIRMATION_TTL_SECONDS
        ]
        for key in expired:
            del _pending_order_confirmations[key]
        _pending_order_confirmations[token] = {
            "created_at": now,
            "action": "SELL_TO_CLOSE",
            "account_hash": details["account_hash"],
            "origin_order_id": order_id,
            "option_symbol": details["option_symbol"],
            "quantity": quantity,
            "limit_price": format(limit_price, ".2f"),
            "paper": is_paper_order,
        }

    return jsonify({
        "confirmation_token": token,
        "action": "SELL_TO_CLOSE",
        "origin_order_id": order_id,
        "account": details["account_label"],
        "option_symbol": details["option_symbol"],
        "quantity": quantity,
        "position_quantity": details["position_quantity"],
        "limit_price": format(limit_price, ".2f"),
        "quote": details["quote"],
        "active_exit_count": details["active_exit_count"],
        "duration": "DAY",
        "session": "NORMAL",
    })


@app.post("/api/trading/submit")
def submit_option_order():
    data = request.get_json(silent=True)
    token = data.get("confirmation_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        return jsonify({"error": "Review the order before submitting it."}), 400

    with _order_confirmation_lock:
        order = _pending_order_confirmations.pop(token, None)
    if order is None:
        return jsonify({"error": "This confirmation has expired or was already used. Review the order again."}), 409
    if time.time() - order["created_at"] > _ORDER_CONFIRMATION_TTL_SECONDS:
        return jsonify({"error": "This confirmation expired. Review the order again."}), 409

    market_data_client = _client_holder["client"]
    if market_data_client is None:
        return jsonify({"error": "Market data is not ready. Check the terminal before reviewing the order again."}), 503

    if order.get("paper", config.PAPER_TRADING):
        if order.get("account_hash") != paper_trading.PAPER_ACCOUNT_ID:
            return jsonify({"error": "This review is not for the Paper Trading account. Review again."}), 409
        try:
            option_symbol = order["option_symbol"]
            quote = _fetch_option_quote(market_data_client, option_symbol)
            if order.get("action") == "SELL_TO_CLOSE":
                paper_order = paper_trading.get_order(order["origin_order_id"])
                if not paper_order or paper_order.get("status") != "FILLED":
                    return jsonify({"error": "The paper entry is not filled; refresh orders and review again."}), 409
                order_id = paper_trading.create_sell(
                    order["origin_order_id"], order["quantity"],
                    order["limit_price"], quote)
                message = (
                    "Paper sell-to-close limit order recorded. It will fill only "
                    "when a refreshed option bid reaches your limit."
                )
            else:
                if not _current_order_candidate(option_symbol):
                    return jsonify({"error": "This contract is no longer eligible. Review a current scanner row."}), 409
                order_id = paper_trading.create_buy(
                    option_symbol, order["quantity"], order["limit_price"],
                    order["take_profit_price"], order["stop_price"], quote)
                message = (
                    "Paper bracket order recorded. The entry fills only when a "
                    "refreshed option ask reaches your limit; attached simulated "
                    "exits are monitored on order refresh."
                )
            return jsonify({
                "submitted": True,
                "order_id": order_id,
                "paper": True,
                "message": message,
            })
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception:
            logging.exception("Unable to record paper option order")
            return jsonify({"error": "Paper order could not be recorded. Check the application log."}), 500

    try:
        trading_client = _get_trading_client()
        accounts = _trading_accounts(trading_client)
        if not any(account.get("hashValue") == order["account_hash"] for account in accounts):
            return jsonify({"error": "The selected account is no longer available. Review the order again."}), 409
        if order.get("action") == "SELL_TO_CLOSE":
            source_order = _get_schwab_order(
                trading_client, order["origin_order_id"], order["account_hash"])
            option_symbol, _ = _validate_filled_buy_order(
                source_order, order["option_symbol"])
            position_quantity = _live_option_position_quantity(
                trading_client, order["account_hash"], option_symbol)
            available_quantity = _available_to_sell_quantity(
                trading_client, order["account_hash"], option_symbol,
                position_quantity, exclude_order_id=order["origin_order_id"])
            if available_quantity < order["quantity"]:
                return jsonify({
                    "error": "Your available Schwab position is smaller than the reviewed sell quantity because contracts are already reserved by another open sell. Refresh orders and review again."
                }), 409
            _cancel_and_confirm_active_exits(
                trading_client, source_order, order["account_hash"])
            position_quantity = _live_option_position_quantity(
                trading_client, order["account_hash"], option_symbol)
            available_quantity = _available_to_sell_quantity(
                trading_client, order["account_hash"], option_symbol,
                position_quantity, exclude_order_id=order["origin_order_id"])
            if available_quantity < order["quantity"]:
                return jsonify({
                    "error": "The available position changed while attached exits were canceled. No sell was submitted; refresh orders."
                }), 409
            _fetch_option_quote(market_data_client, option_symbol)
            order_spec = trading.build_sell_to_close_limit_order(
                option_symbol, order["quantity"], order["limit_price"],
                min(position_quantity, trading.MAX_ORDER_QUANTITY))
        else:
            if not _current_order_candidate(order["option_symbol"]):
                return jsonify({"error": "This contract is no longer eligible. Review a current scanner row."}), 409
            _fetch_option_quote(market_data_client, order["option_symbol"])
            order_spec = trading.build_buy_with_oco_exits(
                order["option_symbol"], order["quantity"], order["limit_price"],
                order["take_profit_price"], order["stop_price"])
        response = trading_client.place_order(order["account_hash"], order_spec)
    except OrderPreflightError as exc:
        logging.warning("Schwab sell was stopped before placement: %s", exc)
        return jsonify({"error": str(exc)}), 502
    except Exception:
        logging.exception("Schwab option order submission failed or timed out")
        return jsonify({
            "error": "Schwab's submission outcome could not be confirmed. Check your Schwab orders before retrying."
        }), 502

    if not 200 <= response.status_code < 300:
        logging.error("Schwab rejected option order with HTTP %s", response.status_code)
        return jsonify({"error": f"Schwab rejected the order (HTTP {response.status_code})."}), 502

    location = response.headers.get("Location", "").rstrip("/")
    order_id = location.rsplit("/", 1)[-1] if location else None
    if order.get("action") != "SELL_TO_CLOSE":
        autopilot.mark_submitted(order["option_symbol"])
    return jsonify({
        "submitted": True,
        "order_id": order_id,
        "message": (
            "Schwab accepted the sell-to-close limit order. Check My Orders for its fill status; acceptance does not mean it filled."
            if order.get("action") == "SELL_TO_CLOSE"
            else "Schwab accepted the order. Check its status in your Schwab account; acceptance does not mean it filled."
        ),
    })


@app.get("/api/trading/orders")
def trading_orders():
    mode = request.args.get("mode", "LIVE").upper()
    if mode not in {"PAPER", "LIVE"}:
        return jsonify({"error": "Choose Paper or Live orders."}), 400
    if mode == "PAPER":
        try:
            symbols = paper_trading.active_symbols()
            if symbols and _client_holder["client"] is None:
                return jsonify({"error": "Market data is not ready to evaluate paper orders."}), 503
            quotes = _fetch_option_quotes(_client_holder["client"], symbols)
            warning = None
            missing = sorted(set(symbols) - set(quotes))
            if missing:
                warning = (
                    "Quote unavailable for one or more active paper orders; "
                    "fills were not evaluated for those contracts."
                )
            orders = paper_trading.orders(quotes)
            return jsonify({
                "mode": "PAPER",
                "warning": warning,
                "orders": orders,
            })
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception:
            logging.exception("Unable to retrieve paper orders")
            return jsonify({"error": "Paper orders could not be loaded. Check the terminal and refresh."}), 500

    try:
        trading_client = _get_trading_client()
        accounts = _trading_accounts(trading_client)
        orders = []
        for account_id, account in enumerate(accounts):
            account_hash = account.get("hashValue")
            if not account_hash:
                continue
            positions = _fetch_account_option_positions(
                trading_client, account_hash)
            response = trading_client.get_orders_for_account(
                account_hash, max_results=50)
            if response.status_code != 200:
                if response.status_code == 401:
                    raise RuntimeError(
                        "Schwab denied order access (HTTP 401). Reauthorize the "
                        "Accounts and Trading app, then refresh orders."
                    )
                raise RuntimeError(
                    f"Schwab order lookup failed (HTTP {response.status_code}).")
            account_orders = response.json()
            if not isinstance(account_orders, list):
                raise RuntimeError("Schwab returned an unexpected order list.")
            symbols = {
                symbol
                for order in account_orders
                if isinstance(order, dict)
                for symbol in _order_option_symbols(order, "BUY_TO_OPEN")
            }
            available_to_sell = {
                symbol: max(
                    0,
                    positions.get(symbol, 0)
                    - _open_single_sell_quantity(account_orders, symbol),
                )
                for symbol in symbols
            }
            label = _account_label(account)
            orders.extend(
                _order_monitor_entry(
                    order, account_id, label, positions, available_to_sell)
                for order in account_orders
                if isinstance(order, dict)
            )
        orders.sort(
            key=lambda order: str(order.get("entered_time") or ""),
            reverse=True,
        )
        return jsonify({"mode": "LIVE", "warning": None, "orders": orders})
    except (RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception:
        logging.exception("Unable to retrieve Schwab orders")
        return jsonify({
            "error": "Schwab orders could not be loaded. Check the terminal and refresh."
        }), 502


@app.get("/api/trading/positions")
def trading_positions():
    """Current option positions with fresh quotes for the position monitor."""
    mode = request.args.get("mode", "LIVE").upper()
    if mode not in {"PAPER", "LIVE"}:
        return jsonify({"error": "Choose Paper or Live positions."}), 400
    try:
        if mode == "PAPER":
            orders = paper_trading.orders()
            positions = [order for order in orders if order.get("instruction") == "BUY_TO_OPEN"
                         and order.get("position_quantity", 0) > 0]
            symbols = sorted({order["symbol"] for order in positions})
            if symbols and _client_holder["client"] is None:
                return jsonify({"error": "Market data is not ready to price paper positions."}), 503
            quotes = _fetch_option_quotes(_client_holder["client"], symbols)
            rows = []
            for order in positions:
                quote = quotes.get(order["symbol"], {})
                mark = quote.get("mark")
                if mark is None and quote.get("bid") is not None and quote.get("ask") is not None:
                    mark = (float(quote["bid"]) + float(quote["ask"])) / 2
                try:
                    quantity = int(order["position_quantity"])
                    cost = float(order.get("fill_price") or order.get("price") or 0)
                    mark_value = float(mark) if mark is not None else None
                except (TypeError, ValueError):
                    cost, mark_value = 0, None
                rows.append({
                    "symbol": order["symbol"], "account_label": "Paper Trading",
                    "quantity": quantity, "average_price": cost or None,
                    "bid": quote.get("bid"), "mark": mark_value, "ask": quote.get("ask"),
                    "market_value": mark_value * quantity * 100 if mark_value is not None else None,
                    "unrealized_pl": (mark_value - cost) * quantity * 100 if mark_value is not None and cost else None,
                    "day_pl": None,
                })
            return jsonify({"mode": mode, "positions": rows, "warning": None})

        client = _get_trading_client()
        rows = []
        for account in _trading_accounts(client):
            account_hash = account.get("hashValue")
            if not account_hash:
                continue
            response = client.get_account(account_hash, fields=[client.Account.Fields.POSITIONS])
            if response.status_code != 200:
                raise RuntimeError(f"Schwab position lookup failed (HTTP {response.status_code}).")
            data = response.json()
            account_data = data.get("securitiesAccount") or data
            for position in account_data.get("positions") or []:
                instrument = position.get("instrument") or {}
                symbol = instrument.get("symbol")
                if not symbol or instrument.get("assetType") != "OPTION":
                    continue
                try:
                    quantity = float(position.get("longQuantity") or 0) - float(position.get("shortQuantity") or 0)
                    average = float(position.get("averagePrice") or position.get("averageLongPrice") or 0)
                except (TypeError, ValueError):
                    continue
                if quantity <= 0:
                    continue
                rows.append({
                    "symbol": str(symbol), "account_label": _account_label(account),
                    "quantity": quantity, "average_price": average or None,
                    "market_value": position.get("marketValue"),
                    "unrealized_pl": position.get("longOpenProfitLoss"),
                    "day_pl": position.get("currentDayProfitLoss"),
                })
        quotes = _fetch_option_quotes(client, sorted({row["symbol"] for row in rows}))
        for row in rows:
            quote = quotes.get(row["symbol"], {})
            row.update({"bid": quote.get("bid"), "mark": quote.get("mark"), "ask": quote.get("ask")})
            if row.get("mark") is None and row.get("bid") is not None and row.get("ask") is not None:
                row["mark"] = (float(row["bid"]) + float(row["ask"])) / 2
        return jsonify({"mode": mode, "positions": rows, "warning": None})
    except (RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception:
        logging.exception("Unable to retrieve active option positions")
        return jsonify({"error": "Positions could not be loaded. Check the terminal and refresh."}), 500


@app.post("/api/trading/orders/<order_id>/cancel")
def cancel_trading_order(order_id):
    if order_id.startswith("P-"):
        try:
            paper_trading.cancel(order_id)
            return jsonify({
                "cancellation_requested": True,
                "paper": True,
                "message": "Paper order canceled locally.",
            })
        except LookupError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        except Exception:
            logging.exception("Unable to cancel paper order %s", order_id)
            return jsonify({"error": "Paper cancellation could not be recorded."}), 500
    if not order_id.isdigit():
        return jsonify({"error": "Provide a valid Schwab order ID."}), 400
    data = request.get_json(silent=True)
    account_id = data.get("account_id") if isinstance(data, dict) else None
    try:
        account_hash, _ = _resolve_account(_get_trading_client(), account_id)
        trading_client = _get_trading_client()
        current = trading_client.get_order(order_id, account_hash)
        if current.status_code == 401:
            raise RuntimeError(
                "Schwab denied order access (HTTP 401). Reauthorize the Accounts "
                "and Trading app, then try again."
            )
        if current.status_code == 404:
            return jsonify({"error": "This order is no longer available in the selected account."}), 404
        if current.status_code != 200:
            raise RuntimeError(
                f"Schwab order lookup failed (HTTP {current.status_code}).")
        order = current.json()
        if not isinstance(order, dict):
            raise RuntimeError("Schwab returned an unexpected order response.")
        if str(order.get("orderId")) != order_id:
            return jsonify({"error": "Schwab returned a different order. Refresh and try again."}), 409
        status = str(order.get("status") or "UNKNOWN").upper()
        if status not in _CANCELLABLE_ORDER_STATUSES:
            return jsonify({
                "error": f"This order is {status.replace('_', ' ').lower()} and cannot be canceled."
            }), 409
        response = trading_client.cancel_order(order_id, account_hash)
        if not 200 <= response.status_code < 300:
            return jsonify({
                "error": f"Schwab did not accept the cancellation (HTTP {response.status_code})."
            }), 502
        return jsonify({
            "cancellation_requested": True,
            "message": "Cancellation requested. Refresh to confirm the order status with Schwab.",
        })
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception:
        logging.exception("Unable to cancel Schwab order %s", order_id)
        return jsonify({
            "error": "Schwab could not confirm the cancellation. Refresh orders before retrying."
        }), 502


def _handle_stream_quotes(message):
    for item in message.get("content", []) or []:
        symbol = item.get("key") or item.get("1")
        if not symbol:
            continue
        quote = {}
        for field, name in (("2", "bid"), ("3", "ask"), ("4", "last")):
            if item.get(field) is not None:
                quote[name] = item[field]
        if quote:
            with _stream_lock:
                now = time.time()
                previous = _stream_quotes.get(symbol, {})
                current_value = quote.get("last")
                if current_value is None and quote.get("bid") is not None and quote.get("ask") is not None:
                    current_value = (float(quote["bid"]) + float(quote["ask"])) / 2
                current_value = float(current_value) if current_value is not None else None
                previous_value = previous.get("last")
                if previous_value is None and previous.get("bid") is not None and previous.get("ask") is not None:
                    previous_value = (float(previous["bid"]) + float(previous["ask"])) / 2
                previous_value = float(previous_value) if previous_value is not None else None
                if current_value is not None:
                    if previous_value is not None:
                        quote["live_change"] = round(current_value - previous_value, 4)
                    history = _stream_history.setdefault(symbol, [])
                    history.append((now, current_value))
                    cutoff = now - _STREAM_SIGNAL_SECONDS
                    while len(history) > 1 and history[1][0] <= cutoff:
                        history.pop(0)
                    if history and history[0][0] <= cutoff:
                        quote["ten_min_change"] = round(current_value - history[0][1], 4)
                _stream_quotes[symbol] = quote


def _handle_stream_equity_quotes(message):
    for item in message.get("content", []) or []:
        symbol = item.get("key") or item.get("0")
        last = item.get("3")
        if not symbol or last is None:
            continue
        try:
            last_price = float(last)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(last_price) or last_price <= 0:
            continue
        with _stream_lock:
            _stream_quotes[symbol] = {
                "last": last_price,
                "quote_time_millis": item.get("34"),
                "trade_time_millis": item.get("35"),
                "received_at": time.time(),
            }


async def _run_stream(client):
    stream = StreamClient(client)
    await stream.login()
    stream.add_level_one_option_handler(_handle_stream_quotes)
    stream.add_level_one_equity_handler(_handle_stream_equity_quotes)
    _stream_state["client"] = stream
    _stream_state["error"] = None
    _stream_state["ready"] = True
    await stream.handle_message()


def _stream_error_message(exc: Exception) -> str:
    message = str(exc)
    if "403" in message or "Forbidden" in message:
        return (
            "Schwab denied streaming setup (HTTP 403 at /trader/v1/userPreference). "
            "Confirm the configured Accounts and Trading app has active production API "
            "access and that its credentials and token belong to that app. This response "
            "is an access denial from Schwab; signing in again usually will not fix a "
            "missing API entitlement. If access is active, contact Schwab Trader API "
            "support with this endpoint and the time of the failure."
        )
    if "401" in message or "Unauthorized" in message:
        return (
            "Schwab rejected streaming authorization (HTTP 401). Streaming uses the "
            "Accounts and Trading app, not the Market Data app. Verify its product "
            "access and credentials; if needed, move the token at "
            f"{config.TRADING_TOKEN_PATH} aside and sign in again."
        )
    return message


def _start_stream(client):
    def runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _stream_state["loop"] = loop
        try:
            loop.run_until_complete(_run_stream(client))
        except Exception as exc:
            message = _stream_error_message(exc)
            _stream_state["error"] = message
            print(f"Schwab streaming unavailable: {message}")
        finally:
            _stream_state["ready"] = False
            loop.close()

    threading.Thread(target=runner, daemon=True).start()


def _subscribe_option(option_symbol: str):
    loop = _stream_state.get("loop")
    stream = _stream_state.get("client")
    if not loop or not stream or not _stream_state.get("ready"):
        return False
    fields = [
        StreamClient.LevelOneOptionFields.BID_PRICE,
        StreamClient.LevelOneOptionFields.ASK_PRICE,
        StreamClient.LevelOneOptionFields.LAST_PRICE,
    ]
    future = asyncio.run_coroutine_threadsafe(
        stream.level_one_option_add([option_symbol], fields=fields), loop)
    try:
        future.result(timeout=3)
        return True
    except Exception:
        return False


def _subscribe_equity(symbol: str):
    loop = _stream_state.get("loop")
    stream = _stream_state.get("client")
    if not loop or not stream or not _stream_state.get("ready"):
        return False
    fields = [
        StreamClient.LevelOneEquityFields.LAST_PRICE,
        StreamClient.LevelOneEquityFields.QUOTE_TIME_MILLIS,
        StreamClient.LevelOneEquityFields.TRADE_TIME_MILLIS,
    ]
    future = asyncio.run_coroutine_threadsafe(
        stream.level_one_equity_add([symbol], fields=fields), loop)
    try:
        future.result(timeout=3)
        return True
    except Exception:
        logging.exception("Could not subscribe to Schwab equity stream for %s", symbol)
        return False


def _subscribe_result_stocks(per_symbol: dict):
    for symbol in per_symbol:
        if symbol in _equity_stream_subscriptions:
            continue
        if _subscribe_equity(symbol):
            _equity_stream_subscriptions.add(symbol)


def _subscribe_result_contracts(per_symbol: dict):
    symbols = set()
    for outcome in per_symbol.values():
        for contract in (outcome.get("call"), outcome.get("put")):
            if contract and contract.get("option_symbol"):
                symbols.add(contract["option_symbol"])
        for contract in outcome.get("weekly_spread", []) or []:
            if contract.get("option_symbol"):
                symbols.add(contract["option_symbol"])

    for option_symbol in symbols:
        if option_symbol in _stream_subscriptions:
            continue
        if _subscribe_option(option_symbol):
            _stream_subscriptions.add(option_symbol)


def _apply_stream_quote(contract: dict):
    with _stream_lock:
        quote = dict(_stream_quotes.get(contract.get("option_symbol"), {}))
    if not quote:
        return

    bid = quote.get("bid", contract.get("bid"))
    ask = quote.get("ask", contract.get("ask"))
    last = quote.get("last")
    current_price = last or ((bid + ask) / 2 if bid and ask else None)
    if current_price is None:
        return

    contract["bid"] = bid
    contract["ask"] = ask
    contract["current_price"] = current_price
    contract["live_change"] = quote.get("live_change")
    contract["ten_min_change"] = quote.get("ten_min_change")
    contract["spread_pct"] = sc.spread_pct(contract)

    baseline = contract.get("price_at_baseline")
    if baseline not in (None, 0):
        change = round(current_price - baseline, 2)
        contract["change_since_baseline"] = change
        contract["pct_since_baseline"] = round(change / baseline * 100, 2)

    noon = contract.get("price_at_noon")
    if noon not in (None, 0):
        change = round(current_price - noon, 2)
        contract["change_since_noon"] = change
        contract["pct_since_noon"] = round(change / noon * 100, 2)


def _results_with_stream_quotes(per_symbol: dict):
    results = copy.deepcopy(per_symbol)
    regular_hours = sc.is_regular_market_hours()
    for symbol, outcome in results.items():
        if regular_hours:
            with _stream_lock:
                stock_quote = dict(_stream_quotes.get(symbol, {}))
            context = outcome.get("day_trade_context") or {}
            live_price = stock_quote.get("last")
            if live_price is not None:
                context["stock_price"] = round(live_price, 4)
                ema20 = context.get("stock_ema20")
                context["stock_ema20_relation"] = (
                    "ABOVE" if ema20 is not None and live_price > ema20 else
                    "BELOW" if ema20 is not None and live_price < ema20 else
                    "AT" if ema20 is not None else None
                )
                momentum_baseline = context.get("stock_momentum_baseline_10m")
                if momentum_baseline is not None and momentum_baseline > 0:
                    momentum_change = round(live_price - momentum_baseline, 4)
                    context["stock_momentum_change"] = momentum_change
                    context["stock_momentum"] = (
                        "UP" if momentum_change > 0 else
                        "DOWN" if momentum_change < 0 else "FLAT"
                    )
                for period, baseline_key in (
                        ("1h", "stock_baseline_1h"),
                        ("930_to_3", "stock_baseline_930"),
                        ("1d", "stock_baseline_830")):
                    baseline = context.get(baseline_key)
                    if baseline is not None and baseline > 0:
                        context[f"stock_pct_{period}"] = round(
                            (live_price - baseline) / baseline * 100, 2)
                now = dt.datetime.now(sc.tz())
                noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
                if now < noon:
                    nine_thirty_baseline = context.get("stock_baseline_930")
                    if nine_thirty_baseline is not None and nine_thirty_baseline > 0:
                        context["stock_pct_930_to_noon"] = round(
                            (live_price - nine_thirty_baseline) / nine_thirty_baseline * 100, 2)
                    morning_baseline = context.get("stock_baseline_830")
                    if morning_baseline is not None and morning_baseline > 0:
                        context["stock_pct_830_to_noon"] = round(
                            (live_price - morning_baseline) / morning_baseline * 100, 2)
                else:
                    noon_baseline = context.get("stock_baseline_noon")
                    if noon_baseline is not None and noon_baseline > 0:
                        context["stock_pct_noon_to_3"] = round(
                            (live_price - noon_baseline) / noon_baseline * 100, 2)
                context["live_price_as_of"] = stock_quote.get("trade_time_millis")
                outcome["day_trade_context"] = context
        for contract in (outcome.get("call"), outcome.get("put")):
            if contract:
                _apply_stream_quote(contract)
        for contract in outcome.get("weekly_spread", []) or []:
            _apply_stream_quote(contract)
    return results


def _qualifying_now(outcome: dict) -> list:
    out = []
    for c in (outcome.get("call"), outcome.get("put")):
        if c and c.get("change_since_baseline") is not None and c["change_since_baseline"] > 0:
            out.append(c)
    return out


def _check_for_new_entries(per_symbol: dict):
    """Email only after a contract stays qualifying for a few refreshes. Reset the
    cooldown when the contract drops out so the same contract can re-alert later."""
    global _previously_qualifying, _pending_email_contracts, _email_sent_contracts

    current = {}
    for outcome in per_symbol.values():
        for c in _qualifying_now(outcome):
            current[c["option_symbol"]] = c

    current_symbols = set(current.keys())
    stable_new = []

    for symbol in sorted(current_symbols):
        if symbol in _email_sent_contracts:
            continue

        if symbol in _previously_qualifying:
            count = _pending_email_contracts.get(symbol, 0) + 1
            _pending_email_contracts[symbol] = count
        else:
            _pending_email_contracts[symbol] = 1

        if _pending_email_contracts.get(symbol, 0) >= _EMAIL_STABILITY_PASSES:
            stable_new.append(current[symbol])
            _email_sent_contracts.add(symbol)
            del _pending_email_contracts[symbol]

    for symbol in list(_pending_email_contracts.keys()):
        if symbol not in current_symbols:
            del _pending_email_contracts[symbol]

    for symbol in list(_email_sent_contracts):
        if symbol not in current_symbols:
            _email_sent_contracts.remove(symbol)

    if stable_new and email_notify.email_configured():
        subject, body = email_notify.format_entry_alert("Options Scanner", stable_new)
        email_notify.send_email(subject, body)

    _previously_qualifying = current_symbols


def _ema_table_contracts(per_symbol: dict, direction: str, put_call: str) -> dict:
    """Return the contracts currently shown on one EMA dashboard table."""
    contracts = {}
    for outcome in per_symbol.values():
        contract = outcome.get("call" if put_call == "CALL" else "put")
        if not contract:
            continue
        if contract.get("put_call") != put_call or contract.get("ema_crossover_direction") != direction:
            continue
        if contract.get("option_pct_since_crossover") is None:
            continue
        if contract.get("spread_pct") is None or contract["spread_pct"] >= 10:
            continue
        contracts[contract["option_symbol"]] = contract
    return contracts


def _check_ema_table_changes(per_symbol: dict):
    """Email contracts added to or removed from the EMA calls/puts tables."""
    global _previously_ema
    current = {
        "calls": _ema_table_contracts(per_symbol, "above", "CALL"),
        "puts": _ema_table_contracts(per_symbol, "below", "PUT"),
    }
    for key, label in (("calls", "EMA8 Crossed Above EMA21 - Calls"),
                       ("puts", "EMA8 Crossed Below EMA21 - Puts")):
        previous = _previously_ema[key]
        if previous is not None:
            added = [current[key][symbol] for symbol in current[key].keys() - previous.keys()]
            removed = [previous[symbol] for symbol in previous.keys() - current[key].keys()]
            if (added or removed) and email_notify.email_configured():
                subject, body = email_notify.format_membership_alert(label, added, removed)
                email_notify.send_email(subject, body)
    _previously_ema = current


def _weekly_trend_candidates(per_symbol: dict) -> list[dict]:
    candidates = []
    for symbol, outcome in per_symbol.items():
        trend = outcome.get("trend_status")
        if trend != "BULLISH":
            continue
        for option in outcome.get("weekly_spread", []) or []:
            put_call = option.get("put_call")
            try:
                volume = float(option.get("volume") or 0)
                spread = float(option.get("spread_pct"))
            except (TypeError, ValueError):
                continue
            if (not math.isfinite(volume) or not math.isfinite(spread)
                    or volume < sc.MIN_WEEKLY_OPTION_VOLUME
                    or spread >= sc.MAX_WEEKLY_SPREAD_PCT):
                continue
            if put_call != "CALL":
                continue
            candidates.append({
                **option,
                "underlying": symbol,
                "volume": volume,
                "spread_pct": spread,
                "stock_trend": trend,
                "scanner_setup": "CALL · STOCK UP",
            })
    return sorted(
        candidates,
        key=lambda option: (
            -float(option.get("volume") or 0),
            float(option.get("spread_pct") or 0),
            option.get("underlying", ""),
        ),
    )


def _live_open_contract_count():
    client = _get_trading_client()
    total = 0
    for account in _trading_accounts(client):
        account_hash = account.get("hashValue")
        if account_hash:
            total += sum(_fetch_account_option_positions(client, account_hash).values())
    return total


def _live_account_value():
    """Total liquidation value across the linked Schwab accounts."""
    client = _get_trading_client()
    total = 0.0
    for account in _trading_accounts(client):
        account_hash = account.get("hashValue")
        if not account_hash:
            continue
        response = client.get_account(account_hash)
        if response.status_code != 200:
            raise RuntimeError(f"Schwab balance lookup failed (HTTP {response.status_code}).")
        balances = (response.json().get("securitiesAccount") or {}).get("currentBalances") or {}
        total += float(balances.get("liquidationValue") or 0)
    return total


def _autopilot_place_live(option_symbol, quantity, entry, take_profit, stop):
    """Send an autopilot buy with OCO exits to the configured Schwab account."""
    client = _get_trading_client()
    accounts = _trading_accounts(client)
    index = config.AUTOPILOT_LIVE_ACCOUNT_INDEX
    if index >= len(accounts) or not accounts[index].get("hashValue"):
        raise RuntimeError("AUTOPILOT_LIVE_ACCOUNT_INDEX does not match a Schwab account.")
    spec = trading.build_buy_with_oco_exits(
        option_symbol, quantity, format(entry, ".2f"),
        format(take_profit, ".2f"), format(stop, ".2f"))
    response = client.place_order(accounts[index]["hashValue"], spec)
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Schwab rejected the autopilot order (HTTP {response.status_code}).")
    location = response.headers.get("Location", "").rstrip("/")
    return location.rsplit("/", 1)[-1] if location else "submitted"


def background_loop(watchlist_path: str, interval: int, weekly_universe: bool,
                     rebuild_universe: bool, max_symbols: int):
    try:
        client = get_client()
    except Exception as exc:
        logging.exception("Market Data Schwab authorization failed")
        with state_lock:
            state["error"] = (
                "Market Data sign-in did not complete. Finish the Schwab login and allow "
                "the browser to redirect to the registered callback "
                f"({config.CALLBACK_URL}), then restart the app. Details: {exc}"
            )
        return

    _client_holder["client"] = client
    cache = sc.load_cache()
    rolling_cache = {}  # in-memory only; rolling 4h window has no lasting cache value across refreshes
    ema_cache = {}  # in-memory only; per-symbol EMA8/EMA21, refreshed every config.EMA_REFRESH_SECONDS

    if weekly_universe:
        import universe

        def _report_progress(checked, total, found):
            with state_lock:
                state["universe_progress"] = {"checked": checked, "total": total, "found": found}

        try:
            tickers = universe.get_weekly_universe(
                client, force_rebuild=rebuild_universe, max_symbols=max_symbols,
                on_progress=_report_progress)
        except Exception as exc:
            logging.exception("Could not build the weekly-options universe")
            with state_lock:
                state["error"] = (
                    "Could not build the weekly-options universe. Check your internet "
                    "connection and the NASDAQ symbol-directory source, or restart without "
                    "--weekly-universe to use watchlist.txt. Details: " + str(exc)
                )
                state["universe_progress"] = None
            return
    else:
        tickers = sc.load_watchlist(watchlist_path)

    with state_lock:
        state["symbol_count"] = len(tickers)
        state["universe_progress"] = None  # build finished (or was skipped via watchlist file)
        state["scan_progress"] = {
            "completed": 0,
            "total": len(tickers),
            "running": True,
        }

    try:
        _get_trading_client()
    except Exception as exc:
        message = _stream_error_message(exc)
        _stream_state["error"] = message
        logging.warning("Schwab streaming could not start: %s", message)

    while True:
        try:
            with state_lock:
                state["scan_progress"] = {
                    "completed": 0,
                    "total": len(tickers),
                    "running": True,
                }

            completed_in_pass = 0

            def _publish_symbol(symbol, outcome):
                nonlocal completed_in_pass
                completed_in_pass += 1
                with state_lock:
                    state["per_symbol"][symbol] = outcome
                    state["updated_at"] = dt.datetime.now().strftime("%H:%M:%S")
                    state["error"] = None
                    state["scan_progress"] = {
                        "completed": completed_in_pass,
                        "total": len(tickers),
                        "running": True,
                    }

            underlying_prices = None
            price_range = None
            if weekly_universe:
                try:
                    underlying_prices = sc.fetch_underlying_prices(client, tickers)
                except Exception as exc:
                    raise RuntimeError(
                        "Could not check stock prices for the weekly scanner range. "
                        f"Details: {exc}"
                    ) from exc
                price_range = (
                    config.WEEKLY_MIN_UNDERLYING_PRICE,
                    config.WEEKLY_MAX_UNDERLYING_PRICE,
                )

            per_symbol = sc.run_pass_full(
                client, tickers, cache, rolling_cache, ema_cache,
                on_symbol=_publish_symbol,
                underlying_prices=underlying_prices,
                underlying_price_range=price_range)
            _subscribe_result_stocks(per_symbol)
            _subscribe_result_contracts(per_symbol)
            sc.save_cache(cache)
            _check_for_new_entries(per_symbol)
            _check_ema_table_changes(per_symbol)
            with state_lock:
                state["per_symbol"] = per_symbol
                state["updated_at"] = dt.datetime.now().strftime("%H:%M:%S")
                state["error"] = None
                state["scan_progress"] = {
                    "completed": len(tickers),
                    "total": len(tickers),
                    "running": False,
                }
            autopilot.run_cycle(
                per_symbol,
                lambda symbols: _fetch_option_quotes(client, symbols),
                _current_order_candidate,
                _live_open_contract_count,
                _live_account_value,
                _autopilot_place_live,
                lambda symbol: _option_momentum(client, symbol).get("status"))
        except Exception as e:  # keep the loop alive even if one pass fails  # keep the loop alive even if one pass fails
            with state_lock:
                state["error"] = str(e)
                if state["scan_progress"] is not None:
                    state["scan_progress"]["running"] = False
        time.sleep(interval)


@app.get("/api/autopilot/status")
def autopilot_status():
    return jsonify(autopilot.status())


@app.get("/api/autopilot/results")
def autopilot_results():
    return jsonify(autopilot.results())


@app.get("/api/autopilot/export.csv")
def autopilot_export():
    return Response(
        autopilot.export_csv(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=autopilot_trades.csv"})


@app.post("/api/autopilot/mode")
def autopilot_mode():
    mode = str((request.get_json(silent=True) or {}).get("mode", "")).lower()
    try:
        autopilot.set_mode(mode)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify(autopilot.status())


@app.post("/api/autopilot/enable")
def autopilot_enable():
    autopilot.set_enabled(True)
    return jsonify(autopilot.status())


@app.post("/api/autopilot/disable")
def autopilot_disable():
    autopilot.set_enabled(False)
    return jsonify(autopilot.status())


@app.post("/api/autopilot/proposals/<proposal_id>/dismiss")
def autopilot_dismiss(proposal_id):
    if not autopilot.dismiss_proposal(proposal_id):
        return jsonify({"error": "This proposal has already expired or been dismissed."}), 404
    return jsonify(autopilot.status())


@app.route("/")
def index():
    return render_template(
        "index.html",
        interval=state["interval"],
        weekly_universe=state["weekly_universe"],
        paper_trading=config.PAPER_TRADING,
    )


@app.get("/service-worker.js")
def service_worker():
    """Serve the network-only service worker from the app root scope."""
    response = app.send_static_file("service-worker.js")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.route("/api/results")
def api_results():
    with state_lock:
        per_symbol = _results_with_stream_quotes(state["per_symbol"])
        response = jsonify({
            "per_symbol": per_symbol,
            "weekly_trend_candidates": _weekly_trend_candidates(per_symbol),
            "updated_at": state["updated_at"],
            "interval": state["interval"],
            "error": state["error"],
            "symbol_count": state["symbol_count"],
            "weekly_universe": state["weekly_universe"],
            "underlying_price_range": (
                {
                    "minimum": config.WEEKLY_MIN_UNDERLYING_PRICE,
                    "maximum": config.WEEKLY_MAX_UNDERLYING_PRICE,
                }
                if state["weekly_universe"] else None
            ),
            "regular_hours": sc.is_regular_market_hours(),
            "scan_progress": dict(state["scan_progress"])
            if state["scan_progress"] else None,
            "streaming_ready": _stream_state["ready"],
            "streaming_error": _stream_state["error"],
        })
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response


@app.route("/api/history/<chart_symbol>")
def api_history(chart_symbol):
    """Today's 5-minute history for an option contract or underlying stock chart."""
    client = _client_holder["client"]
    if client is None:
        return jsonify({"error": "not ready yet, try again in a moment", "points": []}), 503

    try:
        points = sc.fetch_today_history(client, chart_symbol)
    except Exception as e:
        return jsonify({"error": str(e), "points": []}), 500

    baseline_dt, baseline_label = sc.active_baseline()
    # Keep regular-session candles even when the provider's empty-today
    # fallback returns the prior session. This lets the chart remain useful
    # after hours while still excluding premarket and postmarket bars.
    regular_points = []
    for point in points:
        try:
            candle_dt = dt.datetime.fromtimestamp(
                point["time_ms"] / 1000, tz=sc.tz()
            )
        except (KeyError, TypeError, ValueError, OSError):
            continue
        session_open = candle_dt.replace(
            hour=config.BASELINE_HOUR, minute=config.BASELINE_MINUTE,
            second=0, microsecond=0,
        )
        session_close = candle_dt.replace(hour=15, minute=0, second=0, microsecond=0)
        if session_open <= candle_dt <= session_close:
            regular_points.append(point)
    points = regular_points

    # Anchor the visible time axis and session markers to the date of the
    # latest displayed candle (today, or the fallback session).
    session_date = (
        dt.datetime.fromtimestamp(
            max(point["time_ms"] for point in points) / 1000, tz=sc.tz()
        ).date()
        if points else baseline_dt.date()
    )
    session_open_dt = baseline_dt.replace(
        year=session_date.year, month=session_date.month, day=session_date.day
    )
    session_close_dt = session_open_dt.replace(hour=15, minute=0)
    market_open_ms = int(session_open_dt.timestamp() * 1000)
    market_close_ms = int(session_close_dt.timestamp() * 1000)
    nine_thirty_dt = session_open_dt.replace(hour=9, minute=30)
    ten_thirty_dt = session_open_dt.replace(hour=10, minute=30)
    noon_session_dt = session_open_dt.replace(hour=12, minute=0)
    return jsonify({
        "error": None,
        "points": points,
        "market_open_ms": market_open_ms,
        "nine_thirty_ms": int(nine_thirty_dt.timestamp() * 1000),
        "ten_thirty_ms": int(ten_thirty_dt.timestamp() * 1000),
        "baseline_label": baseline_label,
        "noon_ms": int(noon_session_dt.timestamp() * 1000),
        "market_close_ms": market_close_ms,
        "noon_label": sc.NOON_LABEL,
    })


@app.route("/api/stream/subscribe/<path:option_symbol>")
def stream_subscribe(option_symbol):
    return jsonify({"subscribed": _subscribe_option(option_symbol)})


@app.route("/api/quote/<path:option_symbol>")
def api_quote(option_symbol):
    with _stream_lock:
        quote = dict(_stream_quotes.get(option_symbol, {}))
    return jsonify({
        "streaming": _stream_state.get("ready", False),
        "error": _stream_state.get("error"),
        "quote": quote,
    })


def _option_momentum(client, option_symbol):
    """Cached ten-minute option momentum (RISING, SIDEWAYS, FALLING, ...)."""
    with _option_momentum_lock:
        cached = _option_momentum_cache.get(option_symbol)
        if cached and (time.time() - cached["cached_at"]) < _OPTION_MOMENTUM_CACHE_SECONDS:
            return cached["data"]
    now = dt.datetime.now(sc.tz())
    candles = sc.fetch_minute_candles(client, option_symbol, now)
    momentum = sc.classify_option_momentum(candles, now)
    with _option_momentum_lock:
        _option_momentum_cache[option_symbol] = {
            "cached_at": time.time(),
            "data": momentum,
        }
    return momentum


@app.route("/api/momentum/<path:option_symbol>")
def api_option_momentum(option_symbol):
    """Regular-session ten-minute option momentum, independent of streaming auth."""
    with state_lock:
        current_symbols = {
            contract.get("option_symbol")
            for outcome in state["per_symbol"].values()
            for contract in (
                outcome.get("call"),
                outcome.get("put"),
                *(outcome.get("weekly_spread", []) or []),
            )
            if contract and contract.get("option_symbol")
        }
        client = _client_holder["client"]
    if option_symbol not in current_symbols:
        return jsonify({"error": "This contract is no longer in the current scan."}), 404
    if client is None:
        return jsonify({"error": "Market-data client is not ready yet."}), 503

    try:
        return jsonify(_option_momentum(client, option_symbol))
    except Exception as exc:
        logging.exception("Option momentum lookup failed for current scanner contract")
        return jsonify({"error": f"Could not load option momentum: {exc}"}), 502


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchlist", default="watchlist.txt",
                         help="Ticker file to scan, one symbol per line (default: watchlist.txt).")
    parser.add_argument("--interval", type=int, default=10,
                         help="Seconds between refreshes (default 10).")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--weekly-universe", action="store_true",
                         help="Ignore --watchlist and instead build the full weekly-optionable "
                              "universe live from the Schwab API (see universe.py). Slow on "
                              "first run of the day.")
    parser.add_argument("--rebuild-universe", action="store_true",
                         help="With --weekly-universe: force-refresh the symbol list on startup "
                              "instead of using today's cached one.")
    parser.add_argument("--max-symbols", type=int, default=None,
                         help="With --weekly-universe: cap the universe to this many symbols "
                              "(useful for a quick test run).")
    args = parser.parse_args()

    state["interval"] = args.interval
    state["weekly_universe"] = args.weekly_universe

    if config.EMAIL_ENABLED and not email_notify.email_configured():
        print("Warning: EMAIL_ENABLED=true but SMTP settings in .env are incomplete. "
              "Email alerts will be skipped until this is fixed.")

    t = threading.Thread(
        target=background_loop,
        args=(args.watchlist, args.interval, args.weekly_universe,
              args.rebuild_universe, args.max_symbols),
        daemon=True,
    )
    t.start()

    print(f"Dashboard running at http://127.0.0.1:{args.port}")
    print("Press Ctrl+C to stop.")
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
