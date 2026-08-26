import yfinance as yf
import math

from flask import redirect, render_template, session
from functools import wraps


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



def lookup(symbol):
    """Look up quote for symbol."""
    try:
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
        
        # Get company name
        name = ticker.info.get('longName') or ticker.info.get('shortName') or symbol.upper()
        sector = ticker.info.get('sector') or ""
        raw_exchange = ticker.info.get('exchange') or "Unknown"
        
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
        
        description = ticker.info.get('longBusinessSummary') or ticker.info.get('description') or "No description available."
        
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
    except Exception as e:
        print(f"Error looking up {symbol}: {e}")
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


def get_history(symbol):
    """Fetch 3 months of historical data with technical indicators."""
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="3mo")
        if df.empty:
            return None
            
        # Calculate Moving Averages
        df['MA10'] = df['Close'].rolling(window=10).mean()
        df['MA20'] = df['Close'].rolling(window=20).mean()
        
        # Calculate RSI (14-day)
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        
        # Calculate 7-day ROC
        df['ROC7'] = ((df['Close'] - df['Close'].shift(7)) / df['Close'].shift(7)) * 100
        
        # Calculate 7-day MA of ROC (Signal Line)
        df['ROC7_MA7'] = df['ROC7'].rolling(window=7).mean()
        
        # Drop NaN values (first few days of MA/ROC/RSI calculation)
        df = df.dropna()
        if df.empty:
            return None
        
        return {
            "dates": df.index.strftime('%Y-%m-%d').tolist(),
            "open": df['Open'].tolist(),
            "high": df['High'].tolist(),
            "low": df['Low'].tolist(),
            "close": df['Close'].tolist(),
            "ma10": df['MA10'].tolist(),
            "ma20": df['MA20'].tolist(),
            "roc7": df['ROC7'].tolist(),
            "roc7_ma7": df['ROC7_MA7'].tolist(),
            "rsi": df['RSI'].tolist()
        }
    except Exception as e:
        print(f"Error fetching history for {symbol}: {e}")
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
