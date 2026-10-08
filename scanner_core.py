"""
Shared scanning logic used by both the CLI (options_scanner.py) and the
web dashboard (app.py). Keeping this in one place means both stay in sync.
"""
import datetime as dt
import json
import math
import os
from zoneinfo import ZoneInfo

import config

CACHE_PATH = "baseline_cache.json"

# Reject any contract whose bid/ask spread (as a % of the mid price) is
# this or wider -- too wide usually means thin liquidity / bad fills.
MAX_SPREAD_PCT = 5.0

# Only consider contracts priced under this (current mark/last price).
MAX_OPTION_PRICE = 2.0

# Liquidity thresholds for the weekly trend scanner.
MIN_WEEKLY_OPTION_VOLUME = 100
MAX_WEEKLY_SPREAD_PCT = 5.0

# How many strikes on each side of the money to consider before giving up
# on finding one that passes the spread + price filters.
STRIKE_COUNT = 10

# Keep several nearby weekly contracts so the dashboard can show cheaper
# alternatives when the closest strike is above the weekly price limit.
MAX_WEEKLY_CANDIDATES_PER_SIDE = 5


def load_watchlist(path: str) -> list[str]:
    with open(path) as f:
        return [line.strip().upper() for line in f if line.strip() and not line.startswith("#")]


def _tz():
    return ZoneInfo(config.BASELINE_TIMEZONE)


def tz():
    """Public accessor for the configured timezone (America/Chicago by default)."""
    return _tz()


def _today_at(hour: int, minute: int) -> dt.datetime:
    now = dt.datetime.now(_tz())
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def baseline_datetime_today() -> dt.datetime:
    """Market-open baseline today: 8:30 AM America/Chicago (= 9:30 AM ET), CST/CDT auto-adjusted."""
    return _today_at(config.BASELINE_HOUR, config.BASELINE_MINUTE)


def noon_datetime_today() -> dt.datetime:
    """Second fixed baseline today: 12:00 PM America/Chicago."""
    return _today_at(config.NOON_HOUR, config.NOON_MINUTE)


BASELINE_LABEL = "8:30 AM open"
NOON_LABEL = "12:00 PM"


def active_baseline(now_dt: dt.datetime = None):
    """
    Single fixed baseline used all day: the 8:30 AM market-open price.
    Returns (baseline_dt, label). `now_dt` is accepted for backward
    compatibility with callers but no longer changes which baseline is used.
    """
    return baseline_datetime_today(), BASELINE_LABEL


def today_key() -> str:
    return dt.datetime.now(_tz()).strftime("%Y-%m-%d")


def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_json(path: str, data: dict):
    with open(path, "w") as f:
        json.dump(data, f)


def load_cache() -> dict:
    data = _load_json(CACHE_PATH)
    if data.get("date") != today_key():
        return {}
    return data


def save_cache(cache: dict):
    cache["date"] = today_key()
    _save_json(CACHE_PATH, cache)


def fetch_minute_candles(client, option_symbol: str, day: dt.datetime, end: dt.datetime = None):
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    if end is None:
        end = start + dt.timedelta(days=1)
    resp = client.get_price_history_every_minute(
        option_symbol,
        start_datetime=start,
        end_datetime=end,
    )
    if resp.status_code != 200:
        return []
    return resp.json().get("candles", [])


def fetch_baseline_price(client, option_symbol: str, target_dt: dt.datetime):
    """
    One-time, expensive lookup: minute candles for `option_symbol`, return
    the 'open' of the candle at/just-before target_dt.
    """
    start = target_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    # Search the whole trading day so illiquid contracts with no candle at
    # exactly 8:30 can use the first available candle as their fallback.
    end = start + dt.timedelta(days=1)

    resp = client.get_price_history_every_minute(
        option_symbol,
        start_datetime=start,
        end_datetime=end,
    )
    if resp.status_code != 200:
        return None

    candles = resp.json().get("candles", [])
    if not candles:
        return None
    return _candle_at_or_before(candles, target_dt).get("open")


def _candle_at_or_before(candles: list, target_dt: dt.datetime):
    target_ms = int(target_dt.timestamp() * 1000)
    best = None
    for c in candles:
        if c["datetime"] <= target_ms:
            best = c
        else:
            break
    return best if best is not None else candles[0]


def spread_pct(contract: dict):
    """Bid/ask spread as a percentage of the mid price. None if quote is unusable."""
    bid = contract.get("bid") or 0
    ask = contract.get("ask") or 0
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    return round((ask - bid) / mid * 100, 2)


def candidate_strikes_by_side(chain_json: dict, today_str: str):
    """
    Find the nearest expiration date that is AFTER today (skips same-day /
    0DTE expirations), then return, per side (CALL/PUT), the list of
    contracts at that expiration sorted by how close their strike is to
    the underlying price (closest first).
    """
    underlying_price = (chain_json.get("underlyingPrice")
                         or (chain_json.get("underlying") or {}).get("mark")
                         or 0)

    exp_dates = set()
    for side in ("callExpDateMap", "putExpDateMap"):
        exp_dates.update((chain_json.get(side) or {}).keys())

    future_dates = [k for k in exp_dates if k.split(":")[0] > today_str]
    if not future_dates:
        return {}, underlying_price

    nearest_key = min(future_dates, key=lambda k: k.split(":")[0])

    by_side = {}
    for side, put_call in (("callExpDateMap", "CALL"), ("putExpDateMap", "PUT")):
        strikes_map = (chain_json.get(side) or {}).get(nearest_key) or {}
        strike_keys = sorted(
            strikes_map.keys(),
            key=lambda s: abs(float(s) - underlying_price) if underlying_price else 0,
        )
        contracts = []
        for sk in strike_keys:
            contract_list = strikes_map[sk]
            if contract_list:
                contracts.append(contract_list[0])
        by_side[put_call] = contracts

    return by_side, underlying_price


def _next_weekly_expiration(client, symbol: str, today_str: str):
    """Return the nearest future weekly expiration date for a symbol."""
    resp = client.get_option_expiration_chain(symbol)
    if resp.status_code != 200:
        return None
    expirations = []
    for item in resp.json().get("expirationList", []) or []:
        expiration = str(item.get("expirationDate") or "").split("T")[0]
        if item.get("expirationType") == "W" and expiration and expiration > today_str:
            expirations.append(expiration)
    return min(expirations) if expirations else None


