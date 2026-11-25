"""MCP server that exposes Twelve Data stock prices to an MCP client.

Run with: uv run server.py   (stdio transport, which Claude Desktop uses)
"""

import logging
from collections.abc import Awaitable
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError

import stock_formatter
from stock_formatter import StockDataError

# With the stdio transport, stdout carries the protocol, so never print().
# The logging module writes to stderr, which Claude Desktop saves in its MCP log.
log = logging.getLogger("stock-mcp")

mcp = MCPServer(
    "stock-prices",
    instructions=(
        "Current and historical stock prices from Twelve Data. "
        "Symbols are exchange tickers such as AAPL. Dates are YYYY-MM-DD."
    ),
)


async def _for_tool(call: Awaitable[str]) -> str:
    """Turn an expected failure into a ToolError, which the model gets to read."""
    try:
        return await call
    except StockDataError as exc:
        raise ToolError(str(exc)) from exc


async def _for_resource(call: Awaitable[str]) -> str:
    try:
        return await call
    except StockDataError as exc:
        raise ResourceError(str(exc)) from exc


# ==========================================
# TOOLS: the model calls these on its own when a question needs them.
# ==========================================


@mcp.tool()
async def get_current_price(symbol: str) -> str:
    """Get the latest price for one stock, with the day's change, range and volume.

    Args:
        symbol: Ticker symbol, for example AAPL, MSFT or TSLA.
    """
    log.info("Tool call: get_current_price(%s)", symbol)
    return await _for_tool(stock_formatter.get_current_price(symbol))


@mcp.tool()
async def get_multiple_prices(symbols: list[str]) -> str:
    """Get the latest price for several stocks at once. Use this to compare stocks.

    Args:
        symbols: Up to 8 ticker symbols, for example ["AAPL", "MSFT", "NVDA"].
    """
    log.info("Tool call: get_multiple_prices(%s)", symbols)
    return await _for_tool(stock_formatter.get_multiple_prices(symbols))


@mcp.tool()
async def get_closing_price(symbol: str, date: str) -> str:
    """Get a stock's open, high, low, close and volume for one specific past day.

    Weekends and market holidays have no data; the reply says so.

    Args:
        symbol: Ticker symbol, for example AAPL, MSFT or TSLA.
        date: The trading day, as YYYY-MM-DD, for example 2026-01-15.
    """
    log.info("Tool call: get_closing_price(%s, %s)", symbol, date)
    return await _for_tool(stock_formatter.get_closing_price(symbol, date))


@mcp.tool()
async def get_historical_prices(
    symbol: str,
    start_date: str,
    end_date: str,
    interval: Literal["1day", "1week", "1month"] = "1day",
) -> str:
    """Get a stock's price history between two dates, for trends over a period.

    Returns a summary of the move over the period followed by CSV rows, oldest first.

    Args:
        symbol: Ticker symbol, for example AAPL, MSFT or TSLA.
        start_date: First day of the range, as YYYY-MM-DD.
        end_date: Last day of the range, as YYYY-MM-DD.
        interval: Size of each row. Use 1week or 1month for ranges longer than a year.
    """
    log.info("Tool call: get_historical_prices(%s, %s, %s, %s)", symbol, start_date, end_date, interval)
    return await _for_tool(
        stock_formatter.get_historical_prices(symbol, start_date, end_date, interval)
    )


# ==========================================
# RESOURCES: the same data, addressable by URI.
# ==========================================


@mcp.resource("stock://{symbol}/price", mime_type="text/plain")
async def current_price_resource(symbol: str) -> str:
    """Latest price for a ticker symbol, for example stock://AAPL/price."""
    log.info("Resource read: stock://%s/price", symbol)
    return await _for_resource(stock_formatter.get_current_price(symbol))


@mcp.resource("stock://{symbol}/closingdate/{date}", mime_type="text/plain")
async def closing_price_resource(symbol: str, date: str) -> str:
    """One day's prices, for example stock://AAPL/closingdate/2026-01-15."""
    log.info("Resource read: stock://%s/closingdate/%s", symbol, date)
    return await _for_resource(stock_formatter.get_closing_price(symbol, date))


@mcp.resource("stock://{symbol}/history/{start_date}/{end_date}", mime_type="text/plain")
async def historical_prices_resource(symbol: str, start_date: str, end_date: str) -> str:
    """Daily prices between two dates, for example stock://AAPL/history/2026-01-01/2026-01-31."""
    log.info("Resource read: stock://%s/history/%s/%s", symbol, start_date, end_date)
    return await _for_resource(stock_formatter.get_historical_prices(symbol, start_date, end_date))


# ==========================================
# PROMPTS: ready-made requests the user can pick in the client.
# ==========================================


@mcp.prompt(title="Current stock price")
def current_price(symbol: str) -> str:
    """Ask for the current price of a stock. symbol is a ticker such as AAPL."""
    return (
        f"Use the get_current_price tool to look up {symbol.strip().upper()}. "
        "Tell me the price, how it has moved today, and whether the market is open."
    )


@mcp.prompt(title="Closing price on a date")
def closing_price(symbol: str, date: str) -> str:
    """Ask what a stock closed at on one day. symbol is a ticker such as AAPL; date is YYYY-MM-DD."""
    return (
        f"Use the get_closing_price tool to get {symbol.strip().upper()} on {date} "
        "(the date is YYYY-MM-DD). Tell me the close and the day's range. "
        "If the market was closed that day, use the nearest earlier trading day and say so."
    )


@mcp.prompt(title="Historical stock prices")
def historical_prices(symbol: str, start_date: str, end_date: str) -> str:
    """Ask for a stock's price history. symbol is a ticker such as AAPL; dates are YYYY-MM-DD."""
    return (
        f"Use the get_historical_prices tool to get {symbol.strip().upper()} "
        f"from {start_date} to {end_date} (dates are YYYY-MM-DD). "
        "Summarize the overall change, the high and low, and any notable moves."
    )


@mcp.prompt(title="Stock server usage guide")
def usage_guide() -> str:
    """Explain everything this server offers and the formats it expects."""
    return """Here is what the stock-prices MCP server offers. Use it to answer my stock questions.

TOOLS
- get_current_price(symbol): latest price, today's change, day range, volume.
- get_multiple_prices(symbols): the same for up to 8 symbols in one call.
- get_closing_price(symbol, date): open, high, low, close and volume for one past day.
- get_historical_prices(symbol, start_date, end_date, interval): history over a range;
  interval is 1day, 1week or 1month.

RESOURCES
- stock://{symbol}/price
- stock://{symbol}/closingdate/{date}
- stock://{symbol}/history/{start_date}/{end_date}

FORMATS
- Symbols are ticker symbols such as AAPL, MSFT, GOOGL. Case does not matter.
- Dates are YYYY-MM-DD, for example 2026-01-15.
- Weekends and market holidays have no prices.

LIMITS
- Data comes from the Twelve Data API. The free plan allows 8 requests per minute,
  and each symbol counts as one request. If a call fails with a rate-limit error,
  wait a minute before retrying.

Start by telling me briefly what you can look up, then ask which stock I am interested in."""


def main() -> None:
    log.info("Starting stock-prices MCP server on stdio")
    mcp.run()


if __name__ == "__main__":
    main()
