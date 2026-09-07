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

    def test_lookup_falls_back_to_london_strategic_edge(self):
        lse_quote = {"symbol": "AAPL", "price": 200}
        with patch.object(helpers, "_lookup_twelvedata", return_value=None), \
                patch.object(helpers, "_lookup_lse", return_value=lse_quote):
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


if __name__ == "__main__":
    unittest.main()