def _weekly_spread_candidates(chain_json: dict, expiration: str, underlying_price: float):
    """Get nearby weekly calls and puts passing the weekly scanner filters."""
    result = []
    expiration_key = next(
        (key for key in set((chain_json.get("callExpDateMap") or {}).keys()) |
         set((chain_json.get("putExpDateMap") or {}).keys())
         if key.split(":")[0] == expiration),
        None,
    )
    if not expiration_key:
        return result

    for side, put_call in (("callExpDateMap", "CALL"), ("putExpDateMap", "PUT")):
        strikes_map = (chain_json.get(side) or {}).get(expiration_key) or {}
        strikes = sorted(
            strikes_map.items(),
            key=lambda pair: abs(float(pair[0]) - underlying_price) if underlying_price else 0,
        )
        side_candidates = 0
        for _strike, contracts in strikes:
            if not contracts:
                continue
            contract = contracts[0]
            spread = spread_pct(contract)
            current_price = contract.get("mark") or contract.get("last") or contract.get("bid") or 0
            try:
                volume = float(contract.get("totalVolume") or 0)
            except (TypeError, ValueError):
                continue
            if (spread is not None and spread < MAX_WEEKLY_SPREAD_PCT
                    and 0 < current_price < 2
                    and volume >= MIN_WEEKLY_OPTION_VOLUME):
                result.append({
                    "underlying": contract.get("underlyingSymbol") or "",
                    "option_symbol": contract.get("symbol"),
                    "put_call": put_call,
                    "strike": contract.get("strikePrice"),
                    "expiration": contract.get("expirationDate") or expiration,
                    "current_price": current_price,
                    "bid": contract.get("bid"),
                    "ask": contract.get("ask"),
                    "spread_pct": spread,
                    "volume": contract.get("totalVolume", 0) or 0,
                    "open_interest": contract.get("openInterest", 0),
                    "delta": contract.get("delta"),
                    "days_to_expiration": contract.get("daysToExpiration"),
                })
                side_candidates += 1
                if side_candidates >= MAX_WEEKLY_CANDIDATES_PER_SIDE:
                    break
    return result


def _attach_weekly_intraday_changes(client, candidates: list[dict], now_dt: dt.datetime, cache: dict):
    """Add option percentage changes for the weekly intraday windows."""
    eight_thirty = now_dt.replace(hour=8, minute=30, second=0, microsecond=0)
    nine_thirty = now_dt.replace(hour=9, minute=30, second=0, microsecond=0)
    ten_thirty = now_dt.replace(hour=10, minute=30, second=0, microsecond=0)
    nine_am = now_dt.replace(hour=9, minute=0, second=0, microsecond=0)
    noon = now_dt.replace(hour=12, minute=0, second=0, microsecond=0)
    three_pm = now_dt.replace(hour=15, minute=0, second=0, microsecond=0)

    for candidate in candidates:
        price_at_830 = None
        price_at_nine = None
        price_at_nine_thirty = None
        price_at_ten_thirty = None
        price_at_noon = None
        price_at_three = None
        if now_dt >= eight_thirty:
            price_at_830 = fetch_price_at_time_today(
                client, candidate["option_symbol"], eight_thirty, cache)
        if now_dt >= nine_am:
            price_at_nine = fetch_price_at_time_today(
                client, candidate["option_symbol"], nine_am, cache)
        if now_dt >= nine_thirty:
            price_at_nine_thirty = fetch_price_at_time_today(
                client, candidate["option_symbol"], nine_thirty, cache)
        if now_dt >= ten_thirty:
            price_at_ten_thirty = fetch_price_at_time_today(
                client, candidate["option_symbol"], ten_thirty, cache)
        if now_dt >= noon:
            price_at_noon = fetch_price_at_time_today(
                client, candidate["option_symbol"], noon, cache)
        if now_dt >= three_pm:
            price_at_three = fetch_price_at_time_today(
                client, candidate["option_symbol"], three_pm, cache)

        candidate["option_price_at_830am"] = price_at_830
        candidate["option_price_at_9am"] = price_at_nine
        candidate["option_price_at_930am"] = price_at_nine_thirty
        candidate["option_price_at_1030am"] = price_at_ten_thirty
        candidate["option_price_at_noon"] = price_at_noon
        candidate["option_price_at_3pm"] = price_at_three
        candidate["option_pct_830_to_930"] = None
        candidate["option_pct_930_to_1030"] = None
        candidate["option_pct_1030_to_noon"] = None
        candidate["option_pct_9am_to_noon"] = None
        candidate["option_pct_noon_to_3pm"] = None

        if price_at_830 not in (None, 0):
            reference_nine_thirty = price_at_nine_thirty if price_at_nine_thirty is not None else candidate.get("current_price")
            if reference_nine_thirty not in (None, 0):
                candidate["option_pct_830_to_930"] = round(
                    (reference_nine_thirty - price_at_830) / price_at_830 * 100, 2)

        if price_at_nine_thirty not in (None, 0):
            reference_ten_thirty = price_at_ten_thirty if price_at_ten_thirty is not None else candidate.get("current_price")
            if reference_ten_thirty not in (None, 0):
                candidate["option_pct_930_to_1030"] = round(
                    (reference_ten_thirty - price_at_nine_thirty) / price_at_nine_thirty * 100, 2)

        if price_at_ten_thirty not in (None, 0):
            reference_noon = price_at_noon if price_at_noon is not None else candidate.get("current_price")
            if reference_noon not in (None, 0):
                candidate["option_pct_1030_to_noon"] = round(
                    (reference_noon - price_at_ten_thirty) / price_at_ten_thirty * 100, 2)

        if price_at_nine not in (None, 0):
            reference_noon = price_at_noon if price_at_noon is not None else candidate.get("current_price")
            if reference_noon not in (None, 0):
                candidate["option_pct_9am_to_noon"] = round(
                    (reference_noon - price_at_nine) / price_at_nine * 100, 2)

        if price_at_noon not in (None, 0):
            # Until 3:00 PM Central, show the contract's live mark as the current
            # "3:00 PM" reference so the afternoon window stays visible in progress.
            reference_three = price_at_three if price_at_three is not None else candidate.get("current_price")
            candidate["option_price_at_3pm"] = reference_three
            if reference_three not in (None, 0):
                candidate["option_pct_noon_to_3pm"] = round(
                    (reference_three - price_at_noon) / price_at_noon * 100, 2)


