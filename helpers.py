import yfinance as yf
import math
import os
import random
import logging
import time

from flask import redirect, render_template, session
from functools import wraps
import requests

logger = logging.getLogger(__name__)


def apology(message, code=400):
    """Render message as an apology to user."""

    def escape(s):
        """
        Escape special characters.

        https://github.com/jacebrowning/memegen#special-characters
        """
        for old, new in [
            ("-", "--"),
            (" ", "-"),
            ("_", "__"),
            ("?", "~q"),
            ("%", "~p"),
            ("#", "~h"),
            ("/", "~s"),
            ('"', "''"),
        ]:
            s = s.replace(old, new)
        return s

    return render_template("apology.html", top=code, bottom=escape(message)), code


def login_required(f):
    """
    Decorate routes to require login.

    https://flask.palletsprojects.com/en/latest/patterns/viewdecorators/
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get("user_id") is None:
            return redirect("/login")
        return f(*args, **kwargs)

    return decorated_function



def _lookup_yfinance(symbol):
    """Try to look up a quote using yfinance (works locally, often blocked on cloud servers)."""
    ticker = yf.Ticker(symbol)
    
    # Get historical data for the latest price as well as 7d and 30d performance
    data = ticker.history(period="1mo")
    closes = []
    if not data.empty:
        data = data.dropna(subset=['Close'])
        closes = [float(c) for c in data['Close'] if not math.isnan(c)]

    if closes:
        latest_price = closes[-1]
        price_30d = closes[0]
        price_7d = closes[-6] if len(closes) >= 6 else closes[0]
    else:
        fi = ticker.fast_info
        p = getattr(fi, 'last_price', None) or getattr(fi, 'previous_close', None)
        if p is not None and not math.isnan(p):
            latest_price = price_7d = price_30d = float(p)
        else:
            return None
    
    # Metadata is optional; Yahoo can return prices while blocking quoteSummary.
    try:
        info = ticker.info
    except Exception as e:
        logger.warning(f"Error fetching metadata for {symbol}: {e}")
        info = {}

    name = info.get('longName') or info.get('shortName') or symbol.upper()
    sector = info.get('sector') or ""
    raw_exchange = info.get('exchange') or "Unknown"
    
    exchange_map = {
        "NMS": "NASDAQ",
        "NYQ": "NYSE",
        "ASE": "NYSE American",
        "NGM": "NASDAQ",
        "PCX": "NYSE Arca",
        "TOR": "TSX",
        "VAN": "TSX Venture",
        "CME": "CME",
        "CMX": "COMEX",
        "NYM": "NYMEX",
        "CBT": "CBOT",
        "ICE": "ICE",
        "PNK": "OTC",
        "LSE": "London SE",
        "FRA": "Frankfurt SE",
        "NCM": "NASDAQ",
        "BATS": "Cboe BZX",
        "ENX": "Euronext"
    }
    
    exchange = exchange_map.get(raw_exchange, raw_exchange)
    
    description = info.get('longBusinessSummary') or info.get('description') or "No description available."
    
    return {
        "name": name,
        "price": float(latest_price),
        "price_7d": float(price_7d),
        "price_30d": float(price_30d),
        "symbol": symbol.upper(),
        "sector": sector,
        "exchange": exchange,
        "description": description
    }


def _lookup_fmp(symbol):
    """Fallback: Look up a quote using Financial Modeling Prep API (works on cloud servers)."""
    import requests
    fmp_key = os.environ.get("FMP_API_KEY", "")
    if not fmp_key:
        logger.warning("FMP_API_KEY not set — FMP fallback unavailable.")
        return None
    
    try:
        # Get real-time quote
        url = f"https://financialmodelingprep.com/stable/quote/{symbol}"
        resp = requests.get(url, params={"apikey": fmp_key}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        if not data or (isinstance(data, list) and len(data) == 0):
            return None
        
        quote = data[0] if isinstance(data, list) else data
        price = quote.get("price") or quote.get("previousClose")
        if not price:
            return None
        
        # FMP doesn't give 7d/30d in the quote endpoint — use current price as estimate
        prev_close = quote.get("previousClose", price)
        
        # Try to get historical for 7d/30d prices
        price_7d = price
        price_30d = price
        try:
            hist_url = f"https://financialmodelingprep.com/stable/historical-price-eod/light/{symbol}"
            hist_resp = requests.get(hist_url, params={"apikey": fmp_key, "from": "", "to": ""}, timeout=10)
            if hist_resp.status_code == 200:
                hist_data = hist_resp.json()
                if isinstance(hist_data, list) and len(hist_data) >= 6:
                    price_7d = hist_data[min(5, len(hist_data)-1)].get("close", price)
                if isinstance(hist_data, list) and len(hist_data) >= 22:
                    price_30d = hist_data[min(21, len(hist_data)-1)].get("close", price)
        except Exception:
            pass
        
        return {
            "name": quote.get("name") or symbol.upper(),
            "price": float(price),
            "price_7d": float(price_7d),
            "price_30d": float(price_30d),
            "symbol": symbol.upper(),
            "sector": "",
            "exchange": quote.get("exchange") or "Unknown",
            "description": "No description available."
        }
    except Exception as e:
        logger.error(f"FMP lookup failed for {symbol}: {e}")
        return None


def _twelvedata_series(symbol, outputsize=35):
    """Fetch daily OHLCV data from Twelve Data."""
    api_key = os.environ.get("TWELVE_DATA_API_KEY") or os.environ.get("TWELVEDATA_API_KEY", "")
    if not api_key:
        return []
    response = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": symbol,
            "interval": "1day",
            "outputsize": outputsize,
            "order": "ASC",
            "apikey": api_key,
        },
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("status") == "error" or "values" not in data:
        logger.warning("Twelve Data returned no series for %s: %s", symbol, data.get("message", "unknown error"))
        return []
    return data["values"]


def _twelvedata_quote(symbol):
    """Fetch the latest quote from Twelve Data's real-time quote endpoint."""
    api_key = os.environ.get("TWELVE_DATA_API_KEY") or os.environ.get("TWELVEDATA_API_KEY", "")
    if not api_key:
        return None
    response = requests.get(
        "https://api.twelvedata.com/quote",
        params={"symbol": symbol, "apikey": api_key},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("status") == "error" or not data.get("close"):
        return None
    return data


def _lookup_twelvedata(symbol):
    """Look up a live quote and recent performance using Twelve Data."""
    quote = _twelvedata_quote(symbol)
    try:
        rows = _twelvedata_series(symbol)
    except Exception as e:
        logger.warning("Twelve Data history failed for %s: %s", symbol, e)
        rows = []
    closes = [float(row["close"]) for row in rows if row.get("close") is not None]
    if not quote and not closes:
        return None
    price = float(quote["close"]) if quote else closes[-1]
    previous_close = float(quote.get("previous_close", price)) if quote else price
    exchange_map = {
        "NMS": "NASDAQ", "NYQ": "NYSE", "ASE": "NYSE American",
        "NGM": "NASDAQ", "PCX": "NYSE Arca", "TOR": "TSX",
        "VAN": "TSX Venture", "CME": "CME", "CMX": "COMEX",
        "NYM": "NYMEX", "CBT": "CBOT", "ICE": "ICE", "PNK": "OTC",
        "LSE": "London SE", "FRA": "Frankfurt SE",
        "NCM": "NASDAQ", "BATS": "Cboe BZX", "ENX": "Euronext",
    }
    raw_exchange = (quote or {}).get("exchange") or ""
    exchange = exchange_map.get(raw_exchange, raw_exchange) or ""
    # ~5 trading days ≈ 7 calendar days; ~22 trading days ≈ 30 calendar days.
    # closes[0] with outputsize=35 is ~49 calendar days back, which distorted "monthly".
    price_7d = closes[-5] if len(closes) >= 5 else previous_close
    price_30d = closes[-22] if len(closes) >= 22 else (closes[0] if closes else previous_close)
    return {
        "name": (quote or {}).get("name") or symbol.upper(),
        "price": price,
        "price_7d": price_7d,
        "price_30d": price_30d,
        "symbol": symbol.upper(),
        "sector": "",
        "exchange": exchange,
        "description": "No description available.",
    }


def _lse_candles(symbol, limit=35, timeframe="1d", order="asc"):
    """Fetch candles from the London Strategic Edge market-data API."""
    api_key = os.environ.get("LSE_API_KEY", "")
    if not api_key:
        return []
    base_url = os.environ.get(
        "LSE_API_URL", "https://api.londonstrategicedge.com/vault"
    ).rstrip("/")
    response = requests.get(
        f"{base_url}/candles",
        headers={"x-api-key": api_key},
        params={
            "symbol": symbol,
            "timeframe": timeframe,
            "limit": limit,
            "order": order,
        },
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        logger.warning("London Strategic Edge returned an invalid candle response for %s", symbol)
        return []
    return data


def _lookup_lse(symbol):
    """Look up a live quote and recent performance using LSE candles."""
    try:
        rows = _lse_candles(symbol, limit=1, timeframe="1m", order="desc")
    except Exception as e:
        logger.warning("London Strategic Edge intraday data failed for %s: %s", symbol, e)
        rows = []
    try:
        daily_rows = _lse_candles(symbol, limit=35, timeframe="1d")
    except Exception as e:
        logger.warning("London Strategic Edge daily data failed for %s: %s", symbol, e)
        daily_rows = []
    if not rows:
        rows = daily_rows
    closes = [float(row["close"]) for row in rows if row.get("close") is not None]
    if not closes:
        return None
    daily_closes = [float(row["close"]) for row in daily_rows if row.get("close") is not None]
    price_7d = daily_closes[-5] if len(daily_closes) >= 5 else closes[0]
    price_30d = daily_closes[-22] if len(daily_closes) >= 22 else (daily_closes[0] if daily_closes else closes[0])
    return {
        "name": symbol.upper(),
        "price": closes[0],
        "price_7d": price_7d,
        "price_30d": price_30d,
        "symbol": symbol.upper(),
        "sector": "",
        "exchange": "London Strategic Edge",
        "description": "No description available.",
    }


def _is_futures(symbol):
    """Return True if the symbol is a known futures/commodity instrument."""
    s = symbol.upper()
    if s.endswith("=F"):
        return True
    futures_aliases = {
        "BCO/USD", "UKOIL", "WTI", "WTICO/USD", "XAU/USD", "XAG/USD",
        "XCU/USD", "SOYBN/USD", "CORN/USD", "WHEAT/USD", "NATGAS/USD",
    }
    return s in futures_aliases


_metadata_cache = {}
_METADATA_TTL = 3600  # second(s); static sector/exchange data barely changes


def _meta_get(symbol, source=""):
    key = f"{source}:{symbol}" if source else symbol
    entry = _metadata_cache.get(key)
    if entry and time.time() - entry[0] < _METADATA_TTL:
        return entry[1]
    return None


def _meta_set(symbol, meta, source=""):
    key = f"{source}:{symbol}" if source else symbol
    _metadata_cache[key] = (time.time(), meta)


_yahoo_session = {"ts": 0, "session": None, "crumb": ""}


def _get_yahoo_crumb():
    """Return (session, crumb) for Yahoo's crumb-protected endpoints.
    The A3 cookie + crumb are cached for 10 minutes and shared."""
    now = time.time()
    if _yahoo_session["session"] and now - _yahoo_session["ts"] < 600:
        return _yahoo_session["session"], _yahoo_session["crumb"]
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"})
        session.get("https://fc.yahoo.com", timeout=4)
        resp = session.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=4)
        if resp.status_code != 200 or not resp.text.strip():
            session.close()
            return None, ""
        _yahoo_session["ts"] = now
        _yahoo_session["session"] = session
        _yahoo_session["crumb"] = resp.text.strip()
        return _yahoo_session["session"], _yahoo_session["crumb"]
    except Exception as e:
        logger.warning("Yahoo crumb setup failed: %s", e)
        return None, ""


def _yahoo_meta_v1(symbol):
    """Sector + exchange from the Yahoo v1 search endpoint.
    Returns {} on failure. Requires NO crumb and is reachable from Render
    (the app's own /search endpoint proxies it)."""
    exchange_map = {
        "NMS": "NASDAQ", "NYQ": "NYSE", "ASE": "NYSE American",
        "NGM": "NASDAQ", "PCX": "NYSE Arca", "TOR": "TSX",
        "VAN": "TSX Venture", "CME": "CME", "CMX": "COMEX",
        "NYM": "NYMEX", "CBT": "CBOT", "ICE": "ICE", "PNK": "OTC",
        "LSE": "London SE", "FRA": "Frankfurt SE",
        "NCM": "NASDAQ", "BATS": "Cboe BZX", "ENX": "Euronext",
    }
    try:
        resp = requests.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": symbol, "quotesCount": 5, "newsCount": 0, "enableFuzzyQuery": "false"},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"},
            timeout=5,
        )
        if resp.status_code != 200:
            return {}
        quotes = resp.json().get("quotes") or []
        if not quotes:
            return {}
        q = next((x for x in quotes if (x.get("symbol") or "").upper() == symbol), quotes[0])
        raw_ex = q.get("exchange") or ""
        exch_disp = q.get("exchDisp") or ""
        return {
            "sector": q.get("sector") or q.get("sectorDisp") or "",
            "exchange": exch_disp or exchange_map.get(raw_ex, raw_ex) or "",
            "description": "",
        }
    except Exception as e:
        logger.warning("Yahoo v1 search metadata failed for %s: %s", symbol, e)
        return {}


