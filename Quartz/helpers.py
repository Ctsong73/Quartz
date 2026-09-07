import yfinance as yf
import math
import os
import logging

from flask import redirect, render_template, session
from functools import wraps

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
            "exchange": quote.get("exchange", "Unknown"),
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
        params={"symbol": symbol, "interval": "1day", "outputsize": outputsize, "order": "ASC", "apikey": api_key},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("status") == "error" or "values" not in data:
        return []
    return data["values"]


def _twelvedata_quote(symbol):
    """Fetch the latest quote from Twelve Data."""
    api_key = os.environ.get("TWELVE_DATA_API_KEY") or os.environ.get("TWELVEDATA_API_KEY", "")
    if not api_key:
        return None
    response = requests.get("https://api.twelvedata.com/quote", params={"symbol": symbol, "apikey": api_key}, timeout=10)
    response.raise_for_status()
    data = response.json()
    return None if data.get("status") == "error" or not data.get("close") else data


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
    return {"name": (quote or {}).get("name") or symbol.upper(), "price": price,
            "price_7d": closes[-6] if len(closes) >= 6 else previous_close,
            "price_30d": closes[0] if closes else previous_close, "symbol": symbol.upper(),
            "sector": "", "exchange": (quote or {}).get("exchange") or "Twelve Data",
            "description": "No description available."}