def build_candidate(symbol: str, c: dict, current_price: float, baseline_price, baseline_label: str,
                     noon_price=None):
    change_since_baseline = None
    pct_since_baseline = None
    if baseline_price not in (None, 0):
        change_since_baseline = round(current_price - baseline_price, 2)
        pct_since_baseline = round(change_since_baseline / baseline_price * 100, 2)

    change_since_noon = None
    pct_since_noon = None
    if noon_price not in (None, 0):
        change_since_noon = round(current_price - noon_price, 2)
        pct_since_noon = round(change_since_noon / noon_price * 100, 2)

    # Show the 8:30 AM-to-now move before noon, and keep the fixed
    # 8:30 AM-to-12:00 PM delta after noon has actually occurred.
    change_830_to_noon = None
    pct_830_to_noon = None
    if baseline_price not in (None, 0):
        reference_price = noon_price if noon_price not in (None, 0) else current_price
        change_830_to_noon = round(reference_price - baseline_price, 2)
        pct_830_to_noon = round(change_830_to_noon / baseline_price * 100, 2)

    return {
        "underlying": symbol,
        "option_symbol": c["symbol"],
        "put_call": c.get("putCall"),
        "strike": c.get("strikePrice"),
        "expiration": c.get("expirationDate") or c.get("expDate"),
        "current_price": current_price,
        "price_at_baseline": baseline_price,
        "baseline_label": baseline_label,
        "change_since_baseline": change_since_baseline,
        "pct_since_baseline": pct_since_baseline,
        # 12:00 PM fixed baseline -- None until that time has passed today
        "price_at_noon": noon_price,
        "noon_label": NOON_LABEL,
        "change_since_noon": change_since_noon,
        "pct_since_noon": pct_since_noon,
        "change_830_to_noon": change_830_to_noon,
        "pct_830_to_noon": pct_830_to_noon,
        "bid": c.get("bid"),
        "ask": c.get("ask"),
        "spread_pct": spread_pct(c),
        "volume": c.get("totalVolume", 0) or 0,
        "open_interest": c.get("openInterest", 0),
        # filled in later by attach_4h_change()
        "price_4h_ago": None,
        "change_4h": None,
        "pct_4h": None,
    }


def fetch_price_at_time_today(client, option_symbol: str, target_dt: dt.datetime, rolling_cache: dict):
    """
    Live rolling lookup: price of `option_symbol` at/just-before target_dt,
    using today's minute candles. Cached per exact target minute so repeated
    contracts (or a re-render within the same minute) don't refetch.
    """
    cache_key = f"{option_symbol}@{target_dt.strftime('%Y-%m-%d %H:%M')}"
    if cache_key in rolling_cache:
        return rolling_cache[cache_key]

    candles = fetch_minute_candles(client, option_symbol, target_dt)
    price = None
    if candles:
        price = _candle_at_or_before(candles, target_dt).get("open")

    rolling_cache[cache_key] = price
    return price


def fetch_price_at_ema_crossover(client, option_symbol: str, crossover_ms: int, cache: dict):
    """Fetch an option's 5-minute close at the latest EMA crossover."""
    if crossover_ms is None:
        return None
    cache_key = f"ema-cross|{option_symbol}@{crossover_ms}"
    if cache_key in cache:
        return cache[cache_key]

    crossover_dt = dt.datetime.fromtimestamp(crossover_ms / 1000, tz=_tz())
    start = crossover_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = crossover_dt + dt.timedelta(minutes=5)
    resp = client.get_price_history_every_five_minutes(
        option_symbol,
        start_datetime=start,
        end_datetime=end,
    )
    price = None
    if resp.status_code == 200:
        candles = resp.json().get("candles", [])
        if candles:
            candle = _candle_at_or_before(candles, crossover_dt)
            price = candle.get("close") or candle.get("open")

    cache[cache_key] = price
    return price


def attach_ema_crossover_change(client, candidate: dict, ema_trend: dict, cache: dict):
    """Attach the selected option's change since the latest EMA crossover."""
    crossover_ms = ema_trend.get("ema_crossover_datetime")
    price_at_cross = fetch_price_at_ema_crossover(
        client, candidate["option_symbol"], crossover_ms, cache)
    candidate["option_price_at_crossover"] = price_at_cross
    candidate["option_change_since_crossover"] = None
    candidate["option_pct_since_crossover"] = None
    if price_at_cross not in (None, 0):
        change = round(candidate["current_price"] - price_at_cross, 2)
        candidate["option_change_since_crossover"] = change
        candidate["option_pct_since_crossover"] = round(change / price_at_cross * 100, 2)


def attach_4h_change(client, candidate: dict, now_dt: dt.datetime, rolling_cache: dict, hours: int = 4):
    """Fill in the rolling N-hour change (default 4h) for an already-built candidate dict, in place."""
    target_dt = now_dt - dt.timedelta(hours=hours)
    price_then = fetch_price_at_time_today(client, candidate["option_symbol"], target_dt, rolling_cache)

    candidate["price_4h_ago"] = price_then
    if price_then not in (None, 0):
        chg = round(candidate["current_price"] - price_then, 2)
        candidate["change_4h"] = chg
        candidate["pct_4h"] = round(chg / price_then * 100, 2)


def _fetch_cached_baseline(client, opt_symbol: str, target_dt: dt.datetime, label: str, cache: dict):
    """One-time-per-day lookup of a fixed baseline price, cached per (contract, label)."""
    cache_key = f"{opt_symbol}|{label}"
    if cache_key in cache:
        return cache[cache_key]
    price = fetch_baseline_price(client, opt_symbol, target_dt)
    if price not in (None, 0):
        cache[cache_key] = price
    return price