def _yahoo_quote_summary(symbol):
    """Long business description (+ sector/exchange fallback) from quoteSummary.
    Requires a crumb; may be IP-blocked on some hosts. Returns {} on failure."""
    session, crumb = _get_yahoo_crumb()
    if session is None or not crumb:
        return {}
    exchange_map = {
        "NMS": "NASDAQ", "NYQ": "NYSE", "ASE": "NYSE American",
        "NGM": "NASDAQ", "PCX": "NYSE Arca", "TOR": "TSX",
        "VAN": "TSX Venture", "CME": "CME", "CMX": "COMEX",
        "NYM": "NYMEX", "CBT": "CBOT", "ICE": "ICE", "PNK": "OTC",
        "LSE": "London SE", "FRA": "Frankfurt SE",
        "NCM": "NASDAQ", "BATS": "Cboe BZX", "ENX": "Euronext",
    }
    try:
        url = (
            f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
            "?modules=assetProfile,summaryDetail"
        )
        resp = session.get(url, params={"crumb": crumb}, timeout=4)
        if resp.status_code != 200:
            logger.warning("Yahoo quoteSummary HTTP %s for %s", resp.status_code, symbol)
            return {}
        body = resp.json()
        result = (body.get("quoteSummary") or {}).get("result") or []
        if not result:
            return {}
        profile = result[0].get("assetProfile") or {}
        raw_ex = result[0].get("summaryDetail", {}).get("exchange") or ""
        return {
            "sector": profile.get("sector") or "",
            "exchange": exchange_map.get(raw_ex, raw_ex) or "",
            "description": profile.get("longBusinessSummary") or "",
        }
    except Exception as e:
        logger.warning("Yahoo quoteSummary fetch failed for %s: %s", symbol, e)
        return {}


