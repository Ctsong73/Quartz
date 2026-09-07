import os
import logging
import math
import time
import traceback
import requests
import threading
from flask import jsonify

# Configure basic logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s in %(module)s: %(message)s')
logger = logging.getLogger(__name__)

import yfinance as yf
from datetime import date, timedelta
from flask import Flask, flash, redirect, render_template, request, session, jsonify
from werkzeug.security import check_password_hash, generate_password_hash
from dotenv import load_dotenv

load_dotenv()

from helpers import apology, get_history, get_llm_analysis, get_news, login_required, lookup, usd, search_symbol

# API Configuration
os.environ["GROQ_API_KEY"] = os.environ.get("GROQ_API_KEY", "")

_ticker_cache = {"timestamp": 0, "data": {}}

def _get_japan_10y_yield():
    """Fetch official Japan 10-Year Government Bond Yield (%) from Ministry of Finance Japan."""
    try:
        url = "https://www.mof.go.jp/english/jgbs/reference/interest_rate/jgbcme.csv"
        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        if resp.status_code == 200:
            lines = [line.strip() for line in resp.text.split('\n') if line.strip()]
            data_rows = []
            for line in lines:
                parts = line.split(',')
                if len(parts) >= 11 and parts[0].replace('/', '').replace('-', '').isdigit():
                    try:
                        date_str = parts[0]
                        yield_10y = float(parts[10])
                        data_rows.append((date_str, yield_10y))
                    except ValueError:
                        continue
            if len(data_rows) >= 1:
                latest_yield = data_rows[-1][1]
                prev_yield = data_rows[-2][1] if len(data_rows) >= 2 else latest_yield
                change = latest_yield - prev_yield
                pct = (change / prev_yield * 100) if prev_yield != 0 else 0.0
                return {
                    "price": latest_yield,
                    "change": change,
                    "pct": pct,
                    "symbol": "MOF JGB 10Y"
                }
    except Exception as e:
        logger.warning(f"MOF JGB 10Y yield fetch failed: {e}")
    return None

def _get_market_tickers_yfinance():
    """Fetch market ticker data via yfinance (works locally, often blocked on cloud)."""
    tickers = {
        "DJIA": "^DJI",
        "Gold": "GC=F",
        "Oil": "CL=F",
        "BTC": "BTC-USD",
        "TNX": "^TNX",
        "Nasdaq": "^IXIC",
        "Copper": "HG=F",
        "PalmOil": "CPO=F",
        "Nvidia": "NVDA",
        "Japan10Y": "2561.T"
    }
    results = {}

    # Primary: try official Ministry of Finance Japan JGB 10Y Yield (%)
    jgb_yield = _get_japan_10y_yield()
    if jgb_yield:
        results["Japan10Y"] = jgb_yield

    for name, sym in tickers.items():
        if name in results:
            continue
        try:
            t = yf.Ticker(sym)
            # Fetch 5d to ensure valid close prices even across weekends and holidays
            h = t.history(period="5d")
            if not h.empty:
                h = h.dropna(subset=['Close'])
            
            close = None
            prev = None

            if not h.empty:
                closes = [float(c) for c in h['Close'] if not math.isnan(c)]
                if len(closes) >= 1:
                    close = closes[-1]
                    prev = closes[-2] if len(closes) >= 2 else close

            # Fallback to fast_info if history is empty or all NaN
            if close is None:
                fi = t.fast_info
                p = getattr(fi, 'last_price', None) or getattr(fi, 'previous_close', None)
                if p is not None and not math.isnan(p):
                    close = float(p)
                    pc = getattr(fi, 'previous_close', None)
                    prev = float(pc) if (pc is not None and not math.isnan(pc)) else close

            if close is not None:
                change = close - (prev if prev is not None else close)
                pct = (change / prev * 100) if (prev and prev != 0) else 0.0
                results[name] = {
                    "price": close,
                    "change": change,
                    "pct": pct,
                    "symbol": sym
                }
        except Exception as e:
            logger.warning(f"yfinance ticker {sym}: {e}")
    return results