def pick_best_liquid_contract(client, symbol: str, side_contracts: list, now_dt: dt.datetime, cache: dict):
    """
    Walk `side_contracts` (already sorted closest-strike-first) and return
    the first one whose bid/ask spread passes MAX_SPREAD_PCT AND whose
    current price is under MAX_OPTION_PRICE, along with its computed
    candidate dict. Fetches both fixed baselines -- the 8:30 AM open and,
    once it has occurred, the 12:00 PM midday price -- each cached per
    contract so they're only looked up once per day.
    """
    baseline_dt = baseline_datetime_today()
    noon_dt = noon_datetime_today()

    for c in side_contracts:
        sp = spread_pct(c)
        if sp is None or sp >= MAX_SPREAD_PCT:
            continue  # too wide / no quote, try next strike out

        current_price = c.get("mark") or c.get("last") or c.get("bid") or 0
        if current_price <= 0 or current_price >= MAX_OPTION_PRICE:
            continue  # too expensive, try next strike out

        opt_symbol = c["symbol"]
        baseline_price = _fetch_cached_baseline(client, opt_symbol, baseline_dt, BASELINE_LABEL, cache)

        noon_price = None
        if now_dt >= noon_dt:
            noon_price = _fetch_cached_baseline(client, opt_symbol, noon_dt, NOON_LABEL, cache)

        return build_candidate(symbol, c, current_price, baseline_price, BASELINE_LABEL, noon_price)

    return None


def fetch_underlying_prices(client, symbols: list[str], batch_size: int = 100) -> dict:
    """Fetch stock quotes in batches for inexpensive pre-scan price filtering."""
    if batch_size < 1:
        raise ValueError("Quote batch size must be positive.")
    prices = {}
    for start in range(0, len(symbols), batch_size):
        batch = symbols[start:start + batch_size]
        response = client.get_quotes(batch)
        if response.status_code != 200:
            raise RuntimeError(
                f"Schwab stock quote lookup failed (HTTP {response.status_code}).")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Schwab returned an unexpected stock quote response.")
        for symbol in batch:
            entry = payload.get(symbol) or payload.get(symbol.strip()) or {}
            if not isinstance(entry, dict):
                prices[symbol] = None
                continue
            quote = entry.get("quote") or {}
            price = None
            for field in ("lastPrice", "mark"):
                try:
                    candidate = float(quote.get(field))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(candidate) and candidate > 0:
                    price = candidate
                    break
            prices[symbol] = price
    return prices


def scan_symbol(client, symbol: str, cache: dict, rolling_cache: dict = None, now_dt: dt.datetime = None,
                 ema_cache: dict = None):
    """
    One fast pass for `symbol`:
      - pulls the option chain
      - finds the nearest expiration that is NOT today
      - for each side (call/put), picks the closest-to-money strike that
        passes the spread filter AND is priced under MAX_OPTION_PRICE
      - computes change since the fixed 8:30 AM market-open baseline, and
        (once it has occurred) change since the fixed 12:00 PM baseline
      - if rolling_cache is provided, also attaches the rolling 4-hour
        change (with %) for the same contract
      - if ema_cache is provided, also attaches the underlying's cached
        EMA8/EMA21 trend to each contract (and to the outcome itself)

    Returns: {"error": str|None, "call": dict|None, "put": dict|None,
              "ema8": float|None, "ema21": float|None, "ema_bullish": bool|None}
    """
    now = now_dt or dt.datetime.now(_tz())

    resp = client.get_option_chain(
        symbol,
        strike_count=STRIKE_COUNT,
        include_underlying_quote=True,
    )
    chain_error = None
    if resp.status_code == 200:
        chain = resp.json()
    else:
        chain = {}
        chain_error = f"chain request failed: {resp.status_code}"
    today_str = today_key()
    by_side, _underlying_price = candidate_strikes_by_side(chain, today_str)

    ema_trend = get_ema_trend(client, symbol, ema_cache, now) if ema_cache is not None else None
    stock_history_cache = rolling_cache if rolling_cache is not None else {}
    stock_baseline_price = fetch_price_at_time_today(
        client, symbol, baseline_datetime_today(), stock_history_cache)
    stock_noon_price = None
    if now >= noon_datetime_today():
        stock_noon_price = fetch_price_at_time_today(
            client, symbol, noon_datetime_today(), stock_history_cache)

    stock_change_since_baseline = None
    stock_pct_since_baseline = None
    if _underlying_price and stock_baseline_price not in (None, 0):
        stock_change_since_baseline = round(_underlying_price - stock_baseline_price, 2)
        stock_pct_since_baseline = round(stock_change_since_baseline / stock_baseline_price * 100, 2)

    stock_change_since_noon = None
    stock_pct_since_noon = None
    if _underlying_price and stock_noon_price not in (None, 0):
        stock_change_since_noon = round(_underlying_price - stock_noon_price, 2)
        stock_pct_since_noon = round(stock_change_since_noon / stock_noon_price * 100, 2)

    stock_change_830_to_noon = None
    stock_pct_830_to_noon = None
    if stock_baseline_price not in (None, 0):
        stock_reference_price = stock_noon_price if stock_noon_price not in (None, 0) else _underlying_price
        stock_change_830_to_noon = round(stock_reference_price - stock_baseline_price, 2)
        stock_pct_830_to_noon = round(stock_change_830_to_noon / stock_baseline_price * 100, 2)

    trend_status = classify_stock_trend(
        _underlying_price,
        ema_trend["ema8"] if ema_trend else None,
        ema_trend["ema21"] if ema_trend else None,
        stock_pct_since_baseline,
    )
    day_trade_context = ema_trend.get("day_trade_context", {}) if ema_trend else {}
    day_trade_status = classify_day_trade_setup(
        day_trade_context,
        now,
        day_trade_context.get("stock_price"),
        ema_trend["ema8"] if ema_trend else None,
        ema_trend["ema21"] if ema_trend else None,
        day_trade_context.get("session_pct"),
    )

    weekly_spread = []
    try:
        next_weekly = _next_weekly_expiration(client, symbol, today_str)
        if next_weekly:
            weekly_spread = _weekly_spread_candidates(chain, next_weekly, _underlying_price)
            for candidate in weekly_spread:
                candidate["underlying"] = symbol
                candidate["weekly_expiration"] = next_weekly
            _attach_weekly_intraday_changes(
                client, weekly_spread, now, rolling_cache if rolling_cache is not None else {})
    except Exception:
        weekly_spread = []

    if not by_side or (not by_side.get("CALL") and not by_side.get("PUT")):
        return {"error": chain_error or "no expirations found after today", "call": None, "put": None,
                "ema8": ema_trend["ema8"] if ema_trend else None,
                "ema21": ema_trend["ema21"] if ema_trend else None,
                "ema50": ema_trend["ema50"] if ema_trend else None,
                "trend_ride_status": ema_trend["trend_ride_status"] if ema_trend else "—",
                "trend_ride": ema_trend["trend_ride"] if ema_trend else False,
                "ema_bullish": ema_trend["ema_bullish"] if ema_trend else None,
                "stock_current_price": _underlying_price,
                "stock_change_since_baseline": stock_change_since_baseline,
                "stock_pct_since_baseline": stock_pct_since_baseline,
                "trend_status": trend_status,
                "day_trade_status": day_trade_status,
                "day_trade_context": day_trade_context,
                "weekly_spread": weekly_spread}

    call = pick_best_liquid_contract(client, symbol, by_side.get("CALL", []), now, cache)
    put = pick_best_liquid_contract(client, symbol, by_side.get("PUT", []), now, cache)

    if rolling_cache is not None:
        if call:
            attach_4h_change(client, call, now, rolling_cache)
        if put:
            attach_4h_change(client, put, now, rolling_cache)

    def attach_stock_changes(candidate):
        candidate.update({
            "stock_price_at_baseline": stock_baseline_price,
            "stock_price_at_noon": stock_noon_price,
            "stock_current_price": _underlying_price,
            "stock_change_since_baseline": stock_change_since_baseline,
            "stock_pct_since_baseline": stock_pct_since_baseline,
            "stock_change_since_noon": stock_change_since_noon,
            "stock_pct_since_noon": stock_pct_since_noon,
            "stock_change_830_to_noon": stock_change_830_to_noon,
            "stock_pct_830_to_noon": stock_pct_830_to_noon,
        })

    for candidate in (call, put):
        if candidate:
            attach_stock_changes(candidate)

    if ema_trend:
        ema_change_cache = rolling_cache if rolling_cache is not None else {}
        crossover_price = ema_trend.get("ema_crossover_price")
        stock_change = None
        stock_pct_change = None
        if _underlying_price and crossover_price not in (None, 0):
            stock_change = round(_underlying_price - crossover_price, 2)
            stock_pct_change = round(stock_change / crossover_price * 100, 2)
        if call:
            call.update(ema_trend)
            attach_ema_crossover_change(client, call, ema_trend, ema_change_cache)
            call["stock_price"] = _underlying_price
            call["stock_price_at_crossover"] = crossover_price
            call["stock_change_since_crossover"] = stock_change
            call["stock_pct_since_crossover"] = stock_pct_change
        if put:
            put.update(ema_trend)
            attach_ema_crossover_change(client, put, ema_trend, ema_change_cache)
            put["stock_price"] = _underlying_price
            put["stock_price_at_crossover"] = crossover_price
            put["stock_change_since_crossover"] = stock_change
            put["stock_pct_since_crossover"] = stock_pct_change

    error = None
    if call is None and put is None:
        error = "no call/put under $%.2f near the money passed the spread filter" % MAX_OPTION_PRICE

    return {
        "error": error,
        "call": call,
        "put": put,
        "ema8": ema_trend["ema8"] if ema_trend else None,
        "ema21": ema_trend["ema21"] if ema_trend else None,
        "ema50": ema_trend["ema50"] if ema_trend else None,
        "trend_ride_status": ema_trend["trend_ride_status"] if ema_trend else "—",
        "trend_ride": ema_trend["trend_ride"] if ema_trend else False,
        "ema_bullish": ema_trend["ema_bullish"] if ema_trend else None,
        "stock_current_price": _underlying_price,
        "stock_change_since_baseline": stock_change_since_baseline,
        "stock_pct_since_baseline": stock_pct_since_baseline,
        "trend_status": trend_status,
        "day_trade_status": day_trade_status,
        "day_trade_context": day_trade_context,
        "weekly_spread": weekly_spread,
    }


