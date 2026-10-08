"""
Configuration loader for the Schwab options scanner.
Reads credentials and settings from a .env file.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable '{name}'. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


CALLBACK_URL = os.getenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")

# Market-data and trading credentials may belong to separate Schwab apps.
# Keep the legacy variables as a market-data-only fallback for existing setups.
MARKET_DATA_APP_KEY = os.getenv("SCHWAB_MARKET_DATA_APP_KEY") or _require("SCHWAB_APP_KEY")
MARKET_DATA_APP_SECRET = os.getenv("SCHWAB_MARKET_DATA_APP_SECRET") or _require("SCHWAB_APP_SECRET")
MARKET_DATA_TOKEN_PATH = os.getenv(
    "SCHWAB_MARKET_DATA_TOKEN_PATH",
    os.getenv("SCHWAB_TOKEN_PATH", "./schwab_market_data_tokens.json"),
)

TRADING_APP_KEY = os.getenv("SCHWAB_TRADING_APP_KEY")
TRADING_APP_SECRET = os.getenv("SCHWAB_TRADING_APP_SECRET")
TRADING_TOKEN_PATH = os.getenv("SCHWAB_TRADING_TOKEN_PATH", "./schwab_trading_tokens.json")

_paper_trading_setting = os.getenv("PAPER_TRADING", "true").strip().lower()
if _paper_trading_setting not in {"true", "false"}:
    raise RuntimeError("PAPER_TRADING must be set to 'true' or 'false'.")
PAPER_TRADING = _paper_trading_setting == "true"


# --- Paper autopilot ---------------------------------------------------
# Paper-only: simulated orders, never sent to Schwab. Disabled by default.
from datetime import time as _time

AUTOPILOT_ENABLED = os.getenv("AUTOPILOT_ENABLED", "false").strip().lower() == "true"
# "paper": autopilot places simulated orders itself.
# "live": autopilot only creates proposals; each one still needs your review and
# confirmation in the order ticket. Requires PAPER_TRADING=false.
AUTOPILOT_MODE = os.getenv("AUTOPILOT_MODE", "paper").strip().lower()
if AUTOPILOT_MODE not in {"paper", "live"}:
    raise RuntimeError("AUTOPILOT_MODE must be set to 'paper' or 'live'.")
AUTOPILOT_MIN_ABS_DELTA = float(os.getenv("AUTOPILOT_MIN_ABS_DELTA", "0.25"))
AUTOPILOT_MAX_ABS_DELTA = float(os.getenv("AUTOPILOT_MAX_ABS_DELTA", "0.60"))
AUTOPILOT_MIN_DAYS_TO_EXPIRY = int(os.getenv("AUTOPILOT_MIN_DAYS_TO_EXPIRY", "2"))
AUTOPILOT_MIN_OPEN_INTEREST = int(os.getenv("AUTOPILOT_MIN_OPEN_INTEREST", "500"))
AUTOPILOT_MAX_ASK_ABOVE_MID_PCT = float(os.getenv("AUTOPILOT_MAX_ASK_ABOVE_MID_PCT", "5"))
AUTOPILOT_COST_PER_CONTRACT = float(os.getenv("AUTOPILOT_COST_PER_CONTRACT", "1.30"))
AUTOPILOT_SKIP_EXISTING = os.getenv("AUTOPILOT_SKIP_EXISTING", "false").strip().lower() in ("1", "true", "yes", "on")
AUTOPILOT_DAILY_PROFIT_TARGET_PCT = float(os.getenv("AUTOPILOT_DAILY_PROFIT_TARGET_PCT", "2"))
AUTOPILOT_REQUIRE_EMA = os.getenv("AUTOPILOT_REQUIRE_EMA", "true").strip().lower() != "false"
AUTOPILOT_REQUIRE_UP = os.getenv("AUTOPILOT_REQUIRE_UP", "true").strip().lower() != "false"
AUTOPILOT_LIVE_AUTO = os.getenv("AUTOPILOT_LIVE_AUTO", "false").strip().lower() == "true"
AUTOPILOT_LIVE_AUTO_CONFIRM = os.getenv("AUTOPILOT_LIVE_AUTO_CONFIRM", "").strip() == "I_ACCEPT_REAL_MONEY_RISK"
AUTOPILOT_LIVE_ACCOUNT_INDEX = int(os.getenv("AUTOPILOT_LIVE_ACCOUNT_INDEX", "0"))
AUTOPILOT_PROPOSAL_TTL_SECONDS = int(os.getenv("AUTOPILOT_PROPOSAL_TTL_SECONDS", "300"))
AUTOPILOT_MAX_OPEN_POSITIONS = int(os.getenv("AUTOPILOT_MAX_OPEN_POSITIONS", "10"))
AUTOPILOT_RISK_PER_TRADE = float(os.getenv("AUTOPILOT_RISK_PER_TRADE", "0"))
AUTOPILOT_MAX_CONTRACTS = int(os.getenv("AUTOPILOT_MAX_CONTRACTS", "1"))
AUTOPILOT_MAX_DAILY_LOSS = float(os.getenv("AUTOPILOT_MAX_DAILY_LOSS", "250"))
AUTOPILOT_MIN_PRICE = float(os.getenv("AUTOPILOT_MIN_PRICE", "0.10"))
# Share of the account value that may be spent on new entries per day.
AUTOPILOT_DAILY_BUDGET_PCT = float(os.getenv("AUTOPILOT_DAILY_BUDGET_PCT", "50"))
# Simulated account size used for the budget in paper mode.
AUTOPILOT_PAPER_ACCOUNT_SIZE = float(os.getenv("AUTOPILOT_PAPER_ACCOUNT_SIZE", "10000"))
AUTOPILOT_STABILITY_PASSES = int(os.getenv("AUTOPILOT_STABILITY_PASSES", "1"))
AUTOPILOT_TAKE_PROFIT_PCT = float(os.getenv("AUTOPILOT_TAKE_PROFIT_PCT", "15"))
AUTOPILOT_STOP_LOSS_PCT = float(os.getenv("AUTOPILOT_STOP_LOSS_PCT", "10"))
# Central time; no new entries at or after this time.
AUTOPILOT_LAST_ENTRY_TIME = _time.fromisoformat(os.getenv("AUTOPILOT_LAST_ENTRY_TIME", "14:30"))


def has_trading_credentials() -> bool:
    return bool(TRADING_APP_KEY and TRADING_APP_SECRET)

# Local time considered "market open" for the change baseline.
# America/Chicago automatically handles CST vs CDT for you.
# NOTE: the market opens at 9:30 AM ET, which is 8:30 AM Central -- not
# 9:30 AM Central. This is intentionally set to the correct market-open
# time in Central.
#
# Every "change" column compares the current option price to THIS one
# fixed baseline (market open) all day long -- there is no midday reset.
BASELINE_TIMEZONE = "America/Chicago"
BASELINE_HOUR = 8
BASELINE_MINUTE = 30

# Second fixed baseline used alongside the 8:30 AM open baseline. The
# dashboard shows both as separate tables: "since 8:30 AM open" and
# "since 12:00 PM". Before this time each day, the 12:00 PM columns are
# simply blank (no midday price exists yet).
NOON_HOUR = 12
NOON_MINUTE = 0

# --- EMA trend filter --------------------------------------------------
# Powers the "EMA8 > EMA21" table: pulls each underlying's 5-minute closes
# and flags symbols in a short-term uptrend (8-bar EMA above 21-bar EMA).
# Refreshed on its own cadence to avoid hammering the API once per refresh.
EMA_FAST = 8
EMA_SLOW = 21
EMA_LOOKBACK_DAYS = 10        # calendar days of 5-minute candles to pull
EMA_REFRESH_SECONDS = 300     # how often to recompute EMAs per symbol
EMA_MAX_SPREAD_PCT = 10.0     # spread cutoff for the EMA table specifically

# --- Weekly-options universe (replaces watchlist.txt) -----------------
# Instead of scanning a fixed list of tickers from a file, the scanner
# builds its own universe of every stock that currently has WEEKLY
# (not just monthly) options, using the Schwab API itself.
#
# Step 1 needs *some* fast source of candidate tickers to check, since
# Schwab's API has no "list all symbols" endpoint. We use NASDAQ's own
# free, fast symbol-directory files (NOT CBOE, which is slow) as that
# starting point, then ask Schwab's expiration-chain endpoint which of
# those actually have weekly expirations.
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
# Underlying share-price bounds applied only by the --weekly-universe scanner.
WEEKLY_MIN_UNDERLYING_PRICE = 25.0
WEEKLY_MAX_UNDERLYING_PRICE = 500.0

# Where the built weekly-optionable universe is cached. Rebuilt at most
# once per day (it changes slowly) unless --rebuild-universe is passed.
UNIVERSE_CACHE_PATH = "weekly_universe_cache.json"

# How many symbols to probe concurrently against Schwab's expiration-chain
# endpoint while building the universe. Keep this modest to stay well
# under Schwab's API rate limits.
UNIVERSE_BUILD_WORKERS = int(os.getenv("UNIVERSE_BUILD_WORKERS", "8"))

# Email alerts (optional) -- fires when a new contract starts qualifying
# on any dashboard tab. Leave EMAIL_ENABLED=false to disable entirely.
EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "false").strip().lower() == "true"
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "") or SMTP_USER
EMAIL_TO = os.getenv("EMAIL_TO", "")