def _yahoo_metadata(symbol):
    """Fetch static metadata (sector, exchange, description) for a stock.
    Primary source: /v1/finance/search (no crumb, reachable from Render).
    Secondary: /v10/finance/quoteSummary (crumb-protected) for the long business
    description and sector/exchange fallback."""
    symbol = symbol.upper()
    cached = _meta_get(symbol, "yahoo")
    if cached is not None:
        return cached

    meta = _yahoo_meta_v1(symbol) or {}

    # Secondary: quoteSummary for the long description (+ sector/exchange fallback)
    if not meta.get("description"):
        qs = _yahoo_quote_summary(symbol)
        if qs:
            if not meta.get("sector"):
                meta["sector"] = qs.get("sector") or ""
            if not meta.get("exchange"):
                meta["exchange"] = qs.get("exchange") or ""
            meta["description"] = qs.get("description") or ""

    _meta_set(symbol, meta, "yahoo")
    return meta


def _lookup_yahoo_futures(symbol):
    """Live front-month price via Yahoo v7 quote (crumb-protected).
    Only answers Yahoo-style '=F' symbols so alias iterations (e.g. UKOIL) are
    skipped; this is the source that matches CNBC for Brent/WTI front month
    (continuous contracts like BCO/USD trade ~$1-3 higher)."""
    if not str(symbol).upper().endswith("=F"):
        return None
    _, _, multiplier = _display_metadata(symbol.upper())
    if multiplier != 1:
        return None  # grains: keep the LSE per-tonne conversion path unchanged
    session, crumb = _get_yahoo_crumb()
    if session is None or not crumb:
        return None
    try:
        resp = session.get(
            "https://query2.finance.yahoo.com/v7/finance/quote",
            params={"symbols": symbol.upper(), "crumb": crumb},
            timeout=4,
        )
        if resp.status_code != 200:
            return None
        quotes = (resp.json().get("quoteResponse") or {}).get("result") or []
        if not quotes:
            return None
        q = quotes[0]
        price = q.get("regularMarketPrice")
        if price is None:
            return None
        prev = q.get("regularMarketPreviousClose") or price
        return {
            "name": symbol.upper(),
            "price": float(price),
            "price_7d": float(prev),
            "price_30d": float(prev),
            "symbol": symbol.upper(),
            "sector": "",
            "exchange": q.get("fullExchangeName") or q.get("exchange") or "",
            "description": "",
        }
    except Exception as e:
        logger.warning("Yahoo v7 quote failed for %s: %s", symbol, e)
        return None


