#!/usr/bin/env python3
"""
Navexa Demo Agent
=================
Interactive AI assistant that bridges live market data with your Navexa portfolio.

The agent has two categories of tools:
  • Yahoo Finance  — real-time prices, company fundamentals, price history, ticker search
  • Navexa MCP     — read/write your portfolio: holdings, trades, cash, custom prices, reports

Core demo: pull data from outside the platform to enrich your Navexa account (and vice versa).

Usage:
    source .envrc
    python agent.py
"""

import asyncio
import itertools
import json
import os
import sys
import threading
import time
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=PendingDeprecationWarning)
warnings.filterwarnings("ignore", message=".*allowed_objects.*")  # langgraph internal

import yfinance as yf
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient

# ── Config ─────────────────────────────────────────────────────────────────────

NAVEXA_API_KEY     = os.environ.get("NAVEXA_API_KEY", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
MCP_BASE_URL       = os.environ.get("MCP_BASE_URL", "https://mcp.navexa.com")
MCP_MODE           = os.environ.get("MCP_MODE", "manage")
DEMO_MODEL         = os.environ.get("DEMO_MODEL", "claude-sonnet-4-6")

# ── Terminal colours ────────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
CYAN   = "\033[36m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
BLUE   = "\033[34m"
RED    = "\033[31m"


def _hr(char="─", width=70):
    print(DIM + char * width + RESET)


class _Spinner:
    def __init__(self, text="thinking"):
        self._text = text
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def _spin(self):
        for frame in itertools.cycle(["   ", ".  ", ".. ", "..."]):
            if self._stop.is_set():
                break
            sys.stdout.write(f"\r{DIM}{self._text}{frame}{RESET}")
            sys.stdout.flush()
            time.sleep(0.35)
        sys.stdout.write("\r" + " " * (len(self._text) + 3) + "\r")
        sys.stdout.flush()

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join()


class _RateLimiter:
    """Token-bucket rate limiter — enforces a minimum interval between calls."""

    def __init__(self, calls_per_second: float):
        self._interval = 1.0 / calls_per_second
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait = self._interval - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = asyncio.get_event_loop().time()


# ── Yahoo Finance tools ────────────────────────────────────────────────────────

@tool
def get_market_data(symbol: str) -> str:
    """
    Get current market data for a stock from Yahoo Finance.
    Returns price, day change, volume, market cap, and 52-week range.

    For ASX stocks use the .AX suffix (e.g. "BHP.AX", "CBA.AX", "CSL.AX").
    For US stocks use the ticker directly (e.g. "AAPL", "MSFT", "TSLA").
    """
    try:
        ticker = yf.Ticker(symbol)
        info   = ticker.info
        hist   = ticker.history(period="2d")

        if hist.empty:
            return json.dumps({"error": f"No data for '{symbol}'. Check the symbol is correct."})

        current = float(hist["Close"].iloc[-1])
        prev    = float(hist["Close"].iloc[-2]) if len(hist) > 1 else current
        change  = current - prev
        pct     = (change / prev * 100) if prev else 0.0

        return json.dumps({
            "symbol":              symbol,
            "name":                info.get("longName") or info.get("shortName", symbol),
            "currency":            info.get("currency"),
            "current_price":       round(current, 4),
            "day_change":          round(change, 4),
            "day_change_pct":      round(pct, 2),
            "volume":              info.get("volume"),
            "market_cap":          info.get("marketCap"),
            "fifty_two_week_high": info.get("fiftyTwoWeekHigh"),
            "fifty_two_week_low":  info.get("fiftyTwoWeekLow"),
            "exchange":            info.get("exchange"),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_company_info(symbol: str) -> str:
    """
    Get company fundamentals from Yahoo Finance: sector, industry, P/E ratio, EPS,
    dividend yield, revenue, profit margin, analyst target price, and a business summary.

    For ASX stocks use the .AX suffix (e.g. "BHP.AX"). For US stocks use the ticker directly.
    """
    try:
        info = yf.Ticker(symbol).info

        if not info or not info.get("longName"):
            return json.dumps({"error": f"No company data for '{symbol}'."})

        summary = info.get("longBusinessSummary", "") or ""
        return json.dumps({
            "symbol":           symbol,
            "name":             info.get("longName"),
            "sector":           info.get("sector"),
            "industry":         info.get("industry"),
            "country":          info.get("country"),
            "currency":         info.get("currency"),
            "summary":          summary[:500] + ("..." if len(summary) > 500 else ""),
            "employees":        info.get("fullTimeEmployees"),
            "website":          info.get("website"),
            "market_cap":       info.get("marketCap"),
            "pe_ratio":         info.get("trailingPE"),
            "forward_pe":       info.get("forwardPE"),
            "eps":              info.get("trailingEps"),
            "dividend_yield":   info.get("dividendYield"),
            "payout_ratio":     info.get("payoutRatio"),
            "revenue":          info.get("totalRevenue"),
            "profit_margin":    info.get("profitMargins"),
            "return_on_equity": info.get("returnOnEquity"),
            "debt_to_equity":   info.get("debtToEquity"),
            "beta":             info.get("beta"),
            "52_week_high":     info.get("fiftyTwoWeekHigh"),
            "52_week_low":      info.get("fiftyTwoWeekLow"),
            "analyst_target":   info.get("targetMeanPrice"),
            "analyst_rating":   info.get("recommendationKey"),
        }, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_price_history(symbol: str, period: str = "1mo") -> str:
    """
    Get historical closing prices for a stock from Yahoo Finance.

    period options: "1d" | "5d" | "1mo" | "3mo" | "6mo" | "1y" | "2y" | "5y" | "ytd" | "max"

    Returns period return %, high/low/average, and the last 10 daily closing prices.
    Useful for setting custom prices on unlisted holdings or benchmarking performance.

    For ASX stocks use the .AX suffix (e.g. "BHP.AX").
    """
    try:
        hist = yf.Ticker(symbol).history(period=period)

        if hist.empty:
            return json.dumps({"error": f"No history for '{symbol}' over period '{period}'."})

        hist.index = hist.index.strftime("%Y-%m-%d")
        closes = [round(float(v), 4) for v in hist["Close"]]
        dates  = list(hist.index)

        start, end = closes[0], closes[-1]
        total_return = round((end - start) / start * 100, 2) if start else 0.0
        recent = [{"date": d, "close": c} for d, c in zip(dates[-10:], closes[-10:])]

        return json.dumps({
            "symbol":            symbol,
            "period":            period,
            "start_date":        dates[0],
            "end_date":          dates[-1],
            "start_price":       start,
            "end_price":         end,
            "period_return_pct": total_return,
            "period_high":       round(max(closes), 4),
            "period_low":        round(min(closes), 4),
            "average_price":     round(sum(closes) / len(closes), 4),
            "data_points":       len(closes),
            "recent_prices":     recent,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def search_ticker(query: str) -> str:
    """
    Search for stock ticker symbols by company name or keyword using Yahoo Finance.
    Returns matching symbols with their names and exchanges.

    Examples: "BHP", "Commonwealth Bank", "Apple", "Westpac", "lithium ETF"
    """
    try:
        results = []
        for q in yf.Search(query, max_results=8).quotes:
            results.append({
                "symbol":   q.get("symbol"),
                "name":     q.get("longname") or q.get("shortname"),
                "exchange": q.get("exchange"),
                "type":     q.get("quoteType"),
            })
        if not results:
            return json.dumps({"message": f"No results for '{query}'."})
        return json.dumps({"query": query, "results": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── System prompt ───────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a financial assistant demonstrating how AI bridges live market data and a user's \
Navexa investment portfolio.

You have two categories of tools:

**Yahoo Finance tools** — external market data:
- get_market_data: real-time price, day change, volume, market cap, 52-week range
- get_company_info: sector, industry, P/E, EPS, dividend yield, analyst targets, business summary
- get_price_history: historical closing prices, period return stats, and the last 10 daily prices
- search_ticker: find symbols by company name or keyword

**Navexa MCP tools** — the user's portfolio platform:
- Read: portfolios, holdings, trades, cash accounts, performance, timeseries
- Write: add/update trades, set custom prices for unlisted assets, manage cash transactions
- Reports: capital gains, tax, unrealised gains

**Yahoo Finance API limitations — you must understand and respect these:**
- get_price_history returns summary statistics (return %, high, low, average) for the full \
requested period, but only the last 10 individual daily closing prices. If you need more \
individual data points, call the tool multiple times with shorter overlapping periods and \
combine the results.
- Market data is delayed and sourced from Yahoo Finance. It may not reflect the exact \
real-time price on an exchange.
- Not all fields are available for every security. Missing fields will be null — do not \
infer or substitute values.

**Data integrity — non-negotiable rules:**
- NEVER approximate, estimate, or recall financial figures from training data. Every price, \
percentage, quantity, or financial metric you present must come directly from a tool result \
returned in this conversation.
- If a tool returns an error or insufficient data, say so clearly and either retry with \
different parameters or ask the user for clarification. Do not fill gaps with guesses.
- If you are unsure whether a number is accurate, do not state it — fetch it.

**How to help:**
- Look up live stock data and relate it to the user's holdings
- Fetch comparable company data to value unlisted/private assets — then write a custom price back to Navexa
- Research a company before the user adds a trade
- Benchmark portfolio holdings against indices or sector peers
- Pull historical prices to backfill custom prices on unlisted holdings

For ASX stocks, ticker symbols use the .AX suffix (e.g. BHP.AX, CBA.AX, WBC.AX).
Be concise. Lead with the numbers. When you update the portfolio, confirm what changed.\
"""

# ── LLM factory ────────────────────────────────────────────────────────────────

def _build_llm():
    if ANTHROPIC_API_KEY:
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=DEMO_MODEL,
            api_key=ANTHROPIC_API_KEY,
            temperature=0,
            max_tokens=4096,
        )
    elif OPENROUTER_API_KEY:
        from langchain_openai import ChatOpenAI
        model = DEMO_MODEL if DEMO_MODEL.startswith("anthropic/") else f"anthropic/{DEMO_MODEL}"
        return ChatOpenAI(
            model=model,
            api_key=OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
            temperature=0,
            max_tokens=4096,
        )
    else:
        print(f"{RED}Set ANTHROPIC_API_KEY or OPENROUTER_API_KEY in your .envrc{RESET}")
        sys.exit(1)


# ── Tool icons ──────────────────────────────────────────────────────────────────

_TOOL_ICONS = {
    "portfolio": "📁", "holding": "📊", "trade": "💸", "search": "🔍",
    "report": "📋", "cash": "💰", "timeseries": "📈", "price": "💲",
    "tax": "🧾", "market": "📉", "company": "🏢", "history": "📅",
    "ticker": "🔎", "income": "💵", "simulate": "🧮", "custom": "🏷",
}

def _tool_icon(name: str) -> str:
    for key, icon in _TOOL_ICONS.items():
        if key in name.lower():
            return icon
    return "⚙"


# ── Agentic loop with streaming ────────────────────────────────────────────────

async def _run_turn(
    llm_with_tools,
    tool_map: dict,
    messages: list,
    yahoo_tool_names: set,
    navexa_limiter: "_RateLimiter",
) -> None:
    """
    Run one turn of the ReAct loop:
      • Streams text tokens from the LLM
      • Prints tool calls live; red on failure
      • Rate-limits Navexa MCP calls to 5/s
      • Loops until no more tool calls
    """
    spinner = _Spinner()
    spinner.start()
    streaming = False

    while True:
        full_msg = None

        async for chunk in llm_with_tools.astream(messages):
            full_msg = chunk if full_msg is None else full_msg + chunk

            text = chunk.content if isinstance(chunk.content, str) else ""
            if text:
                if not streaming:
                    spinner.stop()
                    streaming = True
                sys.stdout.write(text)
                sys.stdout.flush()

        messages.append(full_msg)

        if not full_msg or not getattr(full_msg, "tool_calls", None):
            break

        if streaming:
            print()
            streaming = False
        spinner.stop()

        tool_results = []
        for tc in full_msg.tool_calls:
            icon     = _tool_icon(tc["name"])
            hint     = next(iter(tc["args"].values()), "") if tc["args"] else ""
            hint_str = f"  {DIM}{str(hint)[:60]}{RESET}" if hint else ""
            print(f"  {CYAN}{icon} {tc['name']}{RESET}{hint_str}")

            # Rate-limit Navexa calls
            if tc["name"] not in yahoo_tool_names:
                await navexa_limiter.acquire()

            err = None
            for attempt in range(2):
                try:
                    result = await tool_map[tc["name"]].ainvoke(tc["args"])
                    err = None
                    break
                except Exception as e:
                    msg = str(e)
                    if attempt == 0 and ("429" in msg or "rate limit" in msg.lower() or "too many" in msg.lower()):
                        print(f"  {YELLOW}⏳ rate limited — waiting 15s...{RESET}")
                        await asyncio.sleep(15)
                        continue
                    err    = msg[:120]
                    result = json.dumps({"error": err})
                    break

            if err:
                print(f"  {RED}✗ {tc['name']}: {err}{RESET}")

            tool_results.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

        messages.extend(tool_results)
        spinner.start()

    spinner.stop()
    if streaming:
        print()


# ── Chat loop ───────────────────────────────────────────────────────────────────

DEMO_HINTS = [
    "What's BHP.AX's current price and key metrics?",
    "Show me my portfolio overview",
    "Look up Apple's fundamentals and analyst rating",
    "Search for lithium ETF tickers",
    "Get 6 months of price history for CBA.AX",
    "I want to update a custom price for an unlisted holding",
]


async def chat() -> None:
    llm = _build_llm()

    yahoo_tools      = [get_market_data, get_company_info, get_price_history, search_ticker]
    yahoo_tool_names = {t.name for t in yahoo_tools}
    navexa_limiter   = _RateLimiter(calls_per_second=5)

    print(BOLD + CYAN + "\nNavexa Demo Agent" + RESET)
    print(DIM + "Connecting to Navexa MCP..." + RESET, end="", flush=True)

    mcp_client = MultiServerMCPClient({
        "navexa": {
            "url":       f"{MCP_BASE_URL}/mcp?mode={MCP_MODE}",
            "transport": "streamable_http",
            "headers":   {"Authorization": f"Bearer {NAVEXA_API_KEY}"},
        }
    })
    mcp_tools = await mcp_client.get_tools()
    all_tools  = yahoo_tools + mcp_tools
    tool_map   = {t.name: t for t in all_tools}

    llm_with_tools = llm.bind_tools(all_tools)

    print(
        f"\r{DIM}Connected  •  "
        f"{len(yahoo_tools)} market data tools  +  {len(mcp_tools)} portfolio tools"
        f"{RESET}"
    )
    print()
    print(DIM + "Try asking:" + RESET)
    for hint in DEMO_HINTS:
        print(DIM + f"  • {hint}" + RESET)
    print()
    print(DIM + "Type 'exit' to quit." + RESET)
    print()

    # Persistent conversation history for the session
    messages = [SystemMessage(content=SYSTEM_PROMPT)]

    while True:
        try:
            user_input = input(BOLD + GREEN + "You: " + RESET).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "q"):
            print("Bye.")
            break

        messages.append(HumanMessage(content=user_input))

        print()
        _hr()
        print(BOLD + BLUE + "Agent:" + RESET)
        print()

        try:
            await _run_turn(llm_with_tools, tool_map, messages, yahoo_tool_names, navexa_limiter)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print(f"\n\n{BOLD}Agent:{RESET} Goodbye! 👋\n")
            return
        except Exception as e:
            print(f"\n{YELLOW}Error: {e}{RESET}")

        print()
        _hr()
        print()


# ── Entry point ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    missing = []
    if not NAVEXA_API_KEY:
        missing.append("NAVEXA_API_KEY")
    if not ANTHROPIC_API_KEY and not OPENROUTER_API_KEY:
        missing.append("ANTHROPIC_API_KEY  or  OPENROUTER_API_KEY")

    if missing:
        print(f"\n{RED}Missing required environment variables:{RESET}")
        for v in missing:
            print(f"  • {v}")
        print(f"\nCopy .envrc.example → .envrc, fill in your keys, then:\n  {BOLD}source .envrc{RESET}\n")
        sys.exit(1)

    try:
        asyncio.run(chat())
    except KeyboardInterrupt:
        print(f"\n{BOLD}Agent:{RESET} Goodbye! 👋\n")
