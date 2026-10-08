"""
Handles Schwab OAuth login and returns an authenticated client.

First run: opens a browser for you to log in to Schwab and approve the app.
After that, the token is cached at SCHWAB_TOKEN_PATH and reused automatically
(schwab-py refreshes it for you) so you won't have to log in every time.
"""
import schwab

import config


def _get_client(api_key: str, app_secret: str, token_path: str, interactive: bool):
    return schwab.auth.easy_client(
        api_key=api_key,
        app_secret=app_secret,
        callback_url=config.CALLBACK_URL,
        token_path=token_path,
        interactive=interactive,
    )


def get_client():
    """Return the authenticated Market Data Schwab client."""
    return _get_client(
        config.MARKET_DATA_APP_KEY,
        config.MARKET_DATA_APP_SECRET,
        config.MARKET_DATA_TOKEN_PATH,
        interactive=True,
    )


def get_trading_client():
    """Return the authenticated Accounts and Trading client, opening OAuth without terminal input."""
    if not config.has_trading_credentials():
        raise RuntimeError(
            "Add SCHWAB_TRADING_APP_KEY and SCHWAB_TRADING_APP_SECRET to your local .env "
            "using the credentials for the Accounts and Trading Production app."
        )
    return _get_client(
        config.TRADING_APP_KEY,
        config.TRADING_APP_SECRET,
        config.TRADING_TOKEN_PATH,
        interactive=False,
    )