def _fmp_profile(symbol):
    """Fetch static metadata (sector, exchange, description) from FMP profile endpoint.
    Tries the /stable/ endpoint first, then the legacy /api/v3/ endpoint (some free
    keys only work on legacy). Only called when FMP_API_KEY is present."""
    symbol = symbol.upper()
    cached = _meta_get(symbol, "fmp")
    if cached is not None:
        return cached
    fmp_key = os.environ.get("FMP_API_KEY", "")
    if not fmp_key:
        return {}
    exchange_map = {
        "NASDAQ": "NASDAQ", "NYSE": "NYSE", "AMEX": "NYSE American",
        "NYSE ARCA": "NYSE Arca", "TSX": "TSX", "LSE": "London SE",
        "XETRA": "Frankfurt SE", "EURONEXT": "Euronext",
    }
    for base in ("stable", "api/v3"):
        try:
            url = f"https://financialmodelingprep.com/{base}/profile/{symbol}"
            resp = requests.get(url, params={"apikey": fmp_key}, timeout=8)
            if resp.status_code != 200:
                logger.warning("FMP profile HTTP %s (/%s) for %s", resp.status_code, base, symbol)
                continue
            data = resp.json()
            if isinstance(data, dict) and data.get("Error Message"):
                logger.warning("FMP profile error for %s: %s", symbol, data.get("Error Message"))
                continue
            profile = data[0] if isinstance(data, list) and data else (data or {})
            raw_ex = profile.get("exchangeShortName") or profile.get("exchange") or ""
            exchange = exchange_map.get(raw_ex.upper(), raw_ex)
            meta = {
                "sector": profile.get("sector") or "",
                "exchange": exchange,
                "description": profile.get("description") or "",
            }
            _meta_set(symbol, meta, "fmp")
            return meta
        except Exception as e:
            logger.warning("FMP profile fetch failed (/%s) for %s: %s", base, symbol, e)
            continue
    _meta_set(symbol, {}, "fmp")
    return {}


def _yf_info_metadata(symbol):
    """Last-resort metadata (sector, exchange, description) from yfinance .info.
    Cached per symbol (6h) so this slow-ish source is used sparingly."""
    exchange_map = {
        "NMS": "NASDAQ", "NYQ": "NYSE", "ASE": "NYSE American",
        "NGM": "NASDAQ", "PCX": "NYSE Arca", "TOR": "TSX",
        "VAN": "TSX Venture", "CME": "CME", "CMX": "COMEX",
        "NYM": "NYMEX", "CBT": "CBOT", "ICE": "ICE", "PNK": "OTC",
        "LSE": "London SE", "FRA": "Frankfurt SE",
        "NCM": "NASDAQ", "BATS": "Cboe BZX", "ENX": "Euronext",
    }
    symbol = symbol.upper()
    cached = _meta_get(symbol, "yf")
    if cached is not None:
        return cached
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception as e:
        logger.warning("yfinance info failed for %s: %s", symbol, e)
        info = {}
    if not isinstance(info, dict) or not info:
        _meta_set(symbol, {}, "yf")
        return {}
    raw_ex = info.get("exchange") or info.get("fullExchangeName") or ""
    exchange = exchange_map.get(raw_ex, raw_ex) or ""
    meta = {
        "sector": info.get("sector") or "",
        "exchange": exchange,
        "description": info.get("longBusinessSummary") or "",
    }
    _meta_set(symbol, meta, "yf")
    return meta


