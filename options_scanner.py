"""
Schwab Options Scanner (CLI)
=============================

For each stock in watchlist.txt, finds ONE option contract:
  - nearest upcoming expiration date
  - strike closest to the current stock price (at-the-money)
  - price under $2, bid/ask spread under 5%
  - price change since market open (8:30 AM Central) is positive

See scanner_core.py for the shared logic (also used by app.py, the web
dashboard).

Usage:
    python options_scanner.py                       # single pass over watchlist.txt
    python options_scanner.py --watch 15             # refresh every 15 seconds
    python options_scanner.py --watchlist my_tickers.txt --out results.csv   # a different file
    python options_scanner.py --weekly-universe       # instead: build the full Schwab weekly-options universe
"""
import argparse
import sys
import time
import datetime as dt

import pandas as pd

import scanner_core as sc
from auth import get_client


def print_results(results: list[dict]):
    ts = dt.datetime.now().strftime("%H:%M:%S")
    if not results:
        print(f"[{ts}] no qualifying near-the-money options right now")
        return
    print(f"[{ts}] {len(results)} qualifying option(s):")
    for r in results:
        print(f"  {r['underlying']:6s} {r['option_symbol']:22s} "
              f"({r['put_call']} ${r['strike']} exp {r['expiration']}) "
              f"+{r['change_since_baseline']} since {r['baseline_label']}  "
              f"(now {r['current_price']}, was {r['price_at_baseline']})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchlist", default="watchlist.txt",
                         help="Ticker file to scan, one symbol per line (default: watchlist.txt).")
    parser.add_argument("--out", default="results.csv")
    parser.add_argument("--watch", type=int, default=0,
                         help="Refresh every N seconds instead of running once.")
    parser.add_argument("--weekly-universe", action="store_true",
                         help="Ignore --watchlist and instead build the full weekly-optionable "
                              "universe live from the Schwab API (see universe.py). Slow on "
                              "first run of the day.")
    parser.add_argument("--rebuild-universe", action="store_true",
                         help="With --weekly-universe: force-refresh the symbol list instead of "
                              "using today's cached one.")
    parser.add_argument("--max-symbols", type=int, default=None,
                         help="With --weekly-universe: cap the universe to this many symbols "
                              "(useful for a quick test run).")
    args = parser.parse_args()

    client = get_client()

    if args.weekly_universe:
        import universe
        tickers = universe.get_weekly_universe(
            client, force_rebuild=args.rebuild_universe, max_symbols=args.max_symbols)
        if not tickers:
            print("No weekly-optionable symbols found.")
            sys.exit(1)
    else:
        tickers = sc.load_watchlist(args.watchlist)
        if not tickers:
            print(f"No tickers found in {args.watchlist}")
            sys.exit(1)

    print(f"Scanning {len(tickers)} symbols (nearest expiry, near-the-money, "
          f"under $2, spread under 5%)...\n")
    print(f"Baseline: {sc.BASELINE_LABEL}, fixed for the whole session.\n")

    cache = sc.load_cache()

    if args.watch <= 0:
        results = sc.run_pass(client, tickers, cache)
        sc.save_cache(cache)
        print_results(results)
        if results:
            pd.DataFrame(results).to_csv(args.out, index=False)
            print(f"\nSaved to {args.out}")
        return

    print(f"Refreshing every {args.watch} seconds. Press Ctrl+C to stop.\n")
    try:
        while True:
            results = sc.run_pass(client, tickers, cache)
            sc.save_cache(cache)
            print_results(results)
            if results:
                pd.DataFrame(results).to_csv(args.out, index=False)
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
