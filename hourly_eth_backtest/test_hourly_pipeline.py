"""Synthetic isolated hourly checks; never write to project input/output data.

Run: python3 -m unittest eth_factor_project.test_hourly_pipeline -v
"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

try:
    from eth_factor_project import hourly_pipeline as hp
except ModuleNotFoundError:
    import hourly_pipeline as hp


def price_input(periods=4):
    dates = pd.date_range("2026-08-27", periods=periods, freq="h", tz="UTC")
    return pd.DataFrame({
        "date": dates,
        "asset": "ETH",
        "symbol": "ETHUSDT",
        "period_start": dates,
        "period_end_exclusive": dates + hp.HOUR,
        "market_close_at": dates + hp.HOUR - pd.Timedelta(milliseconds=1),
        "open": 2400., "high": 2420., "low": 2380., "close": 2410.,
        "volume_eth": 1., "quote_volume_usdt": 2400., "trade_count": 10,
        "taker_buy_volume_eth": 0.6, "taker_buy_quote_usdt": 1440.,
        "open_time": dates.as_unit("ms").asi8,
        "close_time": (dates + hp.HOUR).as_unit("ms").asi8 - 1,
        "source_timestamp_unit": "ms", "source_file": "synthetic_fixture_only",
    })


def chain_input(dates):
    n = len(dates)
    return pd.DataFrame({
        "date": dates, "asset": "ETH",
        "flow_in_usd": np.full(n, 10.), "flow_out_usd": np.full(n, 30.),
        "market_cap_usd": 1000. + np.arange(n) * 100.,
        "flow_available_at": dates + hp.HOUR + pd.Timedelta(minutes=5),
        "cap_available_at": dates + hp.HOUR + pd.Timedelta(minutes=2),
        "flow_version_at": pd.Timestamp("2026-10-31T00:00:00Z"),
        "cap_version_at": pd.Timestamp("2026-10-31T00:00:00Z"),
        "source_url": "https://example.test/synthetic-hourly-fixture",
    })


class HourlyPipelineTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((hp.ROOT / "hourly_config.json").read_text())
        self.config.update({
            "data_start": "2026-08-27T00:00:00Z",
            "output_start": "2026-08-27T00:00:00Z",
            "output_end_exclusive": "2026-08-27T04:00:00Z",
            "hourly_chain_csv": None,
            "include_market_factors": False,
        })

    def load_fixture(self, inp=None):
        with patch.object(hp.pd, "read_csv", return_value=(price_input() if inp is None else inp).copy()):
            return hp.load_prices(self.config)

    def attach_fixture(self, d, inp):
        config = dict(self.config, hourly_chain_csv="synthetic_fixture_only.csv")
        with patch.object(hp.pd, "read_csv", return_value=inp.copy()):
            return hp.attach_chain(d, config)

    def test_hour_labels_have_exact_start_end_and_source_close(self):
        d = self.load_fixture()
        pd.testing.assert_index_equal(d.index, pd.date_range(
            "2026-08-27", periods=4, freq="h", tz="UTC", name="date"))
        self.assertTrue(d["period_start"].eq(d.index).all())
        self.assertTrue(d["period_end_exclusive"].eq(d.index + hp.HOUR).all())
        self.assertTrue(d["market_close_at"].eq(d.index + hp.HOUR - pd.Timedelta(milliseconds=1)).all())

    def test_invalid_close_time_is_rejected(self):
        inp = price_input()
        inp.loc[1, "market_close_at"] = inp.loc[1, "date"] + pd.Timedelta(minutes=15)
        with self.assertRaises(ValueError):
            self.load_fixture(inp)

    def test_invalid_period_end_is_rejected_instead_of_overwritten(self):
        inp = price_input()
        inp.loc[1, "period_end_exclusive"] = inp.loc[1, "date"] + 2 * hp.HOUR
        with self.assertRaises(ValueError):
            self.load_fixture(inp)

    def test_missing_price_hour_remains_on_calendar_and_unavailable(self):
        d = self.load_fixture(price_input().drop(index=1))
        self.assertEqual(len(d), 4)
        self.assertTrue(d.iloc[1][["open", "high", "low", "close", "quote_volume_usdt"]].isna().all())
        self.assertFalse(d.iloc[1]["price_complete"])
        self.assertTrue(np.isnan(d.iloc[1]["return_1h"]))
        self.assertTrue(np.isnan(d.iloc[2]["return_1h"]))

    def test_equal_high_low_makes_clv_missing(self):
        inp = price_input()
        inp.loc[1, ["open", "high", "low", "close"]] = 2400.
        d = self.load_fixture(inp)
        self.assertTrue(np.isnan(d.iloc[1]["clv"]))
        attached = self.attach_fixture(d, chain_input(d.index))
        raw, _ = hp.chain_factors(attached, self.config)
        self.assertTrue(np.isnan(raw.iloc[1]["support_resilience_2400"]))

    def test_price_schema_and_duplicates_fail(self):
        with self.assertRaises((ValueError, KeyError)):
            self.load_fixture(price_input().drop(columns="high"))
        inp = price_input()
        with self.assertRaisesRegex(ValueError, "Duplicate price"):
            self.load_fixture(pd.concat([inp, inp.iloc[[0]]], ignore_index=True))

    def test_missing_price_file_has_no_synthetic_fallback(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(hp, "ROOT", Path(folder)):
            with self.assertRaises(FileNotFoundError):
                hp.load_prices(self.config)

    def test_no_chain_input_means_all_original_factors_are_missing(self):
        d = self.load_fixture()
        with patch.object(hp.pd, "read_csv", side_effect=AssertionError("No default chain read allowed")):
            attached = hp.attach_chain(d, self.config)
        raw, catalog = hp.chain_factors(attached, self.config)
        self.assertEqual(len(catalog), 8)
        self.assertTrue(raw.isna().all().all())
        self.assertFalse(attached["chain_inputs_complete"].any())
        self.assertFalse(attached["strict_pit_eligible"].any())
        self.assertTrue(attached[["flow_in_usd", "flow_out_usd", "market_cap_usd"]].isna().all().all())

    def test_denominator_uses_previous_hour_cap_never_current(self):
        d = self.load_fixture()
        inp = chain_input(d.index)
        attached = self.attach_fixture(d, inp)
        raw, _ = hp.chain_factors(attached, self.config)
        self.assertTrue(np.isnan(raw.iloc[0]["net_outflow_cap"]))
        self.assertAlmostEqual(raw.iloc[1]["net_outflow_cap"], 20 / 1000)
        altered = inp.copy()
        altered.loc[1, "market_cap_usd"] = 1e12
        changed, _ = hp.chain_factors(self.attach_fixture(d, altered), self.config)
        self.assertEqual(raw.iloc[1]["net_outflow_cap"], changed.iloc[1]["net_outflow_cap"])
        self.assertNotEqual(raw.iloc[2]["net_outflow_cap"], changed.iloc[2]["net_outflow_cap"])

    def test_missing_chain_hour_is_not_zero_forward_fill_or_day_split(self):
        d = self.load_fixture()
        attached = self.attach_fixture(d, chain_input(d.index).drop(index=1))
        raw, _ = hp.chain_factors(attached, self.config)
        self.assertTrue(attached.iloc[1][["flow_in_usd", "flow_out_usd", "market_cap_usd"]].isna().all())
        self.assertTrue(raw.iloc[1].isna().all())
        self.assertTrue(np.isnan(attached.iloc[2]["market_cap_previous_hour_usd"]))
        self.assertTrue(np.isnan(raw.iloc[2]["net_outflow_cap"]))
        self.assertTrue(np.isfinite(raw.iloc[2]["flow_imbalance"]))

    def test_chain_schema_and_duplicate_key_fail(self):
        d = self.load_fixture()
        inp = chain_input(d.index)
        with self.assertRaisesRegex(ValueError, "missing columns"):
            self.attach_fixture(d, inp.drop(columns="cap_available_at"))
        with self.assertRaisesRegex(ValueError, "Duplicate hourly chain key"):
            self.attach_fixture(d, pd.concat([inp, inp.iloc[[0]]], ignore_index=True))

    def test_availability_cannot_precede_completed_hour_and_never_implies_pit(self):
        d = self.load_fixture()
        inp = chain_input(d.index)
        attached = self.attach_fixture(d, inp)
        self.assertFalse(attached["strict_pit_eligible"].any())
        early = inp.copy()
        early.loc[1, "cap_available_at"] = early.loc[1, "date"]
        with self.assertRaisesRegex(ValueError, "cannot be available before"):
            self.attach_fixture(d, early)

    def test_used_cap_has_separate_previous_hour_availability_provenance(self):
        d = self.load_fixture()
        inp = chain_input(d.index)
        attached = self.attach_fixture(d, inp)
        self.assertIn("market_cap_previous_hour_available_at", attached)
        self.assertIn("market_cap_previous_hour_version_at", attached)
        self.assertEqual(attached.iloc[1]["market_cap_previous_hour_available_at"], inp.iloc[0]["cap_available_at"])
        self.assertEqual(attached.iloc[1]["market_cap_previous_hour_version_at"], inp.iloc[0]["cap_version_at"])
        self.assertNotEqual(attached.iloc[1]["market_cap_previous_hour_available_at"], attached.iloc[1]["cap_available_at"])

    def test_known_time_bound_uses_current_flow_and_previous_cap_versions(self):
        d = self.load_fixture()
        inp = chain_input(d.index)
        inp.loc[0, "cap_version_at"] = pd.Timestamp("2026-11-01T00:00:00Z")
        inp.loc[1, "cap_version_at"] = pd.Timestamp("2026-11-02T00:00:00Z")
        attached = self.attach_fixture(d, inp)
        self.assertTrue(pd.isna(attached.iloc[0]["chain_known_input_time_lower_bound"]))
        self.assertEqual(attached.iloc[1]["chain_known_input_time_lower_bound"], inp.iloc[0]["cap_version_at"])
        self.assertEqual(attached.iloc[2]["chain_known_input_time_lower_bound"], inp.iloc[1]["cap_version_at"])
        self.assertTrue(attached.iloc[1]["chain_input_timing_complete"])
        self.assertFalse(attached["strict_pit_eligible"].any())

    def test_missing_version_times_are_flagged_even_with_numeric_inputs(self):
        d = self.load_fixture()
        inp = chain_input(d.index).drop(columns=["flow_version_at", "cap_version_at"])
        attached = self.attach_fixture(d, inp)
        self.assertTrue(attached.iloc[1]["chain_inputs_complete"])
        self.assertFalse(attached["chain_input_timing_complete"].any())
        self.assertFalse(attached["strict_pit_eligible"].any())
        self.assertEqual(attached.iloc[1]["chain_known_input_time_lower_bound"], inp.iloc[1]["flow_available_at"])

    def test_momentum_is_72_and_336_completed_hour_observations(self):
        dates = pd.date_range("2026-08-01", periods=400, freq="h", tz="UTC", name="date")
        q = np.arange(400, dtype=float) / 1000
        d = pd.DataFrame({"flow_in_usd": 1., "flow_out_usd": 1. + q * 1e6,
                          "market_cap_previous_hour_usd": 1e6, "clv": 0.5,
                          "proximity_2400": 0.9, "proximity_2300": 0.5}, index=dates)
        raw, _ = hp.chain_factors(d, self.config)
        self.assertTrue(raw["outflow_momentum_72_336h"].iloc[:335].isna().all())
        self.assertAlmostEqual(raw.iloc[335]["outflow_momentum_72_336h"], q[264:336].mean() - q[:336].mean())
        altered = d.copy()
        altered.iloc[336:, altered.columns.get_loc("flow_out_usd")] = 1e12
        changed, _ = hp.chain_factors(altered, self.config)
        pd.testing.assert_frame_equal(raw.iloc[:336], changed.iloc[:336])
        d.iloc[300, d.columns.get_loc("flow_out_usd")] = np.nan
        missing, _ = hp.chain_factors(d, self.config)
        self.assertTrue(missing["outflow_momentum_72_336h"].iloc[335:].isna().all())

    def test_market_netbuy_is_aggressor_imbalance_with_past_volume_denominator(self):
        dates = pd.date_range("2026-08-01", periods=25, freq="h", tz="UTC", name="date")
        d = pd.DataFrame({"taker_buy_quote_usdt": 60., "taker_sell_quote_usdt": 40.,
                          "quote_volume_usdt": 100., "clv": 0.5,
                          "proximity_2400": 0.9, "proximity_2300": 0.5}, index=dates)
        d.loc[dates[-1], ["taker_buy_quote_usdt", "taker_sell_quote_usdt", "quote_volume_usdt"]] = [200., 50., 250.]
        raw, catalog = hp.market_factors(d, self.config)
        self.assertAlmostEqual(raw.iloc[-1]["taker_net_buy_intensity"], (200 - 50) / (24 * 100))
        self.assertAlmostEqual(raw.iloc[-1]["taker_buy_sell_imbalance"], (200 - 50) / 250)
        self.assertAlmostEqual(raw.iloc[-1]["trade_activity"], 250 / 100)
        self.assertTrue(raw["taker_net_buy_intensity"].iloc[:24].isna().all())
        self.assertTrue(all(entry["family"] == "market" for entry in catalog))
        self.assertFalse(any("净流出" in entry["name_zh"] or "净资金流" in entry["name_zh"] for entry in catalog))
        self.assertNotIn("net_outflow_cap", raw)

    def test_default_run_exports_only_missing_chain_factors_to_temp_directory(self):
        self.config["output_end_exclusive"] = "2026-08-28T00:00:00Z"
        d = self.load_fixture(price_input(24))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data/hourly").mkdir(parents=True)
            (root / "data/hourly/reconciliation.json").write_text('{"synthetic_test_fixture":true}')
            with patch.object(hp, "ROOT", root), patch.object(hp, "OUT", root / "out"), \
                    patch.object(hp, "load_prices", return_value=d):
                manifest = hp.run(self.config)
            result = pd.read_csv(root / "out/factors_long.csv")
            self.assertEqual(manifest["chain_complete_hours"], 0)
            self.assertEqual(manifest["output_hours"], 24)
            self.assertFalse(manifest["strict_pit_available"])
            self.assertEqual(set(result["family"]), {"chain"})
            self.assertEqual(result["factor"].nunique(), 8)
            self.assertTrue(result[["raw", "winsorized", "zscore"]].isna().all().all())
            self.assertTrue((result["status"] == "missing_current").all())


if __name__ == "__main__":
    unittest.main()
