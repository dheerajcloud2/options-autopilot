# Schwab Options Scanner

Scans the liquid symbols in `watchlist.txt` by default. It shows options
that are near the money, up since the market-open baseline, priced under $2,
with a bid/ask spread under 5%.

python -m pip install -r requirements.txt
venv\Scripts\activate.bat
venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py --interval 5 
## 1. Get Schwab API access

You said you already have your Schwab API app key and secret. You'll also
need the **callback URL** you registered for that app on
https://developer.schwab.com (must be `https://` and match exactly, e.g.
`https://127.0.0.1:8182`).
cd C:\Users\dheer\ALL\schwab-options-scanner
.\venv\Scripts\python.exe app.py --interval 5
.\venv\Scripts\python.exe app.py --weekly-universe --interval 60
## 2. Install

```bash
cd schwab-options-scanner
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Configure credentials

```bash
cp .env.example .env
```

This scanner supports separate Schwab developer apps for Market Data and
Accounts and Trading. Edit `.env` with the matching Client ID and Client
Secret for each app:

```
SCHWAB_MARKET_DATA_APP_KEY=your_market_data_app_key
SCHWAB_MARKET_DATA_APP_SECRET=your_market_data_app_secret
SCHWAB_TRADING_APP_KEY=your_trading_app_key
SCHWAB_TRADING_APP_SECRET=your_trading_app_secret
SCHWAB_CALLBACK_URL=https://127.0.0.1:8182
SCHWAB_MARKET_DATA_TOKEN_PATH=./schwab_market_data_tokens.json
SCHWAB_TRADING_TOKEN_PATH=./schwab_trading_tokens.json
```

Register the callback URL with **both** Schwab apps. Keep each app's
credentials paired with its own product: Market Data credentials are used
for scanning and quotes; Accounts and Trading credentials are used for
account lookup and order submission. The older `SCHWAB_APP_KEY`,
`SCHWAB_APP_SECRET`, and `SCHWAB_TOKEN_PATH` settings are supported only
as a legacy Market Data configuration; they are not used for trading.

## 4. Edit your watchlist

Put one ticker per line in `watchlist.txt` (a sample is included). This is
the default scan list for both the dashboard and the CLI.

Prefer to scan every stock with weekly options instead of maintaining a
list yourself? Pass `--weekly-universe` to either script — see "Optional:
scan every weekly-optionable stock instead" further down. It's off by
default since it means many more Schwab API calls per refresh.

## 5. Run it (browser dashboard or terminal)

### Paper trading mode

Paper trading is the default selection for each new order. The order ticket
lets you choose Paper Trading or a Schwab live account before reviewing a buy.
Live trading is available when the Accounts and Trading credentials are
configured and authorized. Every live order requires a separate review and
confirmation. The dashboard
clearly marks this mode and stores simulated orders locally in
`~/.schwab-options-scanner/paper_trading.sqlite3` (override with
`PAPER_TRADING_DB_PATH`). Paper orders are shared by dashboard instances using
the same database. They are not Schwab orders and cannot affect a Schwab
account; paper mode does not call Schwab order placement or cancellation APIs.
Schwab market data is still used to evaluate simulated fills.

Paper limit orders remain working until a market quote during regular trading
hours reaches the limit or the DAY order expires. A buy fills when the option
ask is at or below its limit; a sell fills when the bid is at or above its
limit. Simulated OCO take-profit and stop exits are checked when My Orders
refreshes. This model does not simulate partial fills, queue priority,
commissions, or slippage. Keep paper mode enabled while testing.
`PAPER_TRADING` controls the default mode shown when the orders monitor opens;
use its Paper/Live selector to switch modes. Orders can be canceled or closed
from their matching mode view.

The first time you run either the dashboard or the CLI, a browser window
opens for you to log in to the Market Data Schwab app. The first time you
open an order ticket, the browser signs in to the separate Accounts and
Trading Schwab app. After you approve,
you'll be redirected to your callback URL (the page won't load anything,
that's fine) — copy that full redirected URL back into the terminal if
prompted. Each app's login is cached in its own token file and
reused automatically.



### Option A — Web dashboard (recommended)

```bash
python app.py
```

Then open **http://127.0.0.1:5000** in your browser. It refreshes itself
every 10 seconds by default (change with `--interval`, e.g.
`python app.py --interval 5`). Leave the terminal running; close it or
press Ctrl+C to stop the dashboard. The dashboard also starts Schwab's
streaming connection for underlying stock quotes when the watchlist is ready.
Stock Trend percentages update from live regular-session
quotes, using the candle-derived 1H, 8:30-to-noon, noon-to-3 PM,
9:30-to-3 PM, and 8:30-to-3 PM baselines. The
browser checks for streamed updates once per second; the scan interval still
controls how often the broader option scan runs. Streaming requires valid
Accounts and Trading app credentials/token. The header shows LIVE when the
stream connects; outside regular market hours, Stock Trend retains its last
regular-session values.


### Autopilot

The **Autopilot** tab shows the on/off switch (a kill switch that stops new
entries immediately), the active risk limits, proposals, and recent activity.
It is off by default (`AUTOPILOT_ENABLED=false`) and can only be started from
the tab or by setting that variable.

An option is ordered when it appears in any of the window tables (8:30 to 9:30, 9:30 to 10:30, 10:30 to 12, 12 to 3), i.e. it is positive in that window and priced under $2.00. Each option gets at most one order per day, for AUTOPILOT_MAX_CONTRACTS contracts (default 1), with an OCO exit: take profit at +AUTOPILOT_TAKE_PROFIT_PCT (default 15%) and stop loss at -AUTOPILOT_STOP_LOSS_PCT (default 10%) of the entry. Once the account is up `AUTOPILOT_DAILY_PROFIT_TARGET_PCT` (default 2%) for the day, no new entries are made until the next day (paper counts realized P/L; live counts the change in account value since the first pass of the day). Open positions keep their exits. By default (`AUTOPILOT_SKIP_EXISTING=false`) any listed option that passes the filters is bought, provided its price is rising (`AUTOPILOT_REQUIRE_UP`). Set `AUTOPILOT_SKIP_EXISTING=true` to ignore options already listed when autopilot is switched on.

Total premium spent on new entries per day is capped at AUTOPILOT_DAILY_BUDGET_PCT (default 50%) of the account value, fixed at the first check of the day (live: Schwab liquidation value across linked accounts; paper: AUTOPILOT_PAPER_ACCOUNT_SIZE). An order is skipped if the remaining budget cannot cover it. Other limits: AUTOPILOT_MAX_OPEN_POSITIONS, optional AUTOPILOT_RISK_PER_TRADE, no entries at or after AUTOPILOT_LAST_ENTRY_TIME (Central), and in paper mode AUTOPILOT_MAX_DAILY_LOSS.

- **Paper mode (default, `AUTOPILOT_MODE=paper`)** places simulated bracket
  orders itself. View them in My Orders / Positions with the Paper selector.
  Run this for several weeks before considering live mode.
- **Live mode (`AUTOPILOT_MODE=live`)** never places orders. It creates
  proposals that expire after `AUTOPILOT_PROPOSAL_TTL_SECONDS`. **Review order**
  opens the normal order ticket prefilled with the proposed size, entry and exit
  plan; you still pick the account, check the order and tick the confirmation
  box, so every live order is individually confirmed. It requires
  `PAPER_TRADING=false` and Accounts and Trading credentials. Pending and submitted proposals count against the daily budget; dismissed or expired ones do not, but are not re-proposed that day. The position cap
  counts your open Schwab option contracts plus pending proposals. The daily
  loss limit is not enforced in live mode because every order is approved by you.
- **Contract quality filters:** autopilot also requires |delta| between
  `AUTOPILOT_MIN_ABS_DELTA` (0.25) and `AUTOPILOT_MAX_ABS_DELTA` (0.60), at least
  `AUTOPILOT_MIN_DAYS_TO_EXPIRY` (2) days to expiry, open interest of at least
  `AUTOPILOT_MIN_OPEN_INTEREST` (500), and an ask no more than
  `AUTOPILOT_MAX_ASK_ABOVE_MID_PCT` (5) percent above the mid price. Set a value
  to 0 to disable that check. Contracts with missing data fail an enabled check.
- **EMA filter:** with `AUTOPILOT_REQUIRE_EMA=true` (default) calls are bought only
  while the underlying stock is above its 20 EMA (the dashboard's stock EMA
  column); puts only while it is below.
- **Trend filter:** with `AUTOPILOT_REQUIRE_UP=true` (default) only options whose
  table Momentum is UP are bought; FLAT, DOWN or unknown ones are re-checked on
  later scans while they stay listed.
- **Fully automatic live (`AUTOPILOT_LIVE_AUTO=true`)** skips the confirmation:
  autopilot sends the 1-contract buy with OCO exits to Schwab account
  `AUTOPILOT_LIVE_ACCOUNT_INDEX` itself, using real money. It also requires
  `AUTOPILOT_MODE=live`, `PAPER_TRADING=false` and
  `AUTOPILOT_LIVE_AUTO_CONFIRM=I_ACCEPT_REAL_MONEY_RISK`. The 50% daily budget,
  one order per option per day and position cap still apply; the daily loss
  limit does not.

Autopilot is not investment advice and does not guarantee fills or limit losses;
stop-market exits can fill below the trigger.

### Install on a phone

The dashboard can be installed as a mobile web app. Open its HTTPS address in
Safari on iPhone or Chrome on Android, then choose **Add to Home Screen** or
**Install app** from the browser menu. `localhost` is also treated as secure
for development, but a phone cannot use another computer's `127.0.0.1`; deploy
the Flask app behind HTTPS or use a trusted private network HTTPS address.
The app always fetches scanner and trading data from the server and does not
store order or account responses for offline use.

### Option B — Terminal / CSV only

```bash
python options_scanner.py            # single pass over watchlist.txt
python options_scanner.py --watch 10 # refreshes every 10 seconds in the terminal
```

Both options share the same underlying scan logic (`scanner_core.py`) and
the same 8:30 AM baseline cache, so you can use either interchangeably.

Useful flags on both `app.py` and `options_scanner.py`:

- `--watchlist path/to/file.txt` — scan a different ticker file (default
  is `watchlist.txt`, one symbol per line).

### Optional: scan every weekly-optionable stock instead

If you'd rather not maintain a watchlist file at all, pass
`--weekly-universe` to either script and it will build its own list of
every stock that currently has weekly options, live from the Schwab API,
instead of reading `watchlist.txt`. See "How the weekly-options universe
is built" below for details — it's off by default because the first run
of the day takes a few minutes and uses many more API calls per refresh
than a short watchlist.

- `--rebuild-universe` — with `--weekly-universe`, force-refresh the
  symbol list instead of using today's cache.
- `--max-symbols N` — with `--weekly-universe`, cap it to N symbols,
  handy for a fast test run without waiting for the full build.


## How it picks the option

For each stock, it finds the **nearest expiration date that is not today**
(0DTE / same-day expirations are skipped), then walks strikes outward from
the current stock price and picks the closest one that passes **both**:

- **bid/ask spread under 5%** of the mid price
- **current price under $2**

for both the call and the put side independently. If the closest strike
fails either check, it tries the next strike out, and so on.

## What the dashboard shows

The dashboard shows a compact **Stock Trend** table first, followed by three
simple scanner tabs: **Day Change**, **8:30 to 12**, and **12 to 3**. The
8:30-to-noon tab shows its main options table first, then tabs for the
8:30-to-9:30, 9:30-to-10:30, and 10:30-to-noon weekly windows. The shorter
windows show next-weekly options under $2 with a spread under 10%. Calls and
puts are shown separately, and the views stay separate rather than combining
near-term and weekly contracts into one table.
Option tables keep the key comparison fields: contract type, stock, 10-minute
momentum, period change, current mark, strike, spread, volume, expiry, and
order review. They omit repeated stock-price snapshots and historical option
prices for each time-window boundary. Momentum is labeled UP, FLAT, DOWN,
WAIT, or N/A and can be sorted by clicking its column.

The **Weekly Trend** tab shows weekly calls only when the underlying has a
BULLISH trend (price above EMA8 above EMA21, with a positive session move).
Candidates must have at least 100 contracts of volume and a spread below 5%.
In `--weekly-universe` mode, only underlying stocks priced from $25 through
$500 are scanned for option chains. A batched stock-quote check filters the
universe before chain lookups; the normal watchlist scanner is not price
filtered. This share-price range reduces irrelevant scanning but is not a
profitability or safety filter.
This uses the full weekly-optionable universe only when the app is started with
`--weekly-universe`; the default watchlist mode checks only symbols in
`watchlist.txt`. Trend alignment is a filter, not a guarantee that the
underlying or option will continue moving in that direction or be profitable.

To keep the regular watchlist dashboard open separately, start a second
instance in another VS Code terminal:

```powershell5001
.\venv\Scripts\python.exe app.py --weekly-universe --interval 300 --port 
```

Open `http://127.0.0.1:5001/` in another browser tab and select **Weekly
Trend**. The cached universe avoids rebuilding the symbol list for the day;
the first full scan can still take several minutes and makes more Schwab API
requests than the watchlist scanner. The scanner starts another pass after
the previous pass and the configured interval have completed.

