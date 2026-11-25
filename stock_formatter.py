"""Stock data from the Twelve Data REST API, formatted as plain text.

This module knows nothing about MCP. server.py wraps these functions as
tools and resources.
"""

import logging
import os
import re
from datetime import date, timedelta
from pathlib import Path

import httpx
from dotenv import dotenv_values

BASE_URL = "https://api.twelvedata.com"
INTERVALS = ("1day", "1week", "1month")
MAX_ROWS = 400
MAX_SYMBOLS = 8  # one API credit per symbol; the free plan allows 8 per minute

# Claude Desktop starts the server with an arbitrary working directory, so
# look for .env next to this file instead of in the current directory.
ENV_FILE = Path(__file__).parent / ".env"

# httpx logs every request URL at INFO, which is noise in the client's MCP log.
logging.getLogger("httpx").setLevel(logging.WARNING)


class StockDataError(Exception):
    """A problem the caller can act on: bad input, missing key, API error."""


def _clean_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9.\-/:]{1,20}", symbol):
        raise StockDataError(
            f"'{symbol}' is not a valid ticker symbol. Use a ticker such as AAPL or MSFT."
        )
    return symbol


def _clean_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise StockDataError(
            f"{label} '{value}' is not a valid date. Use YYYY-MM-DD, for example 2026-01-31."
        ) from None


def _api_key() -> str:
    """Read the key on every call, so editing .env works without a restart.

    A non-empty environment variable wins. An empty one is treated as unset,
    because some launchers pass the variable through with no value.
    """
    name = "TWELVE_DATA_API_KEY"
    return (os.environ.get(name) or "").strip() or (dotenv_values(ENV_FILE).get(name) or "").strip()


async def _get(endpoint: str, params: dict[str, str], empty_ok: bool = False) -> dict:
    api_key = _api_key()
    if not api_key:
        raise StockDataError(
            "TWELVE_DATA_API_KEY is not set. Add it to the .env file in the "
            "stock-mcp folder and restart the MCP client."
        )

    try:
        # The key goes in a header so it never appears in a URL or a log line.
        headers = {"Authorization": f"apikey {api_key}"}
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=15, headers=headers) as client:
            response = await client.get(endpoint, params=params)
            data = response.json()
    except httpx.HTTPError as exc:
        raise StockDataError(f"Could not reach Twelve Data: {exc}") from exc
    except ValueError:
        raise StockDataError(
            f"Twelve Data returned an unreadable response (HTTP {response.status_code})."
        ) from None

    # Twelve Data reports most errors in the body, often with HTTP 200.
    if isinstance(data, dict) and data.get("status") == "error":
        # Twelve Data answers 400 "No data is available" for a day with no trading.
        if empty_ok and data.get("code") == 400 and "no data" in str(data.get("message", "")).lower():
            return {}
        raise StockDataError(f"Twelve Data error {data.get('code')}: {data.get('message')}")
    return data


def _money(value: str | None, currency: str) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):,.2f} {currency}".strip()


def _format_quote(quote: dict, symbol: str) -> str:
    currency = quote.get("currency", "")
    market = "open" if quote.get("is_market_open") else "closed"
    change = float(quote.get("change") or 0)
    percent = float(quote.get("percent_change") or 0)

    return "\n".join(
        [
            f"{quote.get('name', symbol)} ({quote.get('symbol', symbol)}) on {quote.get('exchange', 'n/a')}",
            f"Price: {_money(quote.get('close'), currency)} as of {quote.get('datetime', 'n/a')} (market {market})",
            f"Change: {change:+,.2f} ({percent:+.2f}%) from previous close {_money(quote.get('previous_close'), currency)}",
            f"Day range: {_money(quote.get('low'), currency)} to {_money(quote.get('high'), currency)}",
            f"Open: {_money(quote.get('open'), currency)}",
            f"Volume: {int(float(quote.get('volume') or 0)):,}",
        ]
    )


async def get_current_price(symbol: str) -> str:
    """Latest quote for one symbol, as a short text summary."""
    symbol = _clean_symbol(symbol)
    return _format_quote(await _get("/quote", {"symbol": symbol}), symbol)