def _enrich_stock_metadata(result, symbol):
    """Enrich a stock lookup result with sector, exchange, and description.
    Tries FMP (if key present) then Yahoo Finance quoteSummary. Mutates result in place."""
    needs_sector = not result.get("sector")
    needs_exchange = not result.get("exchange") or result["exchange"] in ("Twelve Data", "London Strategic Edge", "Unknown")
    needs_desc = not result.get("description") or result["description"] == "No description available."
    if not (needs_sector or needs_exchange or needs_desc):
        return
    
    meta = {}
    # Try FMP first (accurate exchange names)
    if os.environ.get("FMP_API_KEY"):
        meta = _fmp_profile(symbol)
    
    # Fill remaining gaps from Yahoo
    if needs_sector and not meta.get("sector") or needs_desc and not meta.get("description") or needs_exchange and not meta.get("exchange"):
        yahoo_meta = _yahoo_metadata(symbol)
        if yahoo_meta:
            meta = {k: v or meta.get(k) for k, v in yahoo_meta.items()}
    
    if needs_sector and meta.get("sector"):
        result["sector"] = meta["sector"]
    if needs_exchange and meta.get("exchange"):
        result["exchange"] = meta["exchange"]
    if needs_desc and meta.get("description"):
        result["description"] = meta["description"]

    # Last resort: yfinance .info (slow, cached 6h) — ensures sector/exchange
    # rarely stay blank for listed stocks regardless of FMP/Yahoo availability.
    if (needs_sector and not result.get("sector")) or (needs_exchange and not result.get("exchange")):
        yf_meta = _yf_info_metadata(symbol)
        if yf_meta:
            if not result.get("sector") and yf_meta.get("sector"):
                result["sector"] = yf_meta["sector"]
            if not result.get("exchange") and yf_meta.get("exchange"):
                result["exchange"] = yf_meta["exchange"]
            if needs_desc and not result.get("description") and yf_meta.get("description"):
                result["description"] = yf_meta["description"]
    
    # Log if metadata still missing after enrichment attempts
    if needs_sector and not result.get("sector"):
        logger.warning("Metadata enrichment: sector still empty for %s", symbol)
    if needs_exchange and not result.get("exchange"):
        logger.warning("Metadata enrichment: exchange still empty for %s", symbol)


def _provider_symbols(symbol):
    """Return provider-compatible aliases for common Yahoo futures symbols.
    UKOIL precedes BZ=F for Brent because it maps to the ICE front-month
    contract (matching CNBC/Bloomberg quotes), whereas some feeds expose the
    generic/continuous next-nearby contract which trades ~$1-2 higher. Each
    provider tries these in order, so the front-month alias must come first."""
    aliases = {
        "BZ=F": ["UKOIL", "BZ=F", "BCO/USD"],
        "CL=F": ["WTI", "CL=F", "WTICO/USD"],
        "GC=F": ["XAU/USD"],
        "SI=F": ["XAG/USD"],
        "HG=F": ["XCU/USD"],
        "ZS=F": ["SOYBN/USD"],
        "ZC=F": ["CORN/USD"],
        "ZW=F": ["WHEAT/USD"],
        "NG=F": ["NATGAS/USD"],
    }
    return [symbol.upper(), *aliases.get(symbol.upper(), [])] if symbol.upper() not in ("BZ=F", "CL=F") else [*aliases[symbol.upper()]]


def _fallback_exchange(symbol):
    exchanges = {
        "BZ=F": "ICE",
        "CL=F": "NYMEX",
        "GC=F": "COMEX",
        "SI=F": "COMEX",
        "HG=F": "COMEX",
        "ZS=F": "CBOT",
        "ZC=F": "CBOT",
        "ZW=F": "CBOT",
        "NG=F": "NYMEX",
    }
    return exchanges.get(symbol.upper(), "Unknown")


def _display_metadata(symbol):
    metadata = {
        "BZ=F": ("Brent Crude Oil", "ICE", 1),
        "CL=F": ("WTI Crude Oil", "NYMEX", 1),
        "GC=F": ("Gold Futures", "COMEX", 1),
        "SI=F": ("Silver Futures", "COMEX", 1),
        "HG=F": ("Copper Futures", "COMEX", 1),
        "ZS=F": ("Soybean Futures", "CBOT", 100),
        "ZC=F": ("Corn Futures", "CBOT", 100),
        "ZW=F": ("Wheat Futures", "CBOT", 100),
        "NG=F": ("Natural Gas Futures", "NYMEX", 1),
    }
    return metadata.get(symbol.upper(), (None, None, 1))