def _get_market_tickers_fmp():
    """Fallback: Fetch market tickers via Financial Modeling Prep (works on cloud servers)."""
    fmp_key = os.environ.get("FMP_API_KEY", "")
    if not fmp_key:
        return {}
    
    # FMP uses standard symbols — map display names to FMP symbols
    tickers = {
        "DJIA": "^DJI",
        "Gold": "GC=F",
        "Oil": "CL=F",
        "BTC": "BTCUSD",
        "TNX": "^TNX",
        "Nasdaq": "^IXIC",
        "Copper": "HG=F",
        "PalmOil": "CPO=F",
        "Nvidia": "NVDA",
        "Japan10Y": "2561.T"
    }
    results = {}
    
    # FMP allows batch quotes — try to fetch all at once
    try:
        # Batch quote for standard symbols that FMP supports well
        fmp_batch = ["NVDA"]  # Start with simple stocks
        batch_sym = ",".join(fmp_batch)
        url = f"https://financialmodelingprep.com/stable/quote/{batch_sym}"
        resp = requests.get(url, params={"apikey": fmp_key}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                for q in data:
                    sym = q.get("symbol", "")
                    price = q.get("price")
                    prev = q.get("previousClose", price)
                    if price:
                        # Map FMP symbol back to display name
                        for dname, tsym in tickers.items():
                            if sym.upper() == tsym.upper() or (dname == "Nvidia" and sym == "NVDA"):
                                change = price - prev if prev else 0
                                pct = (change / prev * 100) if prev and prev != 0 else 0
                                results[dname] = {
                                    "price": float(price),
                                    "change": float(change),
                                    "pct": float(pct),
                                    "symbol": tsym
                                }
    except Exception as e:
        logger.warning(f"FMP batch ticker fetch failed: {e}")
    
    # Try individual fetches for remaining key symbols
    fmp_individual = {
        "DJIA": "^DJI",
        "Gold": "GCUSD",
        "Oil": "CLUSD",
        "BTC": "BTCUSD",
        "Nasdaq": "^IXIC",
    }
    for name, sym in fmp_individual.items():
        if name in results:
            continue
        try:
            url = f"https://financialmodelingprep.com/stable/quote/{sym}"
            resp = requests.get(url, params={"apikey": fmp_key}, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    q = data[0]
                    price = q.get("price")
                    prev = q.get("previousClose", price)
                    if price:
                        change = price - prev if prev else 0
                        pct = (change / prev * 100) if prev and prev != 0 else 0
                        results[name] = {
                            "price": float(price),
                            "change": float(change),
                            "pct": float(pct),
                            "symbol": tickers.get(name, sym)
                        }
        except Exception as e:
            logger.warning(f"FMP individual ticker {sym}: {e}")
    
    return results


def get_market_tickers():
    """Get market ticker data. Tries yfinance first, falls back to FMP."""
    now = time.time()
    if now - _ticker_cache["timestamp"] < 30 and _ticker_cache["data"]:
        return _ticker_cache["data"]
    
    # Try yfinance first
    results = _get_market_tickers_yfinance()
    
    # If yfinance returned very few results, try FMP as supplement/replacement
    if len(results) < 3:
        logger.info(f"yfinance returned only {len(results)} tickers, trying FMP fallback.")
        fmp_results = _get_market_tickers_fmp()
        # Merge: FMP fills gaps, yfinance takes priority where both exist
        for k, v in fmp_results.items():
            if k not in results:
                results[k] = v
    
    if results:
        _ticker_cache["timestamp"] = now
        _ticker_cache["data"] = results
        
    return results or _ticker_cache["data"]

from database import db

def _start_supabase_keepalive():
    """Background daemon thread to ping Supabase PostgreSQL every 12 hours."""
    def keepalive_loop():
        logger.info("Supabase keep-alive background thread initialized.")
        while True:
            # Wait 12 hours (43200 seconds) between database pings
            time.sleep(43200)
            try:
                result = db.execute("SELECT 1 AS keepalive")
                logger.info(f"Supabase keep-alive ping executed successfully: {result}")
            except Exception as e:
                logger.warning(f"Supabase keep-alive ping encountered issue: {e}")

    thread = threading.Thread(target=keepalive_loop, daemon=True, name="supabase-keepalive")
    thread.start()

_start_supabase_keepalive()

# Configure application
app = Flask(__name__)

# SECRET_KEY: Use env var in production; generate a stable fallback if missing
import secrets
secret_key = os.environ.get("SECRET_KEY")
if not secret_key:
    if os.environ.get("RENDER") or os.environ.get("FLASK_ENV") == "production":
        # On Render without SECRET_KEY: generate one and warn (sessions won't survive redeploy)
        secret_key = secrets.token_hex(32)
        logger.warning("SECRET_KEY not set! Generated a random one — sessions will not persist across deploys. Set SECRET_KEY in Render environment variables.")
    else:
        secret_key = "local-development-only"
app.config["SECRET_KEY"] = secret_key

# Configure session — use Flask's default signed-cookie sessions (no filesystem needed)
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Only enforce Secure cookies on actual HTTPS (Render provides HTTPS automatically)
is_production = bool(os.environ.get("RENDER") or os.environ.get("FLASK_ENV") == "production")
app.config["SESSION_COOKIE_SECURE"] = is_production

# Custom filter
app.jinja_env.filters["usd"] = usd


def get_baseline_nlv(history_rows, days, max_days=None, fallback_value=None, today=None):
    """Return the historical NLV within [days, max_days] ago, or fallback_value."""
    if today is None:
        today = date.today()

    cutoff_start = (today - timedelta(days=days)).isoformat()
    cutoff_end = (today - timedelta(days=max_days)).isoformat() if max_days else None

    for row in reversed(history_rows):
        row_date = row.get("date")
        if isinstance(row_date, date):
            row_date = row_date.isoformat()
        if row_date and row_date <= cutoff_start:
            if cutoff_end and row_date < cutoff_end:
                continue
            return float(row["nlv"])

    if fallback_value is not None:
        return fallback_value

    if history_rows:
        return history_rows[0]["nlv"]

    return 1000000.00



@app.after_request
def after_request(response):
    """Ensure responses aren't cached"""
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Expires"] = 0
    response.headers["Pragma"] = "no-cache"
    return response

@app.route("/")
def home():
    """Redirect to login page"""
    return redirect("/login")

@app.route("/portfolio")
@login_required
def index():
    """Show portfolio of stocks"""
    user_id = session["user_id"]

    cash = float(db.execute("SELECT cash FROM users WHERE id = ?", user_id)[0]["cash"])

    holdings = db.execute("SELECT symbol, shares FROM portfolio WHERE user_id = ?", user_id)
    portfolio = []
    total_value = cash

    for holding in holdings:
        symbol = holding["symbol"]
        shares = holding["shares"]
        quote = lookup(symbol)
        
        # Calculate cost basis from transactions
        if shares > 0:
            cost_data = db.execute(
                "SELECT SUM(shares * price) AS total_cost, SUM(shares) AS total_shares FROM transactions WHERE user_id = ? AND symbol = ? AND type = 'buy'",
                user_id, symbol
            )[0]
        elif shares < 0:
            cost_data = db.execute(
                "SELECT SUM(shares * price) AS total_cost, SUM(shares) AS total_shares FROM transactions WHERE user_id = ? AND symbol = ? AND type = 'sell'",
                user_id, symbol
            )[0]
        else:
            cost_data = {"total_cost": 0, "total_shares": 0}
            
        avg_cost = (float(cost_data["total_cost"]) / float(cost_data["total_shares"])
                if cost_data["total_shares"] and cost_data["total_shares"] > 0 else 0)
        
        if quote:
            price = quote["price"]
            price_7d = quote.get("price_7d", price)
            price_30d = quote.get("price_30d", price)
            
            value = price * shares
            total_value += value
            
            profit_loss = (price - avg_cost) * shares
            
            if avg_cost > 0:
                if shares > 0:
                    profit_loss_pct = ((price - avg_cost) / avg_cost) * 100
                else:
                    profit_loss_pct = ((avg_cost - price) / avg_cost) * 100
            else:
                profit_loss_pct = 0
            
            portfolio.append({
                "symbol": symbol,
                "name": quote.get("name", symbol),
                "shares": shares,
                "price": price,
                "price_7d": price_7d,
                "price_30d": price_30d,
                "avg_cost": avg_cost,
                "value": value,
                "pl": profit_loss,
                "pl_pct": profit_loss_pct,
                "exchange": quote.get("exchange", "Unknown")
            })
    
    # Calculate cash metrics
    short_liability = sum(abs(item["value"]) for item in portfolio if item["shares"] < 0)
    free_cash = cash - short_liability
    margin_cushion = free_cash - short_liability
    
    # Log today's Net Liquidation Value while preserving a daily history trail.
    db.execute(
        "INSERT INTO account_history (user_id, date, nlv) VALUES (?, DATE('now', 'localtime'), ?) "
        "ON CONFLICT(user_id, date) DO UPDATE SET nlv = excluded.nlv",
        user_id, total_value
    )

    # Calculate holdings-based fallback NLVs using historical stock prices (7d and 30d ago)
    nlv_7d_est = cash + sum(item["shares"] * item["price_7d"] for item in portfolio)
    nlv_30d_est = cash + sum(item["shares"] * item["price_30d"] for item in portfolio)

    # Get historical NLV values for the 7-day and 30-day performance windows.
    history_rows = db.execute(
        "SELECT date, nlv FROM account_history WHERE user_id = ? ORDER BY date ASC",
        user_id,
    )

    nlv_7d = get_baseline_nlv(history_rows, days=7, max_days=14, fallback_value=nlv_7d_est)
    nlv_30d = get_baseline_nlv(history_rows, days=30, max_days=60, fallback_value=nlv_30d_est)

    if nlv_7d == nlv_30d and portfolio:
        nlv_7d = nlv_7d_est
        nlv_30d = nlv_30d_est

    weekly_pl = total_value - nlv_7d
    monthly_pl = total_value - nlv_30d
    
    weekly_pl_pct = (weekly_pl / nlv_7d * 100) if nlv_7d > 0 else 0
    monthly_pl_pct = (monthly_pl / nlv_30d * 100) if nlv_30d > 0 else 0
            
    return render_template("portfolio.html", portfolio=portfolio, cash=cash, free_cash=free_cash, short_liability=short_liability, margin_cushion=margin_cushion, total=total_value, weekly_pl=weekly_pl, weekly_pl_pct=weekly_pl_pct, monthly_pl=monthly_pl, monthly_pl_pct=monthly_pl_pct)

@app.route("/portfolio/reset", methods=["POST"])
@login_required
def reset_portfolio():
    """Reset the current user's portfolio to the initial starting state."""
    user_id = session["user_id"]

    db.execute("DELETE FROM portfolio WHERE user_id = ?", user_id)
    db.execute("DELETE FROM transactions WHERE user_id = ?", user_id)
    db.execute("DELETE FROM account_history WHERE user_id = ?", user_id)
    db.execute("UPDATE users SET cash = ? WHERE id = ?", 1000000.00, user_id)

    flash("Portfolio reset to the initial $1,000,000 starting balance.")
    return redirect("/portfolio")

@app.route("/buy", methods=["GET", "POST"])
@login_required
def buy():
    """Buy shares of stock"""
    if request.method == "POST":
        try:
            symbol = request.form.get("symbol")
            shares = request.form.get("shares")
            if not symbol or not shares:
                return apology("Please provide symbol and shares", 400)
            symbol = symbol.upper()

            try:
                shares = int(shares)
                if shares <= 0:
                    return apology("Shares must be more than 0", 400)
            except ValueError:
                return apology("shares must be a number", 400)

            quote = lookup(symbol.upper())
            if not quote:
                return apology("invalid symbol", 400)

            price = float(quote["price"])
            user_id = session["user_id"]
            user_cash = float(db.execute("SELECT cash FROM users WHERE id = ?", user_id)[0]["cash"])

            holdings = db.execute("SELECT symbol, shares FROM portfolio WHERE user_id = ?", user_id)
            current_shares = 0
            total_short_liability = 0.0
            nlv = user_cash
            
            for h in holdings:
                sym = h["symbol"]
                s = int(h["shares"])
                if sym == symbol:
                    current_shares = s
                q = lookup(sym)
                if q:
                    p = float(q["price"])
                    nlv += p * s
                    if s < 0:
                        total_short_liability += abs(p * s)

            c_current = user_cash
            s_current = total_short_liability
            cost = price * shares
            c_after = c_current - cost
            new_shares = current_shares + shares
            
            s_after = s_current
            if current_shares < 0:
                s_after -= abs(current_shares) * price
            if new_shares < 0:
                s_after += abs(new_shares) * price
                
            if c_after < s_after and (c_after - s_after) < (c_current - s_current):
                return apology("insufficient cash for margin requirement", 400)
            if s_after > nlv and s_after > s_current:
                return apology("short value exceeds net liquidation value", 400)

            db.execute("UPDATE users SET cash = ? WHERE id = ?", c_after, user_id)
            db.execute("INSERT INTO transactions (user_id, symbol, shares, price, type) VALUES (?, ?, ?, ?, 'buy')",
                       user_id, symbol, shares, price)

            if current_shares != 0 or len([h for h in holdings if h["symbol"] == symbol]) > 0:
                db.execute("UPDATE portfolio SET shares = ? WHERE user_id = ? AND symbol = ?", new_shares, user_id, symbol)
            else:
                db.execute("INSERT INTO portfolio (user_id, symbol, shares) VALUES (?, ?, ?)", user_id, symbol, shares)
                
            db.execute("DELETE FROM portfolio WHERE user_id = ? AND shares = 0", user_id)

            flash("Bought")
            return redirect("/portfolio")
        except Exception as e:
            logger.error(f"Buy error: {e}", exc_info=True)
            return apology("Something went wrong", 500)

    return render_template("buy.html")

@app.route("/history")
@login_required
def history():
    """Show history of transactions"""
    user_id = session["user_id"]
    transactions = db.execute(
        "SELECT symbol, shares, price, type, timestamp FROM transactions WHERE user_id = ? ORDER BY timestamp DESC", user_id)
    return render_template("history.html", transactions=transactions)

@app.route("/login", methods=["GET", "POST"])
def login():
    """Log user in"""
    session.clear()

    if request.method == "POST":
        if not request.form.get("username"):
            flash("Please provide username and password, or register to join our platform")
            return render_template("login.html")
        elif not request.form.get("password"):
            flash("Please provide username and password, or register to join our platform")
            return render_template("login.html")
        
        username = request.form.get("username")
        rows = db.execute("SELECT * FROM users WHERE username = ?", username)

        if len(rows) != 1:
            flash("Username not found, please register.")
            return render_template("login.html")
        
        if not check_password_hash(rows[0]["hash"], request.form.get("password")):
            flash("Invalid password.")
            return render_template("login.html")
        
        session["user_id"] = rows[0]["id"]
        return redirect("/portfolio")

    return render_template("login.html")

@app.route("/logout")
def logout():
    """Log user out"""
    session.clear()
    return redirect("/")

@app.route("/quote", methods=["GET", "POST"])
@login_required
def quote():
    """Get stock quote."""
    if request.method == "POST" or (request.method == "GET" and request.args.get("symbol")):
        symbol = request.form.get("symbol") if request.method == "POST" else request.args.get("symbol")
        if not symbol:
            return apology("Please provide symbol!", 400)

        quote = lookup(symbol.upper())
        if not quote:
            return apology("No such symbol!", 400)

        history = get_history(symbol.upper())
        news = get_news(quote["symbol"], quote["name"], quote.get("sector", ""))
        analysis = get_llm_analysis(history) if history else None
        
        return render_template("quoted.html", quote=quote, history=history, news=news, analysis=analysis)
    return render_template("quote.html")

@app.route("/search")
@login_required
def search():
    """Proxy for stock symbol search"""
    q = request.args.get("q", "")
    if not q:
        return jsonify([])

    results = search_symbol(q)
    return jsonify(results)

@app.route("/api/quote")
@login_required
def api_quote():
    """API endpoint to get live quote and price for a single symbol"""
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "Symbol is required"}), 400

    quote = lookup(symbol)
    if not quote:
        return jsonify({"error": "Symbol not found"}), 404

    price = float(quote["price"])
    return jsonify({
        "symbol": quote["symbol"],
        "name": quote.get("name", quote["symbol"]),
        "price": price,
        "formatted_price": usd(price)
    })