The **Stock Trend** table keeps the symbol, setup status, and percentage
change over the latest hour, 8:30 AM to noon, noon to 3 PM, 9:30 AM to 3 PM,
and the full 8:30 AM to 3 PM regular session. The noon-to-3 PM value is blank
until noon, and the 9:30-to-3 PM value is blank until the 9:30 AM baseline is
available. Price vs EMA20 compares the latest price with the EMA of the last
20 completed five-minute candles. The 10-minute Momentum column shows whether
price is up, down, or unchanged versus the completed candle from 10 minutes
earlier; both indicators update with live stock quotes when available.
Click a ticker to open its intraday stock chart, or click a metric header to
sort.
The compact table keeps all stocks visible, with readable color-coded
percentage changes and no inner vertical scrollbar. The 1D change compares
the 8:30 AM regular-session open with the current price through 3 PM, then
retains the closing value after hours. The setup status evaluates
the underlying stock, not the option's recent gain: a CALL/PUT watch requires
session VWAP, 5-minute EMA8/EMA21 alignment, direction since the 8:30 AM
Central open, and a completed break/retest of the first 30-minute opening
range. It uses completed five-minute bars, requires all six opening-range
bars, and keeps a retest valid for at most three bars. The latest
regular-session snapshot is retained after hours; extended-hours candles are
not used. Exchange holidays are not currently calendar-filtered. This is a
candidate filter, not an entry signal or a recommendation; confirm the chart
and avoid chasing.

