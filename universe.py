"""
Builds the scan universe: every stock that currently has WEEKLY options,
determined via the Schwab API -- no static watchlist file, no CBOE.

Two-step process:
  1. Get a broad, fast list of candidate US equity tickers (NASDAQ's own
     free symbol-directory files -- not CBOE, which is slow to scrape).
  2. Ask Schwab's option-expiration-chain endpoint, per symbol, whether
     any of its expirations are flagged "W" (weekly). Symbols that pass
     are kept; everything else is dropped.

Step 2 is the expensive part (one API call per candidate symbol), so the
result is cached to disk for the day and only rebuilt when stale or when
explicitly requested.
"""
import concurrent.futures
import datetime as dt
import json
import os
import threading
import time
from zoneinfo import ZoneInfo

import httpx
import requests

import config

_REQUEST_TIMEOUT = 10          # NASDAQ symbol-directory file downloads
_SCHWAB_CALL_TIMEOUT = 8       # per expiration-chain call while building the universe
_SCHWAB_CALL_RETRIES = 1       # extra attempts after the first, on timeout/error


def _today_key() -> str:
    return dt.datetime.now(ZoneInfo(config.BASELINE_TIMEZONE)).strftime("%Y-%m-%d")


def _parse_nasdaq_listed(text: str) -> list[str]:
    lines = text.strip().splitlines()
    symbols = []
    for line in lines[1:-1]:  # first line is header, last line is a file-footer/timestamp
        fields = line.split("|")
        if len(fields) < 4:
            continue
        symbol, _name, _market_cat, test_issue = fields[0], fields[1], fields[2], fields[3]
        etf = fields[5] if len(fields) > 5 else "N"
        if test_issue == "Y" or etf == "Y":
            continue
        if not symbol or any(ch in symbol for ch in ".$ "):
            continue  # skip units/warrants/rights and other non-plain-vanilla tickers
        symbols.append(symbol.upper())
    return symbols


def _parse_other_listed(text: str) -> list[str]:
    lines = text.strip().splitlines()
    symbols = []
    for line in lines[1:-1]:
        fields = line.split("|")
        if len(fields) < 5:
            continue
        # ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|...
        act_symbol, _name, _exch, _cqs, etf = fields[0], fields[1], fields[2], fields[3], fields[4]
        test_issue = fields[6] if len(fields) > 6 else "N"
        if test_issue == "Y" or etf == "Y":
            continue
        if not act_symbol or any(ch in act_symbol for ch in ".$ "):
            continue
        symbols.append(act_symbol.upper())
    return symbols


def fetch_candidate_symbols() -> list[str]:
    """
    Fast (~1-2 sec) download of every plain-vanilla, non-ETF, non-test
    common stock listed on NASDAQ/NYSE/AMEX via NASDAQ's own trader
    files. This is just the *candidate* list -- step 2 below narrows it
    down to the ones that actually have weekly options.
    """
    symbols = set()
    for url, parser in (
            (config.NASDAQ_LISTED_URL, _parse_nasdaq_listed),
            (config.OTHER_LISTED_URL, _parse_other_listed)):
        try:
            resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not download the NASDAQ symbol directory from {url}: {exc}") from exc
        symbols.update(parser(resp.text))

    return sorted(symbols)


def is_weekly_optionable(client, symbol: str):
    """
    True/False if `symbol` currently has a weekly-flagged expiration,
    or None if the check failed even after retries (treated as "skip",
    not as "no weekly options", so callers can report it separately).
    """
    last_err = None
    for attempt in range(_SCHWAB_CALL_RETRIES + 1):
        try:
            resp = client.get_option_expiration_chain(symbol)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_err = e
            continue
        except Exception as e:
            last_err = e
            continue

        if resp.status_code != 200:
            return False
        try:
            data = resp.json()
        except ValueError:
            return False
        for exp in data.get("expirationList", []) or []:
            if exp.get("expirationType") == "W":
                return True
        return False

    return None  # every attempt failed/timed out


def _load_cache() -> dict:
    if not os.path.exists(config.UNIVERSE_CACHE_PATH):
        return {}
    try:
        with open(config.UNIVERSE_CACHE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(symbols: list[str]):
    with open(config.UNIVERSE_CACHE_PATH, "w") as f:
        json.dump({"date": _today_key(), "symbols": symbols}, f)


def build_weekly_universe(client, candidates: list[str] = None,
                           max_workers: int = None,
                           progress_every: int = 250,
                           on_progress=None) -> list[str]:
    """
    Probe every candidate symbol against Schwab's expiration-chain
    endpoint (threaded) and return just the ones with weekly options.
    This is the slow, expensive step -- callers should prefer
    get_weekly_universe() below, which caches the result for the day.

    `on_progress`, if given, is called as on_progress(done, total, found)
    after every symbol checked (e.g. so a web dashboard can show live
    progress instead of only the console prints below).
    """
    candidates = candidates or fetch_candidate_symbols()
    max_workers = max_workers or config.UNIVERSE_BUILD_WORKERS

    weekly = []
    failed = []
    done = 0
    lock = threading.Lock()
    total = len(candidates)

    def _check(sym):
        nonlocal done
        result = is_weekly_optionable(client, sym)
        with lock:
            done += 1
            if result is None:
                failed.append(sym)
            elif result:
                weekly.append(sym)
            if done % progress_every == 0:
                print(f"  ...checked {done}/{total} symbols for weekly options "
                      f"({len(weekly)} qualify so far, {len(failed)} failed/timed out)")
            if on_progress:
                on_progress(done, total, len(weekly))
        return sym, result

    print(f"Building weekly-options universe from {total} candidate symbols "
          f"({max_workers} workers, {_SCHWAB_CALL_TIMEOUT}s timeout, "
          f"{_SCHWAB_CALL_RETRIES} retry)...")

    # Use a shorter per-request timeout just for this bulk probing pass, so a
    # single slow/hanging symbol can't stall the whole build. Restored after.
    original_timeout = getattr(client.session, "timeout", None)
    client.set_timeout(_SCHWAB_CALL_TIMEOUT)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(_check, candidates))
    finally:
        if original_timeout is not None:
            client.set_timeout(original_timeout)

    weekly.sort()
    print(f"Done: {len(weekly)} symbols currently have weekly options "
          f"({len(failed)} symbols failed to check after retries and were skipped).")
    return weekly


def get_weekly_universe(client, force_rebuild: bool = False,
                         max_symbols: int = None,
                         on_progress=None) -> list[str]:
    """
    Main entry point: returns today's list of weekly-optionable symbols,
    using the on-disk cache when it's already fresh for today.
    """
    if not force_rebuild:
        cache = _load_cache()
        if cache.get("date") == _today_key() and cache.get("symbols"):
            symbols = cache["symbols"]
            if max_symbols:
                symbols = symbols[:max_symbols]
            return symbols

    candidates = fetch_candidate_symbols()
    if max_symbols:
        candidates = candidates[:max_symbols]

    weekly = build_weekly_universe(client, candidates, on_progress=on_progress)
    _save_cache(weekly)
    return weekly
