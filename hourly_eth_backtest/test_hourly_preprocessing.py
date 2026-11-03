"""Hour-frequency regressions: python3 -m unittest eth_factor_project.test_hourly_preprocessing -v."""

import unittest

import numpy as np
import pandas as pd

try:
    from eth_factor_project.preprocessing import MAD_SCALE, temporal_preprocess
except ModuleNotFoundError:
    from preprocessing import MAD_SCALE, temporal_preprocess


class HourlyTemporalPreprocessingTests(unittest.TestCase):
    def setUp(self):
        self.index = pd.date_range("2026-07-01", periods=120, freq="h", tz="UTC")
        self.raw = pd.Series(
            np.sin(np.arange(120) / 3) + np.arange(120) / 20,
            index=self.index,
        )

    def test_future_values_cannot_change_past_hourly_results(self):
        expected = temporal_preprocess(self.raw, window=24, min_periods=12, frequency="h")
        altered = self.raw.copy()
        altered.iloc[70:] = np.arange(50) * 1e9
        actual = temporal_preprocess(altered, window=24, min_periods=12, frequency="h")
        pd.testing.assert_frame_equal(expected.iloc[:70], actual.iloc[:70])

    def test_current_hour_does_not_enter_thresholds_or_scaling(self):
        expected = temporal_preprocess(self.raw, window=24, min_periods=12, frequency="h")
        altered = self.raw.copy()
        altered.iloc[60] = 1e12
        actual = temporal_preprocess(altered, window=24, min_periods=12, frequency="h")
        columns = ["median", "mad", "lower", "upper", "mean", "std", "n_history"]
        pd.testing.assert_series_equal(expected.iloc[60][columns], actual.iloc[60][columns])
        self.assertEqual(actual.iloc[60]["winsorized"], actual.iloc[60]["upper"])

    def test_missing_hour_row_is_rejected_instead_of_compressing_calendar(self):
        with self.assertRaisesRegex(ValueError, "complete hourly UTC calendar"):
            temporal_preprocess(self.raw.drop(self.index[20]), frequency="h")

    def test_missing_hour_value_is_not_zero_or_replaced_by_older_history(self):
        raw = pd.Series([1., 2., np.nan, 4., 5., np.nan], index=self.index[:6])
        result = temporal_preprocess(raw, window=3, min_periods=3, frequency="h")
        self.assertEqual(result.iloc[4]["n_history"], 2)
        self.assertEqual(result.iloc[4]["status"], "insufficient_history")
        self.assertTrue(np.isnan(result.iloc[4]["zscore"]))
        self.assertEqual(result.iloc[5]["status"], "missing_current")
        self.assertTrue(np.isnan(result.iloc[5]["winsorized"]))

    def test_missing_current_hour_stays_missing_with_sufficient_history(self):
        raw = pd.Series([1., 2., 3., 4., np.nan], index=self.index[:5])
        result = temporal_preprocess(raw, window=4, min_periods=4, frequency="h").iloc[-1]
        self.assertEqual(result["status"], "missing_current")
        self.assertEqual(result["n_history"], 4)
        self.assertTrue(np.isnan(result["winsorized"]))
        self.assertTrue(np.isnan(result["zscore"]))
        self.assertAlmostEqual(result["mean"], 2.5)

    def test_zero_mad_is_unavailable_instead_of_zero_signal(self):
        raw = pd.Series([2., 2., 2., 2., 5.], index=self.index[:5])
        result = temporal_preprocess(raw, window=4, min_periods=4, frequency="h").iloc[-1]
        self.assertEqual(result["status"], "zero_mad")
        self.assertTrue(np.isnan(result["winsorized"]))
        self.assertTrue(np.isnan(result["zscore"]))

    def test_window_is_exact_previous_hours_with_finite_values_only(self):
        raw = pd.Series([1e6, 1., 2., np.nan, 4., 5., 100.], index=self.index[:7])
        result = temporal_preprocess(raw, window=5, min_periods=4, frequency="h").iloc[-1]
        history = np.array([1., 2., 4., 5.])
        median = np.median(history)
        mad = np.median(np.abs(history - median))
        lower, upper = median - 3 * MAD_SCALE * mad, median + 3 * MAD_SCALE * mad
        clipped = np.clip(history, lower, upper)
        self.assertEqual(result["n_history"], 4)
        self.assertEqual(result["median"], median)
        self.assertEqual(result["mad"], mad)
        self.assertAlmostEqual(result["std"], clipped.std(ddof=0))
        self.assertAlmostEqual(result["zscore"], (upper - clipped.mean()) / clipped.std(ddof=0))

    def test_sixty_day_hourly_window_and_thirty_day_minimum(self):
        index = pd.date_range("2026-05-01", periods=1442, freq="h", tz="UTC")
        raw = pd.Series(np.arange(1442, dtype=float), index=index)
        result = temporal_preprocess(raw, window=1440, min_periods=720, frequency="h")
        self.assertEqual(result.iloc[719]["status"], "insufficient_history")
        self.assertEqual(result.iloc[720]["status"], "ok")
        self.assertEqual(result.iloc[720]["n_history"], 720)
        self.assertEqual(result.iloc[1440]["n_history"], 1440)
        self.assertEqual(result.iloc[1441]["n_history"], 1440)
        self.assertEqual(result.iloc[1440]["median"], 719.5)
        self.assertEqual(result.iloc[1441]["median"], 720.5)

    def test_non_whole_hour_and_non_utc_indices_are_rejected(self):
        shifted = self.raw.copy()
        shifted.index = shifted.index + pd.Timedelta(minutes=30)
        with self.assertRaisesRegex(ValueError, "whole UTC hours"):
            temporal_preprocess(shifted, frequency="h")
        local = self.raw.tz_convert("Asia/Hong_Kong")
        with self.assertRaisesRegex(ValueError, "must use UTC"):
            temporal_preprocess(local, frequency="h")

    def test_naive_hourly_indices_follow_documented_utc_convention(self):
        expected = temporal_preprocess(self.raw, window=24, min_periods=12, frequency="h")
        actual = temporal_preprocess(
            self.raw.tz_localize(None), window=24, min_periods=12, frequency="h"
        )
        expected.index = expected.index.tz_localize(None)
        pd.testing.assert_frame_equal(expected, actual)

    def test_frequency_must_be_explicit_daily_or_hourly(self):
        with self.assertRaisesRegex(ValueError, "frequency must be"):
            temporal_preprocess(self.raw, frequency="15min")


if __name__ == "__main__":
    unittest.main()
