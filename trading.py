"""Validation and construction for the dashboard's manual option orders."""
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from schwab.orders.common import (
    Duration,
    OptionInstruction,
    OrderStrategyType,
    OrderType,
    Session,
    first_triggers_second,
    one_cancels_other,
)
from schwab.orders.generic import OrderBuilder
from schwab.orders.options import option_buy_to_open_limit, option_sell_to_close_limit

MAX_ORDER_QUANTITY = 100
MAX_ORDER_PRICE = Decimal("2.00")
OCO_EXIT_PRESETS = {
    "tight": {"label": "10% Profit / 20% Stop", "take_profit_pct": Decimal("10"), "stop_loss_pct": Decimal("20")},
    "conservative": {"label": "Conservative", "take_profit_pct": Decimal("20"), "stop_loss_pct": Decimal("15")},
    "balanced": {"label": "Balanced", "take_profit_pct": Decimal("30"), "stop_loss_pct": Decimal("20")},
    "wider": {"label": "Wider", "take_profit_pct": Decimal("50"), "stop_loss_pct": Decimal("30")},
}


def validate_order(quantity, limit_price):
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ValueError("Quantity must be a whole number.")
    if not 1 <= quantity <= MAX_ORDER_QUANTITY:
        raise ValueError(f"Quantity must be between 1 and {MAX_ORDER_QUANTITY}.")

    try:
        price = Decimal(str(limit_price))
    except (InvalidOperation, ValueError):
        raise ValueError("Limit price must be a valid dollar amount.") from None
    if not price.is_finite() or price < Decimal("0.01") or price > MAX_ORDER_PRICE:
        raise ValueError("Limit price must be between $0.01 and $2.00.")
    if price != price.quantize(Decimal("0.01")):
        raise ValueError("Limit price must be in $0.01 increments.")
    return quantity, price


def build_buy_to_open_limit_order(option_symbol, quantity, limit_price):
    quantity, price = validate_order(quantity, limit_price)
    return option_buy_to_open_limit(
        option_symbol, quantity, format(price, ".2f")).build()


def validate_sell_to_close_order(quantity, limit_price, position_quantity):
    if isinstance(position_quantity, bool) or not isinstance(position_quantity, int):
        raise ValueError("Current Schwab position quantity is unavailable.")
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ValueError("Quantity must be a whole number.")
    if not 1 <= quantity <= min(position_quantity, MAX_ORDER_QUANTITY):
        raise ValueError(
            f"Quantity must be between 1 and the {position_quantity} contracts currently held."
        )
    price = _validate_exit_price(limit_price, "Sell limit price")
    return quantity, price


def select_sell_limit_reference(quote):
    """Prefer the live bid for a sell-to-close limit; fall back to mark/mid/last."""
    for field in ("bid", "mark", "last", "ask"):
        value = quote.get(field)
        try:
            price = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if price.is_finite() and price > 0:
            increment = Decimal("0.05") if price >= Decimal("3.00") else Decimal("0.01")
            return (price / increment).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP) * increment

    bid = quote.get("bid")
    ask = quote.get("ask")
    try:
        bid_price = Decimal(str(bid))
        ask_price = Decimal(str(ask))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if bid_price.is_finite() and ask_price.is_finite() and ask_price >= bid_price >= 0:
        midpoint = (bid_price + ask_price) / Decimal("2")
        increment = Decimal("0.05") if midpoint >= Decimal("3.00") else Decimal("0.01")
        return (midpoint / increment).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP) * increment
    return None


def build_sell_to_close_limit_order(option_symbol, quantity, limit_price,
                                    position_quantity):
    quantity, price = validate_sell_to_close_order(
        quantity, limit_price, position_quantity)
    return option_sell_to_close_limit(
        option_symbol, quantity, format(price, ".2f")).build()


def _validate_exit_price(value, label):
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{label} is required.")
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be a valid dollar amount.") from None
    if not price.is_finite() or price < Decimal("0.01"):
        raise ValueError(f"{label} must be at least $0.01.")
    increment = Decimal("0.05") if price >= Decimal("3.00") else Decimal("0.01")
    if price % increment != 0:
        raise ValueError(
            f"{label} must use $0.01 increments below $3.00 or $0.05 increments at or above $3.00."
        )
    return price


def validate_bracket_order(quantity, entry_price, take_profit_price, stop_price):
    quantity, entry = validate_order(quantity, entry_price)
    if entry < Decimal("0.02"):
        raise ValueError(
            "Entry price must be at least $0.02 to place a stop-loss trigger below entry."
        )
    take_profit = _validate_exit_price(take_profit_price, "Take-profit price")
    stop = _validate_exit_price(stop_price, "Stop-loss trigger")

    if stop >= entry:
        raise ValueError("Stop-loss trigger must be below the entry limit price.")
    if take_profit <= entry:
        raise ValueError("Take-profit price must be above the entry limit price.")
    return quantity, entry, take_profit, stop