The scanner still excludes same-day-expiring options and retains its existing
weekly-option and low-price filters. Those filters can omit otherwise liquid
contracts and do not make a contract low-risk. This scanner is for reviewing
intraday candidates, not for automatically deciding or placing day trades.

One page, split into **Calls** and **Puts**, sortable by any column (click
a header; click again to flip direction). Click any row to open a price
chart for that contract's whole day, with a dashed marker at 8:30 AM
(market open).

Each row is a stock's near-the-money, near-term, under-$2 contract with a
spread under 5%, shown only while it's **up since market open** — negative
or flat ones are hidden, with a quiet "none right now" message when
nothing qualifies.

The option **Momentum** column measures the selected option's change
over the latest ten minutes of completed regular-session one-minute candles.
It shows **UP**, **DOWN**, or **FLAT** (the absolute move is below the greater
of 2% of the option price or $0.02); **WAIT** means there is not yet enough
candle history, and **N/A** means the lookup failed. The column is sortable
in UP, FLAT, DOWN, WAIT order.
This uses the Market Data API and does not depend on Schwab streaming being
connected. Results are cached for one minute, and after hours use the latest
regular-session candles rather than extended-hours quotes.

### Reviewed option order entry

The web dashboard lets you prepare a single-leg option bracket order
from a scanner row. Click **Review**, choose the Paper Trading account (or,
only when explicitly configured for live trading, a Schwab account), set the
contract quantity, entry limit, take-profit limit, and stop-market trigger,
then separately confirm the complete order. In live mode, the entry is a
**buy-to-open limit** order; after the entry fills, it triggers an OCO pair
of **sell-to-close** exits. When one exit executes, Schwab cancels the other.
In paper mode, the same entry and OCO prices are simulated from refreshed
quotes. Orders use regular market hours and DAY duration. Entry limit is capped
at $2.00 per contract and quantity at 100. Entry must be at least $0.02
so a lower stop trigger can be placed. Choose 10% profit / 20% stop,
Conservative (+20% profit / -15% stop), Balanced (+30% / -20%), Wider
(+50% / -30%), or Custom percentages (1%–500%). The suggested entry starts from a valid current mark,
then the bid/ask midpoint, last price, ask, or scanner price as available;
it is not a trader recommendation. Review the live quote and set your own
maximum buy limit. The ticket calculates exit prices from the entry,
rounds them to cents, and shows the exact prices during review. These are
editable examples, not recommendations. No order
is sent until the final confirmation is
checked and submitted. The dashboard does not automatically decide when
to trade, and scanner rows are not a buy recommendation.