@app.route("/api/market_ticker")
def market_ticker():
    """API endpoint for live market tickers beside logo"""
    data = get_market_tickers()
    return jsonify(data)

@app.route("/health")
def health():
    """Health check endpoint for debugging deployment issues."""
    status = {
        "status": "ok",
        "database": "unknown",
        "twelvedata": "configured" if os.environ.get("TWELVE_DATA_API_KEY") or os.environ.get("TWELVEDATA_API_KEY") else "no_api_key",
        "london_strategic_edge": "configured" if os.environ.get("LSE_API_KEY") else "no_api_key",
        "yfinance": "unknown",
        "fmp": "unknown",
    }
    
    # Test database connection
    try:
        result = db.execute("SELECT 1 AS test")
        status["database"] = "connected" if result else "empty_result"
    except Exception as e:
        status["database"] = f"error: {str(e)}"
    
    # Test yfinance
    try:
        t = yf.Ticker("AAPL")
        h = t.history(period="1d")
        status["yfinance"] = "working" if not h.empty else "blocked"
    except Exception as e:
        status["yfinance"] = f"blocked: {str(e)[:100]}"
    
    # Test FMP
    fmp_key = os.environ.get("FMP_API_KEY", "")
    if fmp_key:
        try:
            resp = requests.get(
                "https://financialmodelingprep.com/stable/quote/AAPL",
                params={"apikey": fmp_key}, timeout=10
            )
            if resp.status_code == 200 and resp.json():
                status["fmp"] = "working"
            else:
                status["fmp"] = f"error: HTTP {resp.status_code}"
        except Exception as e:
            status["fmp"] = f"error: {str(e)[:100]}"
    else:
        status["fmp"] = "no_api_key"
    
    # Environment check
    status["env"] = {
        "RENDER": bool(os.environ.get("RENDER")),
        "DATABASE_URL": bool(os.environ.get("DATABASE_URL")),
        "SECRET_KEY": bool(os.environ.get("SECRET_KEY")),
        "FMP_API_KEY": bool(fmp_key),
        "TWELVE_DATA_API_KEY": bool(os.environ.get("TWELVE_DATA_API_KEY") or os.environ.get("TWELVEDATA_API_KEY")),
        "LSE_API_KEY": bool(os.environ.get("LSE_API_KEY")),
        "GROQ_API_KEY": bool(os.environ.get("GROQ_API_KEY")),
    }
    
    return jsonify(status)