def run_pass_full(client, tickers: list[str], cache: dict, rolling_cache: dict = None,
                   ema_cache: dict = None, on_symbol=None, underlying_prices=None,
                   underlying_price_range=None) -> dict:
    """Scan tickers with optional pre-fetched share-price bounds and progress reporting."""
    now_dt = dt.datetime.now(_tz())
    per_symbol = {}
    for symbol in tickers:
        price = (underlying_prices or {}).get(symbol)
        if (underlying_price_range is not None and price is not None
                and not underlying_price_range[0] <= price <= underlying_price_range[1]):
            outcome = {
                "error": (
                    f"Underlying stock price ${price:.2f} is outside "
                    f"${underlying_price_range[0]:.2f}–"
                    f"${underlying_price_range[1]:.2f} weekly scanner range."
                ),
                "call": None,
                "put": None,
                "weekly_spread": [],
                "stock_current_price": price,
            }
        else:
            outcome = scan_symbol(client, symbol, cache, rolling_cache, now_dt, ema_cache)
        per_symbol[symbol] = outcome
        if on_symbol is not None:
            on_symbol(symbol, outcome)
    return per_symbol


def run_pass(client, tickers: list[str], cache: dict) -> list[dict]:
    """CLI convenience: flat list of contracts (call+put) that are up since the active baseline."""
    results = []
    for symbol in tickers:
        outcome = scan_symbol(client, symbol, cache)
        for contract in (outcome["call"], outcome["put"]):
            if contract and contract["change_since_baseline"] is not None and contract["change_since_baseline"] > 0:
                results.append(contract)
    return results