Use **My Paper Orders** in paper mode or **My Schwab Orders** in live mode to
review recent orders, fills, and OCO exit statuses. The list refreshes
automatically every 30 seconds while the dashboard is open, or on demand.
Paper cancellations are local. In live mode, **Cancel** is offered only for
orders Schwab currently reports as cancellable; verify the final status here
or directly in Schwab.

For an eligible filled buy, **Sell to Close** opens a review ticket for a
manual limit order. Paper mode checks simulated position availability and
cancels local OCO exits when the paper sell is recorded. Live mode checks the
live long position, requests cancellation of attached active OCO exits, and
submits the manual sell only after Schwab confirms those exits are canceled.
If cancellation or position verification fails, no live sell is sent. Review
the quantity and limit price carefully: accepting a sell limit does not
guarantee a fill, and an unfilled order can remain open.

A stop-market trigger does **not** guarantee the execution price or that
an exit will fill; a fast or illiquid market can result in substantial
slippage. OCO orders do not guarantee a maximum loss, and the entry can
fill without an exit order being accepted. Verify the complete bracket
and order status in Schwab after submission. This feature is not financial
advice.

If the order ticket reports **Schwab denied account access (HTTP 401)**,
verify the Accounts and Trading credentials and product access, then move
the token file configured by `SCHWAB_TRADING_TOKEN_PATH` (default
`./schwab_trading_tokens.json`) aside and sign in again. The scanner uses a
separate Market Data app and token, so account-access failures need not stop
market-data scans.