async def get_multiple_prices(symbols: list[str]) -> str:
    """Latest quotes for several symbols in one request."""
    cleaned = list(dict.fromkeys(_clean_symbol(s) for s in symbols))
    if not cleaned:
        raise StockDataError("Give at least one ticker symbol.")
    if len(cleaned) > MAX_SYMBOLS:
        raise StockDataError(f"Ask for at most {MAX_SYMBOLS} symbols at a time, not {len(cleaned)}.")
    if len(cleaned) == 1:
        return await get_current_price(cleaned[0])

    # With several symbols the response is keyed by symbol, and a symbol that
    # fails carries its own error instead of failing the whole request.
    data = await _get("/quote", {"symbol": ",".join(cleaned)})
    sections = []
    for symbol in cleaned:
        quote = data.get(symbol) or {}
        if not quote or quote.get("status") == "error":
            sections.append(f"{symbol}: no data ({quote.get('message', 'not returned by Twelve Data')})")
        else:
            sections.append(_format_quote(quote, symbol))
    return "\n\n".join(sections)


async def get_closing_price(symbol: str, on_date: str) -> str:
    """Open, high, low, close and volume for one trading day."""
    symbol = _clean_symbol(symbol)
    day = _clean_date(on_date, "date")
    if day > date.today():
        raise StockDataError(f"{day} is in the future.")
    if day.weekday() >= 5:
        return f"{day} is a {day:%A}, so markets were closed. Try the Friday before or the Monday after."

    data = await _get(
        "/time_series",
        {
            "symbol": symbol,
            "interval": "1day",
            "start_date": day.isoformat(),
            "end_date": (day + timedelta(days=1)).isoformat(),
        },
        empty_ok=True,
    )
    rows = data.get("values") or []
    if not rows:
        return f"No trading data for {symbol} on {day}. It may have been a market holiday, or today's session has not finished."

    row = rows[0]
    currency = data.get("meta", {}).get("currency", "")
    return "\n".join(
        [
            f"{symbol} on {row['datetime']} ({data.get('meta', {}).get('exchange', 'n/a')})",
            f"Close: {_money(row['close'], currency)}",
            f"Open: {_money(row['open'], currency)}",
            f"Day range: {_money(row['low'], currency)} to {_money(row['high'], currency)}",
            f"Volume: {int(float(row.get('volume') or 0)):,}",
        ]
    )


async def get_historical_prices(
    symbol: str, start_date: str, end_date: str, interval: str = "1day"
) -> str:
    """Open/high/low/close/volume rows between two dates, oldest first."""
    symbol = _clean_symbol(symbol)
    start = _clean_date(start_date, "start_date")
    end = _clean_date(end_date, "end_date")
    if start > end:
        raise StockDataError(f"start_date {start} is after end_date {end}.")
    if interval not in INTERVALS:
        raise StockDataError(f"interval must be one of {', '.join(INTERVALS)}, not '{interval}'.")

    data = await _get(
        "/time_series",
        {
            "symbol": symbol,
            "interval": interval,
            "start_date": start.isoformat(),
            # Twelve Data stops before end_date, so ask for one day more.
            "end_date": (end + timedelta(days=1)).isoformat(),
            "order": "asc",
            "outputsize": str(MAX_ROWS),
        },
    )

    rows = data.get("values") or []
    if not rows:
        return f"No price data for {symbol} between {start} and {end}."

    currency = data.get("meta", {}).get("currency", "")
    first_close = float(rows[0]["close"])
    last_close = float(rows[-1]["close"])
    change = last_close - first_close
    percent = change / first_close * 100 if first_close else 0.0

    lines = [
        f"{symbol} {interval} prices in {currency or 'local currency'}, "
        f"{rows[0]['datetime']} to {rows[-1]['datetime']} ({len(rows)} rows)",
        f"Close moved from {first_close:,.2f} to {last_close:,.2f}: {change:+,.2f} ({percent:+.2f}%)",
        f"Highest high: {max(float(r['high']) for r in rows):,.2f}. "
        f"Lowest low: {min(float(r['low']) for r in rows):,.2f}.",
        "",
        "date,open,high,low,close,volume",
    ]
    lines += [
        f"{r['datetime']},{float(r['open']):.2f},{float(r['high']):.2f},"
        f"{float(r['low']):.2f},{float(r['close']):.2f},{r.get('volume', '')}"
        for r in rows
    ]
    if len(rows) == MAX_ROWS:
        lines.append(f"(Stopped at {MAX_ROWS} rows. Use a shorter range or a longer interval for the rest.)")
    return "\n".join(lines)