def lookup(symbol):
    """Look up a quote using configured providers in reliability order.
    
    Provider ordering:
    - Futures/commodities: Yahoo v7 quote first (front-month contracts that match
      CNBC/Bloomberg), then London Strategic Edge, Twelve Data, FMP, yfinance.
      Grains (display multiplier != 1) keep the LSE per-tonne conversion path.
    - Stocks: Twelve Data first (better equities coverage), then LSE, FMP, yfinance.

    After a successful stock lookup, sector, exchange, and description are enriched
    from Yahoo Finance v1 search / quoteSummary (or FMP if a key is configured).
    """
    is_fut = _is_futures(symbol)
    if is_fut:
        providers = (
            ("Yahoo Futures", _lookup_yahoo_futures),
            ("London Strategic Edge", _lookup_lse),
            ("Twelve Data", _lookup_twelvedata),
            ("FMP", _lookup_fmp),
            ("yfinance", _lookup_yfinance),
        )
    else:
        providers = (
            ("Twelve Data", _lookup_twelvedata),
            ("London Strategic Edge", _lookup_lse),
            ("FMP", _lookup_fmp),
            ("yfinance", _lookup_yfinance),
        )

    for name, provider in providers:
        for provider_symbol in _provider_symbols(symbol):
            try:
                result = provider(provider_symbol)
                if result:
                    if name == "London Strategic Edge" or is_fut:
                        # Apply display metadata overrides (name, exchange, unit multiplier)
                        try:
                            td_meta = _twelvedata_quote(provider_symbol) or {} if name == "London Strategic Edge" else {}
                        except Exception:
                            td_meta = {}
                        display_name, display_exchange, multiplier = _display_metadata(symbol)
                        if display_name:
                            result["name"] = display_name
                        elif name == "London Strategic Edge":
                            result["name"] = td_meta.get("name") or result.get("name") or symbol.upper()
                        if display_exchange:
                            result["exchange"] = display_exchange
                        elif name == "London Strategic Edge":
                            result["exchange"] = td_meta.get("exchange") or _fallback_exchange(symbol)
                        if multiplier != 1:
                            for field in ("price", "price_7d", "price_30d"):
                                if field in result:
                                    result[field] = float(result[field]) * multiplier
                    result["symbol"] = symbol.upper()
                    # Enrich stocks with sector/exchange/description metadata
                    if not is_fut:
                        _enrich_stock_metadata(result, symbol.upper())
                    return result
            except Exception as e:
                logger.warning("%s lookup failed for %s: %s", name, provider_symbol, e)

    return None

def search_symbol(query):
    """Search for a symbol by name."""
    try:
        import requests
        import urllib.parse
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={urllib.parse.quote(query)}"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        results = []
        for q in data.get("quotes", [])[:10]:
            if "symbol" in q and ("shortname" in q or "longname" in q):
                name = q.get("shortname") or q.get("longname")
                results.append({
                    "symbol": q["symbol"],
                    "name": name,
                    "exchange": q.get("exchange", "")
                })
        return results
    except Exception as e:
        print(f"Error searching for {query}: {e}")
        return []


def _history_from_rows(rows):
    """Calculate technical indicators from normalized daily rows."""
    if len(rows) < 27:
        return None
    closes = [row["close"] for row in rows]

    def rolling_mean(values, index, window):
        if index < window - 1:
            return None
        return sum(values[index - window + 1:index + 1]) / window

    ma10 = [rolling_mean(closes, i, 10) for i in range(len(closes))]
    ma20 = [rolling_mean(closes, i, 20) for i in range(len(closes))]
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    rsi = [None] * len(closes)
    for i in range(14, len(closes)):
        average_gain = sum(gains[i - 14:i]) / 14
        average_loss = sum(losses[i - 14:i]) / 14
        rsi[i] = 100 if average_loss == 0 else 100 - (100 / (1 + average_gain / average_loss))
    roc7 = [None if i < 7 else ((closes[i] - closes[i - 7]) / closes[i - 7]) * 100 for i in range(len(closes))]
    roc7_ma7 = [rolling_mean([value or 0 for value in roc7], i, 7) if i >= 13 else None for i in range(len(closes))]
    start = next((i for i in range(len(closes)) if None not in (ma10[i], ma20[i], rsi[i], roc7[i], roc7_ma7[i])), None)
    if start is None:
        return None
    return {
        "dates": [row["date"] for row in rows[start:]],
        "open": [row["open"] for row in rows[start:]],
        "high": [row["high"] for row in rows[start:]],
        "low": [row["low"] for row in rows[start:]],
        "close": closes[start:],
        "ma10": ma10[start:],
        "ma20": ma20[start:],
        "roc7": roc7[start:],
        "roc7_ma7": roc7_ma7[start:],
        "rsi": rsi[start:],
    }