Order submission uses the Schwab account authorized by your local API
token and can place real trades. Only use this feature if you understand
the order and its risks. The app binds to `127.0.0.1`; do not expose it to
other machines or the public internet. After submitting, verify the
order's status in Schwab; acceptance does not guarantee a fill.

## The 8:30 AM baseline

The market opens at 9:30 AM **Eastern**, which is **8:30 AM Central** — not
9:30 Central. The baseline is set correctly to 8:30 AM `America/Chicago`.

The "% Change (Session)" and "Change" columns are always measured against
that single **8:30 AM open** price for the rest of the trading day — there
is no midday reset. The `@ Baseline` column shows "as of 8:30 AM open" on
every row.

There's also a **% Change (4h)** column — a rolling comparison to the price
exactly 4 hours ago, independent of the 8:30 AM baseline. It can show
negative even on a row that's positive for the session.

## How the baseline is calculated fast

Getting the true change from a specific clock time requires a separate,
slower minute-candle lookup — but doing that lookup every refresh would be
too slow for something moving quickly. So:

1. **Once per contract, per day**, the script fetches 1-minute candles and
   grabs the price at 8:30 AM. This gets cached to `baseline_cache.json`.
2. **Every refresh after that** just calls the live option chain (one fast
   call per stock) and compares the current price to the cached baseline —
   no repeated slow history calls.
3. The cache resets automatically each new trading day.

The 4-hour rolling change and the click-to-chart history are both live
lookups (they have to be — the data they need keeps moving), so each costs
an extra history call when used.

## How the weekly-options universe is built

Schwab's API has no "list every symbol" endpoint, so building the scan
list is a two-step process (`universe.py`):

1. **Candidate list**: a fast (~1-2 second) download of NASDAQ's own free
   symbol-directory files (`nasdaqlisted.txt` / `otherlisted.txt`), covering
   every NASDAQ/NYSE/AMEX common stock — filtered to drop ETFs, test
   issues, and units/warrants. This is intentionally **not** CBOE's site,
   which is much slower.