def suggest_bracket_exit_prices(entry_price):
    """Suggest editable exits near +30% / -20%, rounded to whole cents."""
    return calculate_bracket_exit_prices(entry_price, 30, 20)


def _validate_percentage(value, label):
    try:
        percentage = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be a valid percentage.") from None
    if not percentage.is_finite() or percentage < Decimal("1") or percentage > Decimal("500"):
        raise ValueError(f"{label} must be between 1% and 500%.")
    return percentage


def calculate_bracket_exit_prices(entry_price, take_profit_pct, stop_loss_pct):
    """Calculate long-option OCO exits, rounding outward to the option tick."""
    try:
        entry = Decimal(str(entry_price))
    except (InvalidOperation, ValueError):
        raise ValueError("Entry price must be a valid dollar amount.") from None
    if not entry.is_finite() or entry < Decimal("0.02") or entry > MAX_ORDER_PRICE:
        raise ValueError("Entry price must be between $0.02 and $2.00 for a bracket order.")
    if entry != entry.quantize(Decimal("0.01")):
        raise ValueError("Entry price must be in $0.01 increments.")

    profit_pct = _validate_percentage(take_profit_pct, "Take-profit percentage")
    loss_pct = _validate_percentage(stop_loss_pct, "Stop-loss percentage")
    tick = Decimal("0.01")
    take_profit_raw = entry * (Decimal("1") + profit_pct / Decimal("100"))
    stop_raw = entry * (Decimal("1") - loss_pct / Decimal("100"))
    take_profit = (
        (take_profit_raw / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    )
    stop = max(
        tick,
        (stop_raw / tick).to_integral_value(rounding=ROUND_FLOOR) * tick,
    )
    return take_profit, stop


def resolve_bracket_exit_plan(entry_price, plan, custom_take_profit_pct=None,
                              custom_stop_loss_pct=None):
    if not isinstance(plan, str):
        raise ValueError("Choose an OCO exit preset.")
    if plan == "custom":
        profit_pct = _validate_percentage(custom_take_profit_pct, "Take-profit percentage")
        loss_pct = _validate_percentage(custom_stop_loss_pct, "Stop-loss percentage")
        label = "Custom"
    else:
        preset = OCO_EXIT_PRESETS.get(plan)
        if preset is None:
            raise ValueError("Choose a valid OCO exit preset.")
        profit_pct = preset["take_profit_pct"]
        loss_pct = preset["stop_loss_pct"]
        label = preset["label"]
    take_profit, stop = calculate_bracket_exit_prices(
        entry_price, profit_pct, loss_pct)
    return take_profit, stop, profit_pct, loss_pct, label


def suggest_bracket_entry_price(reference_price):
    try:
        reference = Decimal(str(reference_price))
    except (InvalidOperation, ValueError):
        raise ValueError("No usable current option price is available for this contract.") from None
    if not reference.is_finite() or reference <= 0:
        raise ValueError("No usable current option price is available for this contract.")
    entry = min(
        MAX_ORDER_PRICE,
        max(Decimal("0.02"), reference.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
    )
    return entry


def select_bracket_entry_reference(quote, fallback_price=None):
    def positive_price(value):
        try:
            price = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None
        return price if price.is_finite() and price > 0 else None

    bid = positive_price(quote.get("bid"))
    ask = positive_price(quote.get("ask"))
    mark = positive_price(quote.get("mark"))
    last = positive_price(quote.get("last"))
    midpoint = None
    if bid is not None and ask is not None and ask >= bid:
        midpoint = (bid + ask) / Decimal("2")

    if mark is not None:
        if midpoint is None or bid <= mark <= ask:
            return mark
    if midpoint is not None:
        return midpoint
    if mark is not None:
        return mark
    if last is not None:
        return last
    if ask is not None:
        return ask
    return fallback_price


def build_buy_with_oco_exits(
        option_symbol, quantity, entry_price, take_profit_price, stop_price):
    quantity, entry, take_profit, stop = validate_bracket_order(
        quantity, entry_price, take_profit_price, stop_price)

    take_profit_order = option_sell_to_close_limit(
        option_symbol, quantity, format(take_profit, ".2f"))
    stop_order = (
        OrderBuilder()
        .set_session(Session.NORMAL)
        .set_duration(Duration.DAY)
        .set_order_type(OrderType.STOP)
        .set_stop_price(format(stop, ".2f"))
        .set_order_strategy_type(OrderStrategyType.SINGLE)
        .add_option_leg(OptionInstruction.SELL_TO_CLOSE, option_symbol, quantity)
    )
    exit_oco = one_cancels_other(take_profit_order, stop_order)
    entry_order = option_buy_to_open_limit(
        option_symbol, quantity, format(entry, ".2f"))
    return first_triggers_second(entry_order, exit_oco).build()