def get_history(symbol):
    """Fetch history using Twelve Data, London Strategic Edge, then yfinance."""
    providers = []
    if os.environ.get("TWELVE_DATA_API_KEY") or os.environ.get("TWELVEDATA_API_KEY"):
        providers.append(("Twelve Data", lambda: _twelvedata_series(symbol.upper(), outputsize=90)))
    if os.environ.get("LSE_API_KEY"):
        providers.append(("London Strategic Edge", lambda: _lse_candles(symbol.upper(), limit=90)))

    for name, fetch in providers:
        try:
            rows = []
            for row in fetch():
                rows.append({
                    "date": str(row.get("t", row.get("ts", row.get("datetime", ""))))[:10],
                    "open": float(row.get("o", row.get("open"))),
                    "high": float(row.get("h", row.get("high"))),
                    "low": float(row.get("l", row.get("low"))),
                    "close": float(row.get("c", row.get("close"))),
                })
            history = _history_from_rows(rows)
            if history:
                return history
        except Exception as e:
            logger.warning("%s history failed for %s: %s", name, symbol, e)

    try:
        ticker = yf.Ticker(symbol.upper())
        data = ticker.history(period="3mo")
        if data.empty:
            return None
        rows = [{
            "date": index.strftime("%Y-%m-%d"),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
        } for index, row in data.dropna(subset=["Open", "High", "Low", "Close"]).iterrows()]
        return _history_from_rows(rows)
    except Exception as e:
        logger.warning("yfinance history failed for %s: %s", symbol, e)
        return None


import os
import requests
import xml.etree.ElementTree as ET

def get_technical_summary(history):
    """Fallback: High-fidelity context-aware technical engine with expanded commentary."""
    try:
        # Latest data points
        price = history["close"][-1]
        prev_price = history["close"][-2]
        ma10 = history["ma10"][-1]
        prev_ma10 = history["ma10"][-2]
        ma20 = history["ma20"][-1]
        roc = history["roc7"][-1]
        rsi = history["rsi"][-1]
        
        # Historical slope (last 5 sessions) for trend direction
        ma10_prev_5 = history["ma10"][-5]
        ma10_slope = ((ma10 - ma10_prev_5) / ma10_prev_5) * 100
        
        # Deviations from MAs
        dev_10 = ((price - ma10) / ma10) * 100
        dev_20 = ((price - ma20) / ma20) * 100
        
        # Moving average alignment (Golden/Death cross indicators)
        ma_alignment = "Bullish Alignment" if ma10 > ma20 else "Bearish Alignment"
        ma_distance = ((ma10 - ma20) / ma20) * 100
        
        # Determine Trend Status with more nuance
        if ma10 > ma20:
            if price > ma10:
                trend_status = "Bullish Rallying" if ma10_slope > 0.5 else "Stable Bullish"
            else:
                trend_status = "Bullish Correction"
        else:
            if price < ma10:
                trend_status = "Bearish Pressure" if ma10_slope < -0.5 else "Minor Downtrend"
            else:
                trend_status = "Potential Recovery"
        
        # Relative Oversold/Overbought signals
        momentum_note = ""
        rsi_interpretation = ""
        if rsi > 70:
            momentum_note = "reaching overbought levels"
            rsi_interpretation = "suggesting potential pullback risk"
        elif rsi < 30:
            momentum_note = "entering oversold territory"
            rsi_interpretation = "suggesting potential rebound opportunity"
        else:
            momentum_note = "finding neutral momentum"
            rsi_interpretation = "indicating a balanced market sentiment"
            
        # Check for Crossovers
        crossover_note = ""
        if price > ma10 and prev_price <= prev_ma10:
            crossover_note = "The price just signaled a bullish break above the 10-day MA, suggesting renewed buying interest. "
        elif price < ma10 and prev_price >= prev_ma10:
            crossover_note = "The price just experienced a bearish breakdown below the 10-day MA, warning of potential selling pressure. "
            
        # Build Expanded Commentary (targeting ~200 words)
        sentence1 = (
            f"The stock is in a {trend_status.lower()} phase, currently trading {abs(dev_10):.1f}% "
            f"{'above' if dev_10 > 0 else 'below'} its 10-day moving average and {abs(dev_20):.1f}% "
            f"{'above' if dev_20 > 0 else 'below'} its 20-day moving average, indicating its position within the intermediate-term price structure."
        )
        
        sentence2 = (
            f"The moving average configuration shows {ma_alignment.lower()} with the 10-day MA positioned "
            f"{abs(ma_distance):.1f}% {'above' if ma_distance > 0 else 'below'} the 20-day MA, which reinforces the overall directional bias and trend strength. "
            f"{crossover_note}This structure is important as it establishes the foundation for short and intermediate-term trading strategies."
        )
        
        sentence3 = (
            f"Market momentum is {momentum_note} with a 7-day Rate of Change (ROC) of {roc:+.1f}%. "
            f"This metric suggests the stock is {'gaining' if roc > 0 else 'losing'} momentum in the near term and represents a {'positive' if roc > 0 else 'negative'} divergence indicator. "
            f"Current Relative Strength Index (RSI) stands at {rsi:.1f}, {rsi_interpretation} without extreme oversold or overbought conditions."
        )
        
        sentence4 = (
            f"Technical signals suggest {'maintaining a cautious watch' if abs(ma10_slope) < 0.5 else 'active monitoring of key levels'} as the {ma10_slope:+.1f}% slope in the 10-day average "
            f"indicates that the underlying trend is currently {'accelerating upward with conviction' if ma10_slope > 1 else 'accelerating downward with acceleration' if ma10_slope < -1 else 'finding steady footing without directional conviction'}. "
            f"Traders should monitor support at the 20-day MA and identify resistance at recent highs to execute informed trading decisions. Volume confirmation on any directional move would strengthen the technical setup."
        )
        
        return f"{sentence1} {sentence2} {sentence3} {sentence4}"
        
    except Exception as e:
        print(f"Engine Error: {e}")
        return "Market data integration is stabilizing. Current indicators show signs of consolidation across multiple timeframes and technical formations."