2. **Weekly filter**: each candidate is checked against Schwab's option
   **expiration-chain** endpoint, which flags each expiration date as
   weekly (`"W"`), monthly, or quarterly. Only symbols with at least one
   weekly-flagged expiration are kept.

Step 2 is the slow part — one Schwab API call per candidate symbol — so
it's threaded (`UNIVERSE_BUILD_WORKERS` in `config.py`, default 8 at a
time) and the result is cached for the day in
`weekly_universe_cache.json`. Every scan after the first one that day
reuses that cached list instantly. Pass `--rebuild-universe` to force a
fresh build (e.g., first thing each morning), or `--max-symbols N` to cap
it for a quick test.

## Click-to-chart

Clicking any row opens a chart of that contract's price today (loaded via
Chart.js from a CDN — requires internet access same as any other web page).
A dashed vertical line marks 8:30 AM (market open). This is a fresh lookup
each time you click, not cached, so it always reflects the latest data
available.

## Email alerts

Set `EMAIL_ENABLED=true` in `.env` and fill in the SMTP settings (see
`.env.example` — Gmail example included, requires an
[app password](https://myaccount.google.com/apppasswords), not your normal
password) to get an email whenever a **new** contract starts qualifying.
It only emails once per new entry — it won't re-email you every refresh for
a contract that's still qualifying, only when something new shows up (or
re-qualifies after having dropped off).

## Real-time / fast-moving options: `--watch` mode

Options can move in seconds, so a single run may already be stale by the
time you read it. Use watch mode to keep refreshing:

```bash
python options_scanner.py --watch 10
```

This reruns every 10 seconds (adjust as needed) and overwrites `results.csv`
each pass, printing updates to the terminal live. Press Ctrl+C to stop.

Notes on how fast you can safely go:
- Each refresh cycle makes **one chain request per stock** in your
  watchlist (plus, only the first time per contract, one history call for
  the baseline). A watchlist of a few dozen tickers refreshing every
  5-10 seconds is very reasonable.
- Going below ~5 seconds, or using `--weekly-universe` (several hundred to
  low thousands of symbols) on a short interval, risks hitting Schwab's API
  rate limits. If you see request failures, increase the `--watch`
  interval, trim your watchlist, or lower `UNIVERSE_BUILD_WORKERS`.
- `--interval` controls scanner polling, not the Schwab WebSocket. Stock
  quote updates use Schwab streaming; the browser displays new stream values
  within about one second. Schwab quote cadence and market-data entitlements
  still determine when prices change.

## Notes / things to tune

- **Spread filter**: `MAX_SPREAD_PCT` in `scanner_core.py` controls the 5%
  cutoff. `STRIKE_COUNT` controls how many strikes out it's willing to look
  before giving up on finding a tight-enough spread.
- **Price filter**: `MAX_OPTION_PRICE` in `scanner_core.py` controls the $2
  cutoff — only contracts priced under this are considered.
- **0DTE excluded by design**: same-day expirations are always skipped, so
  SPY/QQQ (which list daily expirations) will use the next trading day's
  expiry instead of today's.
- **Pre-market**: if you run this before 8:30 AM Central, there's no 8:30
  candle yet — the script falls back to the first candle available that day.
- **Weekends/holidays**: there's no trading-day check built in; running this
  when markets are closed will just return stale or empty data.
- **Universe size/workers**: `UNIVERSE_BUILD_WORKERS` in `config.py`
  controls how many symbols are checked concurrently while building the
  weekly-options list; `UNIVERSE_CACHE_PATH` controls where that day's list
  is cached.
- **schwab-py** is the unofficial but widely used Python wrapper for the
  Schwab Trader API (https://github.com/alexgolec/schwab-py). If Schwab
  changes their API, check that project for updates.

## Customizing further

- Want to filter by calls only, puts only, or a further-out expiration
  instead of the nearest one? Adjust `candidate_strikes_by_side()` and the
  `get_option_chain(...)` call in `scanner_core.py` — see schwab-py's docs
  for all available chain parameters.
- Want a different price ceiling or spread cutoff? Change `MAX_OPTION_PRICE`
  / `MAX_SPREAD_PCT` at the top of `scanner_core.py`.




  .\venv\Scripts\python.exe app.py --weekly-universe --interval 300 --port 5001