def fetch_today_history(client, option_symbol: str):
    """
    Today's option history aggregated into 5-minute OHLC bars for charting.
    Schwab's five-minute endpoint is documented for stocks/ETFs, so option
    candles use the minute endpoint and are grouped locally.
    """
    now = dt.datetime.now(_tz())
    start = baseline_datetime_today()
    resp = client.get_price_history_every_minute(
        option_symbol,
        start_datetime=start,
        end_datetime=now,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Schwab history request failed: HTTP {resp.status_code}")
    candles = resp.json().get("candles", [])
    if not candles:
        fallback_start = now - dt.timedelta(days=1)
        fallback = client.get_price_history_every_minute(
            option_symbol,
            start_datetime=fallback_start,
            end_datetime=now,
        )
        if fallback.status_code != 200:
            raise RuntimeError(f"Schwab history request failed: HTTP {fallback.status_code}")
        candles = fallback.json().get("candles", [])
    bars = {}
    for candle in candles:
        timestamp = candle["datetime"]
        bucket = timestamp - (timestamp % (5 * 60 * 1000))
        close = candle.get("close") or candle.get("open")
        if close is None:
            continue
        bar = bars.setdefault(bucket, {
            "time_ms": bucket,
            "open": candle.get("open") or close,
            "high": close,
            "low": close,
            "close": close,
        })
        bar["high"] = max(bar["high"], candle.get("high") or close)
        bar["low"] = min(bar["low"], candle.get("low") or close)
        bar["close"] = close
    for bar in bars.values():
        bar["price"] = bar["close"]
    return list(bars.values())


# --- EMA8 / EMA21 trend filter -----------------------------------------

def fetch_5min_candles(client, symbol: str, days: int = None):
    """5-minute OHLC candles for `symbol` going back `days` calendar days."""
    days = days or config.EMA_LOOKBACK_DAYS
    end = dt.datetime.now(_tz())
    start = end - dt.timedelta(days=days)
    resp = client.get_price_history_every_five_minutes(
        symbol,
        start_datetime=start,
        end_datetime=end,
    )
    if resp.status_code != 200:
        return []
    return resp.json().get("candles", [])


def compute_ema(values: list, period: int):
    """Standard EMA over `values` (oldest first), seeded with an SMA of the
    first `period` values. Returns None if there isn't enough history."""
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def classify_stock_trend(stock_price, ema_fast, ema_slow, session_pct):
    """Confirm direction only when price, EMA alignment, and session move agree."""
    if any(value is None for value in (stock_price, ema_fast, ema_slow, session_pct)):
        return "MIXED"
    if stock_price > ema_fast > ema_slow and session_pct > 0:
        return "BULLISH"
    if stock_price < ema_fast < ema_slow and session_pct < 0:
        return "BEARISH"
    return "MIXED"


def is_regular_market_hours(now_dt=None):
    now_dt = now_dt or dt.datetime.now(_tz())
    now_dt = now_dt.astimezone(_tz())
    market_open = now_dt.replace(hour=8, minute=30, second=0, microsecond=0)
    market_close = now_dt.replace(hour=15, minute=0, second=0, microsecond=0)
    return now_dt.weekday() < 5 and market_open <= now_dt < market_close


def classify_option_momentum(candles, now_dt=None, window_minutes=10):
    """Measure the option's latest ten-minute move using regular-session candles."""
    now_dt = (now_dt or dt.datetime.now(_tz())).astimezone(_tz())
    sessions = {}
    for candle in candles:
        timestamp = candle.get("datetime")
        close = candle.get("close")
        if timestamp is None or close is None:
            continue
        candle_dt = dt.datetime.fromtimestamp(timestamp / 1000, tz=_tz())
        market_open = candle_dt.replace(hour=8, minute=30, second=0, microsecond=0)
        market_close = candle_dt.replace(hour=15, minute=0, second=0, microsecond=0)
        if (candle_dt.weekday() >= 5 or candle_dt < market_open or candle_dt >= market_close or
                candle_dt + dt.timedelta(minutes=1) > now_dt):
            continue
        sessions.setdefault(candle_dt.date(), []).append((candle_dt, float(close)))

    if not sessions:
        return {"status": "WAITING", "change": None, "data_as_of": None}

    session_date = max(sessions)
    session = sorted(sessions[session_date])
    latest_dt, latest_price = session[-1]
    target_dt = latest_dt - dt.timedelta(minutes=window_minutes)
    baseline = next((item for item in reversed(session) if item[0] <= target_dt), None)
    if baseline is None or baseline[1] <= 0:
        return {
            "status": "WAITING",
            "change": None,
            "data_as_of": latest_dt.isoformat(),
        }

    change = round(latest_price - baseline[1], 4)
    threshold = max(0.02, latest_price * 0.02)
    status = "RISING" if change >= threshold else (
        "FALLING" if change <= -threshold else "SIDEWAYS"
    )
    return {
        "status": status,
        "change": change,
        "data_as_of": latest_dt.isoformat(),
    }


def _opening_range_context(candles, now_dt):
    now_dt = now_dt.astimezone(_tz())
    sessions = {}
    for candle in candles:
        timestamp = candle.get("datetime")
        if timestamp is None:
            continue
        candle_dt = dt.datetime.fromtimestamp(timestamp / 1000, tz=_tz())
        session_open = candle_dt.replace(hour=8, minute=30, second=0, microsecond=0)
        regular_close = candle_dt.replace(hour=15, minute=0, second=0, microsecond=0)
        if (candle_dt.weekday() >= 5 or candle_dt < session_open or
                candle_dt >= regular_close or candle_dt + dt.timedelta(minutes=5) > now_dt):
            continue
        high = candle.get("high")
        low = candle.get("low")
        close = candle.get("close")
        volume = candle.get("volume")
        if high is None or low is None or close is None or volume is None:
            continue
        sessions.setdefault(candle_dt.date(), []).append({
            "time": candle_dt,
            "open": float(candle.get("open") or close),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": float(volume),
        })
    if not sessions:
        return {
            "session_date": None,
            "data_as_of": None,
            "stock_price": None,
            "stock_ema20": None,
            "stock_ema20_relation": None,
            "stock_momentum": None,
            "stock_momentum_change": None,
            "stock_momentum_baseline_10m": None,
            "session_pct": None,
            "stock_pct_1h": None,
            "stock_pct_830_to_noon": None,
            "stock_pct_noon_to_3": None,
            "stock_pct_930_to_3": None,
            "stock_pct_930_to_noon": None,
            "stock_pct_1d": None,
            "stock_baseline_1h": None,
            "stock_baseline_830": None,
            "stock_baseline_noon": None,
            "stock_baseline_930": None,
            "vwap": None,
            "opening_range_high": None,
            "opening_range_low": None,
            "opening_range_ready": False,
            "long_retest_confirmed": False,
            "short_retest_confirmed": False,
        }

    session_date = max(sessions)
    completed = sorted(sessions[session_date], key=lambda candle: candle["time"])
    session_open = dt.datetime.combine(session_date, dt.time(8, 30), tzinfo=_tz())
    regular_close = dt.datetime.combine(session_date, dt.time(15, 0), tzinfo=_tz())
    volume_total = sum(candle["volume"] for candle in completed)
    vwap = None
    if volume_total > 0:
        vwap = sum(
            ((candle["high"] + candle["low"] + candle["close"]) / 3) * candle["volume"]
            for candle in completed
        ) / volume_total

    opening_range_bars = [
        candle for candle in completed
        if session_open <= candle["time"] < session_open + dt.timedelta(minutes=30)
    ]
    expected_opening_times = {
        session_open + dt.timedelta(minutes=5 * index) for index in range(6)
    }
    actual_opening_times = {candle["time"] for candle in opening_range_bars}
    opening_range_ready = (
        now_dt >= session_open + dt.timedelta(minutes=30) and
        max(candle["time"] for candle in completed) >= session_open + dt.timedelta(minutes=25) and
        actual_opening_times == expected_opening_times
    )
    session_open_price = next(
        (candle["open"] for candle in opening_range_bars if candle["time"] == session_open),
        None,
    )
    stock_price = completed[-1]["close"]
    latest_time = completed[-1]["time"]
    ema20_values = _ema_series([candle["close"] for candle in completed], 20)
    stock_ema20 = ema20_values[-1] if ema20_values else None
    stock_ema20_relation = (
        ("ABOVE" if stock_price > stock_ema20 else
         "BELOW" if stock_price < stock_ema20 else "AT")
        if stock_ema20 is not None else None
    )

    def baseline_price_from(target_time):
        baseline = next(
            (candle for candle in reversed(completed)
             if candle["time"] <= target_time),
            None,
        )
        return baseline["close"] if baseline and baseline["close"] > 0 else None

    hourly_baseline = baseline_price_from(latest_time - dt.timedelta(hours=1))
    ten_minute_baseline = baseline_price_from(latest_time - dt.timedelta(minutes=10))
    ten_minute_change = (
        round(stock_price - ten_minute_baseline, 4)
        if ten_minute_baseline is not None else None
    )
    stock_momentum = (
        "UP" if ten_minute_change > 0 else
        "DOWN" if ten_minute_change < 0 else
        "FLAT" if ten_minute_change is not None else None
    )
    noon_time = session_open + dt.timedelta(hours=3, minutes=30)
    noon_candle = next(
        (candle for candle in completed if candle["time"] >= noon_time),
        None,
    )
    noon_baseline = noon_candle["open"] if noon_candle else None
    nine_thirty_time = session_open + dt.timedelta(hours=1)
    nine_thirty_candle = next(
        (candle for candle in completed if candle["time"] == nine_thirty_time),
        None,
    )
    nine_thirty_baseline = nine_thirty_candle["open"] if nine_thirty_candle else None
    hourly_pct = (
        round((stock_price - hourly_baseline) / hourly_baseline * 100, 2)
        if hourly_baseline is not None else None
    )
    session_pct = (
        round((stock_price - session_open_price) / session_open_price * 100, 2)
        if session_open_price else None
    )
    morning_end_price = (
        noon_baseline if noon_baseline is not None else stock_price
    )
    morning_pct = (
        round((morning_end_price - session_open_price) / session_open_price * 100, 2)
        if session_open_price else None
    )
    afternoon_pct = (
        round((stock_price - noon_baseline) / noon_baseline * 100, 2)
        if noon_baseline is not None and noon_baseline > 0 and latest_time >= noon_time else None
    )
    nine_thirty_pct = (
        round((stock_price - nine_thirty_baseline) / nine_thirty_baseline * 100, 2)
        if (nine_thirty_baseline is not None and nine_thirty_baseline > 0
            and latest_time >= nine_thirty_time) else None
    )
    nine_thirty_to_noon_pct = None
    if (nine_thirty_baseline is not None and nine_thirty_baseline > 0
            and latest_time >= nine_thirty_time):
        end_price = noon_baseline if noon_baseline is not None else stock_price
        nine_thirty_to_noon_pct = round(
            (end_price - nine_thirty_baseline) / nine_thirty_baseline * 100, 2)
    data_as_of = min(completed[-1]["time"] + dt.timedelta(minutes=5), regular_close)
    if not opening_range_ready:
        return {
            "session_date": session_date.isoformat(),
            "data_as_of": data_as_of.isoformat(),
            "stock_price": round(stock_price, 4),
            "stock_ema20": round(stock_ema20, 4) if stock_ema20 is not None else None,
            "stock_ema20_relation": stock_ema20_relation,
            "stock_momentum": stock_momentum,
            "stock_momentum_change": ten_minute_change,
            "stock_momentum_baseline_10m": ten_minute_baseline,
            "session_pct": session_pct,
            "stock_pct_1h": hourly_pct,
            "stock_pct_830_to_noon": morning_pct,
            "stock_pct_noon_to_3": afternoon_pct,
            "stock_pct_930_to_3": nine_thirty_pct,
            "stock_pct_930_to_noon": nine_thirty_to_noon_pct,
            "stock_pct_1d": session_pct,
            "stock_baseline_1h": hourly_baseline,
            "stock_baseline_830": session_open_price,
            "stock_baseline_noon": noon_baseline,
            "stock_baseline_930": nine_thirty_baseline,
            "vwap": round(vwap, 4) if vwap is not None else None,
            "opening_range_high": None,
            "opening_range_low": None,
            "opening_range_ready": False,
            "long_retest_confirmed": False,
            "short_retest_confirmed": False,
        }

    range_high = max(candle["high"] for candle in opening_range_bars)
    range_low = min(candle["low"] for candle in opening_range_bars)
    after_range = [
        candle for candle in completed
        if candle["time"] >= session_open + dt.timedelta(minutes=30)
    ]
    tolerance = max(range_high, range_low) * 0.001

    def has_recent_retest(direction):
        breakout_indices = [
            index for index, candle in enumerate(after_range)
            if (candle["close"] > range_high if direction == "long"
                else candle["close"] < range_low)
        ]
        for breakout_index in breakout_indices:
            for retest_index in range(breakout_index + 1, len(after_range)):
                retest = after_range[retest_index]
                if direction == "long":
                    held_level = retest["low"] <= range_high + tolerance and retest["close"] > range_high
                    stayed_valid = all(
                        candle["close"] > range_high
                        for candle in after_range[retest_index:]
                    )
                else:
                    held_level = retest["high"] >= range_low - tolerance and retest["close"] < range_low
                    stayed_valid = all(
                        candle["close"] < range_low
                        for candle in after_range[retest_index:]
                    )
                if held_level and stayed_valid and len(after_range) - 1 - retest_index <= 2:
                    return True
        return False

    return {
        "session_date": session_date.isoformat(),
        "data_as_of": data_as_of.isoformat(),
        "stock_price": round(stock_price, 4),
        "stock_ema20": round(stock_ema20, 4) if stock_ema20 is not None else None,
        "stock_ema20_relation": stock_ema20_relation,
        "stock_momentum": stock_momentum,
        "stock_momentum_change": ten_minute_change,
        "stock_momentum_baseline_10m": ten_minute_baseline,
        "session_pct": session_pct,
        "stock_pct_1h": hourly_pct,
        "stock_pct_830_to_noon": morning_pct,
        "stock_pct_noon_to_3": afternoon_pct,
        "stock_pct_930_to_3": nine_thirty_pct,
        "stock_pct_930_to_noon": nine_thirty_to_noon_pct,
        "stock_pct_1d": session_pct,
        "stock_baseline_1h": hourly_baseline,
        "stock_baseline_830": session_open_price,
        "stock_baseline_noon": noon_baseline,
        "stock_baseline_930": nine_thirty_baseline,
        "vwap": round(vwap, 4) if vwap is not None else None,
        "opening_range_high": round(range_high, 4),
        "opening_range_low": round(range_low, 4),
        "opening_range_ready": True,
        "long_retest_confirmed": has_recent_retest("long"),
        "short_retest_confirmed": has_recent_retest("short"),
    }


def classify_day_trade_setup(context, now_dt, stock_price, ema_fast, ema_slow, session_pct):
    """Classify the most recent regular-session snapshot, including after hours."""
    if not context.get("session_date"):
        return "WAIT · NO REGULAR-SESSION DATA"
    if not context.get("opening_range_ready"):
        return "WAIT · OPENING RANGE FORMING"
    if context.get("vwap") is None or any(
            value is None for value in (stock_price, ema_fast, ema_slow, session_pct)):
        return "WAIT · INSUFFICIENT DATA"

    vwap = context["vwap"]
    if (stock_price > ema_fast > ema_slow and stock_price > vwap and session_pct > 0):
        if context.get("long_retest_confirmed"):
            return "CALL WATCH · OR RETEST"
        return "BULLISH · WAIT FOR OR RETEST"
    if (stock_price < ema_fast < ema_slow and stock_price < vwap and session_pct < 0):
        if context.get("short_retest_confirmed"):
            return "PUT WATCH · OR RETEST"
        return "BEARISH · WAIT FOR OR RETEST"
    return "MIXED · WAIT"


def _ema_series(values: list, period: int):
    """Return EMA values aligned to the input, with None before the seed."""
    if len(values) < period:
        return [None] * len(values)
    result = [None] * (period - 1)
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    result.append(ema)
    for value in values[period:]:
        ema = value * k + ema * (1 - k)
        result.append(ema)
    return result


def _compute_ema_trend(client, symbol: str, now_dt: dt.datetime = None):
    now_dt = now_dt or dt.datetime.now(_tz())
    candles = fetch_5min_candles(client, symbol)
    regular_candles = []
    for candle in candles:
        timestamp = candle.get("datetime")
        if timestamp is None or candle.get("close") is None:
            continue
        candle_dt = dt.datetime.fromtimestamp(timestamp / 1000, tz=_tz())
        session_open = candle_dt.replace(hour=8, minute=30, second=0, microsecond=0)
        regular_close = candle_dt.replace(hour=15, minute=0, second=0, microsecond=0)
        if (candle_dt.weekday() < 5 and session_open <= candle_dt < regular_close and
                candle_dt + dt.timedelta(minutes=5) <= now_dt):
            regular_candles.append(candle)
    regular_candles.sort(key=lambda candle: candle["datetime"])
    closes = [c["close"] for c in regular_candles]
    if len(closes) < config.EMA_SLOW:
        return None  # not enough 5-minute history yet

    ema_fast = compute_ema(closes, config.EMA_FAST)
    ema_slow = compute_ema(closes, config.EMA_SLOW)
    if ema_fast is None or ema_slow is None:
        return None

    fast_series = _ema_series(closes, config.EMA_FAST)
    slow_series = _ema_series(closes, config.EMA_SLOW)
    long_series = _ema_series(closes, 50)
    ride_price = closes[-1]
    ema50 = long_series[-1]
    ema21_rising = (len(slow_series) >= 4 and slow_series[-4] is not None
                    and slow_series[-1] > slow_series[-4])
    aligned_rise = (ema50 is not None and ride_price > ema_fast > ema_slow > ema50
                    and ema21_rising)
    pullback = (aligned_rise and ride_price > 0 and
                min(abs(ride_price - ema_fast), abs(ride_price - ema_slow)) / ride_price <= 0.015)
    crossover_price = None
    crossover_datetime = None
    crossover_direction = None
    previous_bullish = None
    for index, (fast, slow) in enumerate(zip(fast_series, slow_series)):
        if fast is None or slow is None:
            continue
        bullish = fast > slow
        if previous_bullish is not None and bullish != previous_bullish:
            crossover_price = closes[index]
            crossover_datetime = regular_candles[index].get("datetime")
            crossover_direction = "above" if bullish else "below"
        previous_bullish = bullish

    return {
        "ema8": round(ema_fast, 2),
        "ema21": round(ema_slow, 2),
        "ema50": round(ema50, 2) if ema50 is not None else None,
        "trend_ride_status": ("PULLBACK WATCH" if pullback else "TRENDING" if aligned_rise else "—"),
        "trend_ride": bool(aligned_rise),
        "ema_bullish": ema_fast > ema_slow,
        "ema_crossover_price": crossover_price,
        "ema_crossover_datetime": crossover_datetime,
        "ema_crossover_direction": crossover_direction,
        "day_trade_context": _opening_range_context(candles, now_dt),
    }


def get_ema_trend(client, symbol: str, ema_cache: dict, now_dt: dt.datetime = None):
    """
    Cached EMA8/EMA21 trend for `symbol`, recomputed at most once every
    EMA_REFRESH_SECONDS. `ema_cache` is a plain dict the caller keeps
    across passes (e.g. {symbol: {"computed_at": dt, "data": {...}|None}}).
    Returns None if there isn't enough daily history or the lookup fails.
    """
    now_dt = now_dt or dt.datetime.now(_tz())
    cached = ema_cache.get(symbol)
    if cached and (now_dt - cached["computed_at"]).total_seconds() < config.EMA_REFRESH_SECONDS:
        return cached["data"]

    try:
        data = _compute_ema_trend(client, symbol, now_dt)
    except Exception:
        # Keep the last good value (if any) rather than blanking out the
        # row over a single transient API error.
        data = cached["data"] if cached else None

    ema_cache[symbol] = {"computed_at": now_dt, "data": data}
    return data