def get_llm_analysis(history, quote=None):
    """Multi-provider technical analysis: Groq (Primary) -> Engine (Fallback).
    Pass optional quote dict {symbol, name, sector} for stock-specific context."""
    # 1. Try Groq (Usually much better free tier speed/availability)
    groq_api_key = os.environ.get("GROQ_API_KEY")
    symbol = (quote or {}).get("symbol", "")
    name = (quote or {}).get("name", "")
    sector = (quote or {}).get("sector", "")

    closes = history["close"]
    prices = [round(x, 2) for x in closes[-25:]]
    ma10 = [round(x, 2) for x in history["ma10"][-25:]]
    ma20 = [round(x, 2) for x in history["ma20"][-25:]]
    roc7 = [round(x, 2) for x in history["roc7"][-25:]]
    rsi = history["rsi"]
    recent_rsi = rsi[-1]
    rsi_5ago = rsi[-6] if len(rsi) > 6 else rsi[0]

    price = closes[-1]
    prev = closes[-2]
    day_chg = ((price - prev) / prev) * 100 if prev else 0.0
    week_chg = ((closes[-1] - closes[-6]) / closes[-6]) * 100 if len(closes) > 6 else 0.0

    roc_signal = history.get("roc7_ma7")
    roc_ma = [round(x, 2) for x in roc_signal[-25:]] if roc_signal else None

    context_bits = [f"Security: {name} ({symbol})" if name else f"Security: {symbol}"]
    if sector:
        context_bits.append(f"Sector: {sector}")
    context = " - ".join(context_bits)
    prompt = (
        f"{context}\n"
        f"Daily closes (last 25): {prices}\n"
        f"10-day MA (last 25): {ma10}\n"
        f"20-day MA (last 25): {ma20}\n"
        f"7-day ROC %% (last 25): {roc7}\n"
        + (f"7-day ROC smoothed signal (last 25): {roc_ma}\n" if roc_ma else "")
        + f"RSI(14): {recent_rsi:.1f} today vs {rsi_5ago:.1f} five sessions ago\n"
        f"Last session change: {day_chg:+.2f}%%  |  5-day change: {week_chg:+.2f}%%\n\n"
        "Write a lively, natural technical commentary for today's chart in plain English. "
        "Do NOT use headings, bullets, or labels. Open with whichever point is most telling "
        "right now (a sharp move, an MA crossover, momentum or RSI extremes), then build a "
        "short narrative that connects the trend (10 vs 20-day MA and its current slope), "
        "momentum (7-day ROC and its smoothed signal), and RSI behavior — noting whether RSI "
        "is climbing or cooling versus five sessions ago. Express a clear but cautious "
        "directional view and mention ONE price level to watch. Match your tone to the tape: "
        "decisive when signals align, skeptical or balanced when they conflict.\n"
        "Under 160 words. No preamble."
    )
    temp = round(random.uniform(0.75, 0.95), 2)

    if groq_api_key and groq_api_key != "":
        try:
            url = "https://api.groq.com/openai/v1/chat/completions"
            headers = {"Authorization": f"Bearer {groq_api_key}", "Content-Type": "application/json"}
            payload = {
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 500, "temperature": temp
            }
            response = requests.post(url, headers=headers, json=payload, timeout=8)
            if response.status_code == 200:
                return response.json()['choices'][0]['message']['content'].strip()
            print(f"Groq API failed ({response.status_code}). Triggering technical fallback.")
        except Exception as e:
            print(f"Groq Error: {e}")

    # 2. Final Fallback: Technical Rule Engine
    return get_technical_summary(history)


import urllib.parse

def get_news(symbol, name="", sector=""):
    """Fetch the latest news stories for a symbol using a web crawler (via Google News RSS)."""
    try:
        # Search for stock-specific news
        query_parts = [symbol]
        if name and name.upper() != symbol.upper():
            query_parts.append(name)
        if sector:
            query_parts.append(sector)
            
        query = " ".join(query_parts) + " stock"
        encoded_query = urllib.parse.quote(query)
        
        url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
        response = requests.get(url, timeout=5)
        if response.status_code != 200:
            return []
            
        root = ET.fromstring(response.content)
        items = root.findall('.//item')
        
        results = []
        for item in items[:5]:
            title = item.find('title').text
            link = item.find('link').text
            source = item.find('source').text if item.find('source') is not None else "Financial News"
            
            # Clean up title (Google News often appends publisher name)
            if " - " in title:
                title = title.rsplit(" - ", 1)[0]
                
            results.append({
                "title": title,
                "link": link,
                "publisher": source
            })
        return results
    except Exception as e:
        print(f"Error crawling news for {symbol}: {e}")
        return []


def usd(value):
    """Format value as USD."""
    return f"${value:,.2f}"
