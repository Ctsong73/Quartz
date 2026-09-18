import os
import unittest
from unittest.mock import patch

import helpers


class ProviderTests(unittest.TestCase):
    def test_lookup_prefers_twelve_data(self):
        twelve_quote = {"symbol": "AAPL", "price": 200}
        with patch.object(helpers, "_lookup_twelvedata", return_value=twelve_quote) as twelve, \
                patch.object(helpers, "_lookup_fmp") as fmp:
            self.assertEqual(helpers.lookup("aapl"), twelve_quote)
            twelve.assert_called_once_with("AAPL")
            fmp.assert_not_called()

    def test_lookup_prefers_twelve_data_for_live_prices(self):
        twelve_quote = {"symbol": "AAPL", "price": 200}
        with patch.object(helpers, "_lookup_twelvedata", return_value=twelve_quote) as twelve, \
                patch.object(helpers, "_lookup_lse") as lse:
            self.assertEqual(helpers.lookup("AAPL"), twelve_quote)
            twelve.assert_called_once_with("AAPL")
            lse.assert_not_called()

    def test_lookup_falls_back_to_twelve_data_for_brent(self):
        twelve_quote = {"symbol": "BZ=F", "price": 97.25}
        with patch.object(helpers, "_lookup_yahoo_futures", return_value=None), \
                patch.object(helpers, "_lookup_twelvedata", return_value=twelve_quote):
            self.assertEqual(helpers.lookup("BZ=F")["price"], 97.25)

    def test_lookup_preserves_futures_exchange_when_metadata_is_unavailable(self):
        self.assertEqual(helpers._fallback_exchange("BZ=F"), "ICE")

    def test_twelve_data_precedes_lse_for_stocks(self):
        """Twelve Data should still be tried first for equities (non-futures)."""
        twelve_quote = {"symbol": "AAPL", "price": 200, "sector": "Technology",
                        "exchange": "NASDAQ", "description": "Apple Inc."}
        with patch.object(helpers, "_lookup_lse") as lse, \
                patch.object(helpers, "_lookup_twelvedata", return_value=twelve_quote), \
                patch.object(helpers, "_enrich_stock_metadata"):
            quote = helpers.lookup("AAPL")
        self.assertEqual(quote["price"], 200)
        lse.assert_not_called()

    def test_lookup_converts_lse_soybean_units(self):
        lse_quote = {"symbol": "SOYBN/USD", "price": 12.964, "price_7d": 13.1, "price_30d": 13.2}
        with patch.object(helpers, "_lookup_lse", return_value=lse_quote), \
                patch.object(helpers, "_twelvedata_quote", return_value=None):
            quote = helpers.lookup("ZS=F")
        self.assertEqual(quote["price"], 1296.4)
        self.assertEqual(quote["name"], "Soybean Futures")
        self.assertEqual(quote["exchange"], "CBOT")
    def test_yahoo_is_final_fallback_for_futures(self):
        """Yahoo is the last resort: TD, LSE, FMP, then yfinance all tried first."""
        yahoo_quote = {"symbol": "BZ=F", "price": 96.28, "price_7d": 95.5, "price_30d": 94.0}
        with patch.object(helpers, "_lookup_yahoo_futures", return_value=yahoo_quote) as yahoo, \
                patch.object(helpers, "_lookup_lse", return_value=None) as lse, \
                patch.object(helpers, "_lookup_fmp", return_value=None) as fmp, \
                patch.object(helpers, "_lookup_twelvedata", return_value=None) as twelve, \
                patch.object(helpers, "_lookup_yfinance", return_value=None):
            quote = helpers.lookup("BZ=F")
        self.assertEqual(quote["price"], 96.28)
        yahoo.assert_called()
        lse.assert_called()
        fmp.assert_called()
        twelve.assert_called()

    def test_lse_precedes_twelve_data_for_futures(self):
        """LSE should be tried before FMP/yfinance once Twelve Data is down."""
        lse_quote = {"symbol": "BZ=F", "price": 97.10, "price_7d": 96.5, "price_30d": 95.0}
        with patch.object(helpers, "_lookup_yahoo_futures", return_value=None), \
                patch.object(helpers, "_lookup_lse", return_value=lse_quote) as lse, \
                patch.object(helpers, "_lookup_twelvedata", return_value=None) as twelve:
            quote = helpers.lookup("BZ=F")
        self.assertEqual(quote["price"], 97.10)
        lse.assert_called()
        twelve.assert_called()

    def test_ukoil_alias_precedes_bco_for_brent(self):
        """UKOIL should be tried before BCO/USD for front-month accuracy."""
        aliases = helpers._provider_symbols("BZ=F")
        bco_index = aliases.index("BCO/USD")
        ukoil_index = aliases.index("UKOIL")
        self.assertLess(ukoil_index, bco_index)

    def test_is_futures_detects_yahoo_style_symbols(self):
        self.assertTrue(helpers._is_futures("BZ=F"))
        self.assertTrue(helpers._is_futures("GC=F"))
        self.assertTrue(helpers._is_futures("CL=F"))
        self.assertFalse(helpers._is_futures("AAPL"))
        self.assertFalse(helpers._is_futures("NVDA"))

    def test_is_futures_detects_provider_aliases(self):
        self.assertTrue(helpers._is_futures("UKOIL"))
        self.assertTrue(helpers._is_futures("BCO/USD"))
        self.assertTrue(helpers._is_futures("XAU/USD"))
        self.assertFalse(helpers._is_futures("MSFT"))

    def test_enrich_stock_metadata_fills_missing_fields(self):
        result = {"sector": "", "exchange": "Twelve Data", "description": "No description available."}
        meta = {"sector": "Technology", "exchange": "NASDAQ", "description": "Apple Inc description."}
        with patch.object(helpers, "_yahoo_metadata", return_value=meta), \
                patch.object(helpers, "_fmp_profile", return_value={}):
            helpers._enrich_stock_metadata(result, "AAPL")
        self.assertEqual(result["sector"], "Technology")
        self.assertEqual(result["exchange"], "NASDAQ")
        self.assertEqual(result["description"], "Apple Inc description.")

    def test_enrich_stock_metadata_preserves_existing_fields(self):
        """Should not overwrite fields that already have valid values."""
        result = {"sector": "Financials", "exchange": "NYSE", "description": "A bank."}
        with patch.object(helpers, "_yahoo_metadata") as yahoo:
            helpers._enrich_stock_metadata(result, "JPM")
        yahoo.assert_not_called()
        self.assertEqual(result["sector"], "Financials")

    def test_lookup_falls_back_to_london_strategic_edge(self):
        lse_quote = {"symbol": "AAPL", "price": 200, "sector": "",
                     "exchange": "London Strategic Edge", "description": "No description available."}
        with patch.object(helpers, "_lookup_twelvedata", return_value=None), \
                patch.object(helpers, "_lookup_lse", return_value=lse_quote), \
                patch.object(helpers, "_enrich_stock_metadata"):
            self.assertEqual(helpers.lookup("AAPL"), lse_quote)

    def test_history_normalizes_provider_rows(self):
        rows = []
        for index in range(40):
            price = 100 + index
            rows.append({
                "t": f"2026-01-{index + 1:02d}T00:00:00Z",
                "o": price - 1,
                "h": price + 1,
                "l": price - 2,
                "c": price,
            })
        with patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "key"}), \
            patch.object(helpers, "_twelvedata_series", return_value=[{
                "datetime": row["t"],
                "open": row["o"],
                "high": row["h"],
                "low": row["l"],
                "close": row["c"],
            } for row in rows]):
            history = helpers.get_history("AAPL")
        self.assertIsNotNone(history)
        self.assertEqual(history["close"][-1], 139.0)
        self.assertEqual(len(history["close"]), len(history["rsi"]))

    def test_lse_candles_are_normalized_for_history(self):
        rows = []
        for index in range(40):
            price = 100 + index
            rows.append({
                "ts": f"2026-01-{index + 1:02d} 00:00:00.000000",
                "open": price - 1,
                "high": price + 1,
                "low": price - 2,
                "close": price,
            })
        with patch.dict(os.environ, {"LSE_API_KEY": "lse_live_test"}), \
                patch.object(helpers, "_lse_candles", return_value=rows):
            history = helpers.get_history("AAPL")
        self.assertIsNotNone(history)
        self.assertEqual(history["close"][-1], 139.0)

    def test_lse_live_quote_requests_newest_candle(self):
        with patch.dict(os.environ, {"LSE_API_KEY": "lse_live_test"}), \
                patch.object(helpers, "_lse_candles", side_effect=[
                    [{"close": 125}],
                    [{"close": 120}, {"close": 121}],
                ]) as candles:
            quote = helpers._lookup_lse("BRENT")
        self.assertEqual(quote["price"], 125)
        self.assertEqual(candles.call_args_list[0].kwargs, {"limit": 1, "timeframe": "1m", "order": "desc"})


    def test_brent_next_contract_symbol(self):
        self.assertEqual(helpers._brent_next_contract("2026-10-01T00:00:00Z"), "BZX26.NYM")
        self.assertEqual(helpers._brent_next_contract("2026-11-02T00:00:00Z"), "BZZ26.NYM")
        self.assertEqual(helpers._brent_next_contract("2026-12-01T00:00:00Z"), "BZF27.NYM")
        self.assertIsNone(helpers._brent_next_contract("garbage"))
        self.assertIsNone(helpers._days_until_expiry("garbage"))

    def test_brent_rolls_to_next_contract_inside_expiry_window(self):
        from datetime import datetime, timezone, timedelta

        class FakeResp:
            status_code = 200
            def __init__(self, payload):
                self._payload = payload
            def json(self):
                return self._payload

        seen = []
        front_exp = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%dT00:00:00Z")
        def fake_get(url, params=None, timeout=4):
            sym = params.get("symbols")
            seen.append(sym)
            is_next = "26.NYM" in sym
            return FakeResp({"quoteResponse": {"result": [{
                "symbol": sym,
                "regularMarketPrice": 103.72 if is_next else 98.90,
                "regularMarketPreviousClose": 103.20 if is_next else 98.50,
                "expireIsoDate": front_exp if not is_next else "2026-11-02T00:00:00Z",
            }]}})

        class FakeSession:
            def get(self, url, params=None, timeout=4):
                return fake_get(url, params, timeout)

        with patch.object(helpers, "_get_yahoo_crumb", return_value=(FakeSession(), "crumb")):
            q = helpers._lookup_yahoo_futures("UKOIL")
        self.assertIsNotNone(q)
        self.assertEqual(q["price"], 103.72)
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1])

    def test_brent_keeps_front_outside_expiry_window(self):
        from datetime import datetime, timezone, timedelta

        class FakeResp:
            status_code = 200
            def __init__(self, payload):
                self._payload = payload
            def json(self):
                return self._payload

        seen = []
        front_exp = (datetime.now(timezone.utc) + timedelta(days=90)).strftime("%Y-%m-%dT00:00:00Z")
        def fake_get(url, params=None, timeout=4):
            seen.append(params.get("symbols"))
            return FakeResp({"quoteResponse": {"result": [{
                "symbol": "BZ=F",
                "regularMarketPrice": 98.90,
                "regularMarketPreviousClose": 98.50,
                "expireIsoDate": front_exp,
            }]}})

        class FakeSession:
            def get(self, url, params=None, timeout=4):
                return fake_get(url, params, timeout)

        with patch.object(helpers, "_get_yahoo_crumb", return_value=(FakeSession(), "crumb")):
            q = helpers._lookup_yahoo_futures("BZ=F")
        self.assertEqual(q["price"], 98.90)
        self.assertEqual(seen, ["BZ=F"])


if __name__ == "__main__":
    unittest.main()