@app.route("/ping")
def ping():
    """Lightweight ping endpoint to wake Render and keep Supabase PostgreSQL active."""
    try:
        db.execute("SELECT 1 AS keepalive")
        return jsonify({
            "status": "active",
            "database": "pinged",
            "timestamp": time.time()
        })
    except Exception as e:
        logger.error(f"Ping endpoint error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/register", methods=["GET", "POST"])
def register():
    """Register user"""
    if request.method == 'POST':
        username = request.form.get("username")
        password = request.form.get("password")
        recovery_answer = request.form.get("recovery_answer")

        if not username or not password or not request.form.get("confirmation") or not recovery_answer:
            flash("Please provide all fields, including your favorite color.")
            return render_template("register.html")

        if password != request.form.get("confirmation"):
            return apology("Password Mismatch!", 400)

        hash_pw = generate_password_hash(password)
        recovery_hash = generate_password_hash(recovery_answer.lower().strip())
        
        try:
            db.execute("INSERT INTO users (username, hash, recovery_answer, cash) VALUES (?, ?, ?, 1000000.00)", 
                      username, hash_pw, recovery_hash)
            flash("Registration Successful!")
            return redirect("/login")
        except:
            return apology("username taken", 400)

    return render_template("register.html")

@app.route("/sell", methods=["GET", "POST"])
@login_required
def sell():
    """Sell shares of stock"""
    user_id = session["user_id"]

    if request.method == "POST":
        try:
            symbol = request.form.get("symbol")
            shares = request.form.get("shares")

            if not symbol or not shares:
                return apology("Please provide symbol and shares", 400)

            try:
                shares = int(shares)
                if shares <= 0:
                    return apology("shares must be more than 0", 400)
            except ValueError:
                return apology("shares must be a number", 400)

            symbol = symbol.upper()
            quote = lookup(symbol)
            if not quote:
                return apology("invalid symbol", 400)
            price = float(quote["price"])

            user_cash = float(db.execute("SELECT cash FROM users WHERE id = ?", user_id)[0]["cash"])
            holdings = db.execute("SELECT symbol, shares FROM portfolio WHERE user_id = ?", user_id)
            
            current_shares = 0
            total_short_liability = 0.0
            nlv = user_cash
            
            for h in holdings:
                sym = h["symbol"]
                s = int(h["shares"])
                if sym == symbol:
                    current_shares = s
                q = lookup(sym)
                if q:
                    p = float(q["price"])
                    nlv += p * s
                    if s < 0:
                        total_short_liability += abs(p * s)

            c_current = user_cash
            s_current = total_short_liability
            cost = -price * shares
            c_after = c_current - cost
            new_shares = current_shares - shares
            
            s_after = s_current
            if current_shares < 0:
                s_after -= abs(current_shares) * price
            if new_shares < 0:
                s_after += abs(new_shares) * price
                
            if c_after < s_after and (c_after - s_after) < (c_current - s_current):
                return apology("insufficient cash for margin requirement", 400)
            if s_after > nlv and s_after > s_current:
                return apology("short value exceeds net liquidation value", 400)

            db.execute("UPDATE users SET cash = ? WHERE id = ?", c_after, user_id)
            db.execute("INSERT INTO transactions (user_id, symbol, shares, price, type) VALUES (?, ?, ?, ?, 'sell')",
                       user_id, symbol, shares, price)

            if current_shares != 0 or len([h for h in holdings if h["symbol"] == symbol]) > 0:
                db.execute("UPDATE portfolio SET shares = ? WHERE user_id = ? AND symbol = ?", new_shares, user_id, symbol)
            else:
                db.execute("INSERT INTO portfolio (user_id, symbol, shares) VALUES (?, ?, ?)", user_id, symbol, -shares)

            db.execute("DELETE FROM portfolio WHERE user_id = ? AND shares = 0", user_id)
            flash("Sold!")
            return redirect("/portfolio")
        except Exception as e:
            logger.error(f"Sell error: {e}", exc_info=True)
            return apology("Something went wrong", 500)

    return render_template("sell.html")