def _lse_candles(symbol, limit=35, timeframe="1d", order="asc"):
    """Fetch candles from London Strategic Edge."""
    api_key = os.environ.get("LSE_API_KEY", "")
    if not api_key:
        return []
    response = requests.get(
        f"{os.environ.get('LSE_API_URL', 'https://api.londonstrategicedge.com/vault').rstrip('/')}/candles",
        headers={"x-api-key": api_key},
        params={"symbol": symbol, "timeframe": timeframe, "limit": limit, "order": order},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def _lookup_lse(symbol):
    """Look up a live quote and recent performance using London Strategic Edge."""
    try:
        rows = _lse_candles(symbol, limit=1, timeframe="1m", order="desc")
    except Exception:
        rows = []
    try:
        daily_rows = _lse_candles(symbol, limit=35, timeframe="1d")
    except Exception:
        daily_rows = []
    if not rows:
        rows = daily_rows
    closes = [float(row["close"]) for row in rows if row.get("close") is not None]
    if not closes:
        return None
    daily_closes = [float(row["close"]) for row in daily_rows if row.get("close") is not None]
    return {"name": symbol.upper(), "price": closes[0],
            "price_7d": daily_closes[-6] if len(daily_closes) >= 6 else closes[0],
            "price_30d": daily_closes[0] if daily_closes else closes[0], "symbol": symbol.upper(),
            "sector": "", "exchange": "London Strategic Edge", "description": "No description available."}


def _provider_symbols(symbol):
    aliases = {"BZ=F": ["BRENT"], "CL=F": ["WTI", "WTICO/USD"], "GC=F": ["XAU/USD"], "SI=F": ["XAG/USD"]}
    return [symbol.upper(), *aliases.get(symbol.upper(), [])]


def lookup(symbol):
    """Look up a quote using live providers before legacy fallbacks."""
    providers = (("London Strategic Edge", _lookup_lse), ("Twelve Data", _lookup_twelvedata),
                 ("FMP", _lookup_fmp), ("yfinance", _lookup_yfinance))
    for name, provider in providers:
        for provider_symbol in _provider_symbols(symbol):
            try:
                result = provider(provider_symbol)
                if result:
                    result["symbol"] = symbol.upper()
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
    if len(rows) < 27:
        return None
    closes = [row["close"] for row in rows]
    def rolling_mean(values, index, window):
        return None if index < window - 1 else sum(values[index - window + 1:index + 1]) / window
    ma10 = [rolling_mean(closes, i, 10) for i in range(len(closes))]
    ma20 = [rolling_mean(closes, i, 20) for i in range(len(closes))]
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    rsi = [None] * len(closes)
    for i in range(14, len(closes)):
        avg_gain = sum(gains[i - 14:i]) / 14
        avg_loss = sum(losses[i - 14:i]) / 14
        rsi[i] = 100 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    roc7 = [None if i < 7 else ((closes[i] - closes[i - 7]) / closes[i - 7]) * 100 for i in range(len(closes))]
    roc7_ma7 = [rolling_mean([value or 0 for value in roc7], i, 7) if i >= 13 else None for i in range(len(closes))]
    start = next((i for i in range(len(closes)) if None not in (ma10[i], ma20[i], rsi[i], roc7[i], roc7_ma7[i])), None)
    if start is None:
        return None
    return {"dates": [row["date"] for row in rows[start:]], "open": [row["open"] for row in rows[start:]],
            "high": [row["high"] for row in rows[start:]], "low": [row["low"] for row in rows[start:]],
            "close": closes[start:], "ma10": ma10[start:], "ma20": ma20[start:], "roc7": roc7[start:],
            "roc7_ma7": roc7_ma7[start:], "rsi": rsi[start:]}


def get_history(symbol):
    """Fetch history using Twelve Data, London Strategic Edge, then yfinance."""
    providers = []
    if os.environ.get("TWELVE_DATA_API_KEY") or os.environ.get("TWELVEDATA_API_KEY"):
        providers.append(("Twelve Data", lambda: _twelvedata_series(symbol.upper(), outputsize=90)))
    if os.environ.get("LSE_API_KEY"):
        providers.append(("London Strategic Edge", lambda: _lse_candles(symbol.upper(), limit=90, timeframe="1d")))
    for name, fetch in providers:
        try:
            rows = [{"date": str(row.get("t", row.get("ts", row.get("datetime", ""))))[:10],
                     "open": float(row.get("o", row.get("open"))), "high": float(row.get("h", row.get("high"))),
                     "low": float(row.get("l", row.get("low"))), "close": float(row.get("c", row.get("close")))}
                    for row in fetch()]
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
        rows = [{"date": index.strftime("%Y-%m-%d"), "open": float(row["Open"]), "high": float(row["High"]),
                 "low": float(row["Low"]), "close": float(row["Close"])}
                for index, row in data.dropna(subset=["Open", "High", "Low", "Close"]).iterrows()]
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


def get_llm_analysis(history):
    """Multi-provider technical analysis: Groq (Primary) -> Engine (Fallback)"""
    # 1. Try Groq (Usually much better free tier speed/availability)
    groq_api_key = os.environ.get("GROQ_API_KEY")
    recent_rsi = history["rsi"][-1]
    prompt = (
        f"As a professional technical analyst, analyze this stock with the following data:\n"
        f"Prices: {[round(x, 2) for x in history['close'][-20:]]}\n"
        f"10-Day MA: {[round(x, 2) for x in history['ma10'][-20:]]}\n"
        f"20-Day MA: {[round(x, 2) for x in history['ma20'][-20:]]}\n"
        f"7-Day ROC (%): {[round(x, 2) for x in history['roc7'][-20:]]}\n"
        f"Current RSI(14): {recent_rsi:.1f}\n\n"
        f"Provide a structured analysis in THREE separate paragraphs:\n"
        f"1. **Trend Analysis**: Analyze the price relationship with the 10 and 20-day Moving Averages.\n"
        f"2. **Momentum (ROC)**: Comment on the 7-day Rate of Change and its direction.\n"
        f"3. **Relative Strength (RSI)**: Comment on the current RSI value and potential overbought/oversold conditions.\n\n"
        f"Be professional, objective, and do not use preambles."
    )

    if groq_api_key and groq_api_key != "":
        try:
            url = "https://api.groq.com/openai/v1/chat/completions"
            headers = {"Authorization": f"Bearer {groq_api_key}", "Content-Type": "application/json"}
            payload = {
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1000, "temperature": 0.5
            }
            response = requests.post(url, headers=headers, json=payload, timeout=5)
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