@app.route("/reset_password", methods=["GET", "POST"])
def reset_password():
    """Reset user password using recovery secret"""
    if request.method == "POST":
        username = request.form.get("username")
        recovery_answer = request.form.get("recovery_answer")
        new_password = request.form.get("new_password")
        confirmation = request.form.get("confirmation")

        if not username or not recovery_answer or not new_password or not confirmation:
            flash("Please provide all fields.")
            return render_template("reset_password.html")

        if new_password != confirmation:
            flash("Passwords must match.")
            return render_template("reset_password.html")

        rows = db.execute("SELECT * FROM users WHERE username = ?", username)

        if len(rows) != 1 or not check_password_hash(rows[0]["recovery_answer"], recovery_answer.lower().strip()):
            flash("Invalid username or recovery secret.")
            return render_template("reset_password.html")

        db.execute("UPDATE users SET hash = ? WHERE id = ?", generate_password_hash(new_password), rows[0]["id"])
        flash("Password reset successfully! Please log in.")
        return redirect("/login")
    
    return render_template("reset_password.html")

if __name__ == "__main__":
    # Open browser automatically (only in the main process, not the Werkzeug reloader child)
    import webbrowser
    if not os.environ.get("WERKZEUG_RUN_MAIN"):
        webbrowser.open("http://localhost:5000")
    app.run(debug=True)

    