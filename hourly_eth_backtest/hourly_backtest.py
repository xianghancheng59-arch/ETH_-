"""Point-in-time-aware hourly factor evaluation.

Signals are formed from a completed UTC hour.  A one-hour execution label
enters at the next bar's open, so the signal bar's close is never reused as an
entry price.  Snapshot results are reported separately from rows whose source
availability timestamps prove historical availability.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output_hourly"
HORIZONS = (1, 3, 6, 12, 24, 72)
FEE_BPS = 2.0


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    aligned = pd.read_csv(OUT / "aligned_hourly.csv", parse_dates=["date"])
    factors = pd.read_csv(OUT / "factors_long.csv", parse_dates=["date"])
    for frame in (aligned, factors):
        frame["date"] = pd.to_datetime(frame["date"], utc=True)
    if aligned["date"].duplicated().any():
        raise ValueError("Duplicate aligned hourly keys")
    if factors.duplicated(["date", "asset", "factor"]).any():
        raise ValueError("Duplicate factor hourly keys")
    expected = pd.date_range(aligned["date"].min(), aligned["date"].max(), freq="h", tz="UTC")
    if not aligned["date"].equals(pd.Series(expected, name="date")):
        raise ValueError("Aligned input is not a complete UTC hourly calendar")
    return aligned.sort_values("date").reset_index(drop=True), factors.sort_values(["factor", "date"]).reset_index(drop=True)


def add_labels(aligned: pd.DataFrame) -> pd.DataFrame:
    d = aligned.copy().sort_values("date").reset_index(drop=True)
    d["signal_period_end"] = pd.to_datetime(d["period_end_exclusive"], utc=True)
    d["entry_time"] = d["date"] + pd.Timedelta(hours=1)
    d["entry_open"] = d["open"].shift(-1)
    for h in HORIZONS:
        d[f"exit_time_{h}h"] = d["date"] + pd.Timedelta(hours=h + 1)
        d[f"exit_close_{h}h"] = d["close"].shift(-h)
        d[f"forward_open_to_close_{h}h"] = d[f"exit_close_{h}h"] / d["entry_open"] - 1.0
    availability = pd.DataFrame(
        {
            "signal_period_end": d["signal_period_end"],
            "flow_available_at": pd.to_datetime(d["flow_available_at"], utc=True),
            "previous_cap_available_at": pd.to_datetime(d["market_cap_previous_hour_available_at"], utc=True),
        }
    )
    d["signal_available_at"] = availability.max(axis=1)
    d["historical_pit_eligible"] = d["signal_available_at"].notna() & (d["signal_available_at"] <= d["entry_time"])
    return d


def event_results(aligned: pd.DataFrame, factors: pd.DataFrame) -> pd.DataFrame:
    labels = add_labels(aligned)
    rows = []
    for (factor, asset), g in factors.groupby(["factor", "asset"], sort=True):
        x = labels.loc[labels["asset"].eq(asset), ["date", "entry_time", "signal_available_at", "historical_pit_eligible"] +
                       [f"forward_open_to_close_{h}h" for h in HORIZONS]].merge(
            g[["date", "raw", "zscore", "status"]], on="date", how="left", validate="one_to_one")
        for signal_name in ("raw", "zscore"):
            x["signal"] = pd.to_numeric(x[signal_name], errors="coerce")
            x["position"] = np.sign(x["signal"])
            for h in HORIZONS:
                label = f"forward_open_to_close_{h}h"
                for sample_name, mask in (("snapshot", pd.Series(True, index=x.index)),
                                          ("pit_eligible_only", x["historical_pit_eligible"])):
                    valid = x["position"].ne(0) & x["position"].notna() & x[label].notna() & mask
                    pnl = x.loc[valid, "position"] * x.loc[valid, label]
                    rows.append({
                        "factor": factor, "asset": asset, "signal": signal_name,
                        "horizon_hours": h, "sample": sample_name, "observations": int(len(pnl)),
                        "mean_return": float(pnl.mean()) if len(pnl) else np.nan,
                        "volatility": float(pnl.std(ddof=1)) if len(pnl) > 1 else np.nan,
                        "annualized_sharpe_event": float(pnl.mean() / pnl.std(ddof=1) * math.sqrt(8760 / h)) if len(pnl) > 1 and pnl.std(ddof=1) > 0 else np.nan,
                        "hit_rate": float((pnl > 0).mean()) if len(pnl) else np.nan,
                        "compounded_event_return": float((1 + pnl).prod() - 1) if len(pnl) else np.nan,
                        "strict_pit_eligible": sample_name == "pit_eligible_only",
                    })
    return pd.DataFrame(rows)


def one_hour_strategies(aligned: pd.DataFrame, factors: pd.DataFrame) -> pd.DataFrame:
    labels = add_labels(aligned)
    rows = []
    for (factor, asset), g in factors.groupby(["factor", "asset"], sort=True):
        x = labels.loc[labels["asset"].eq(asset), ["date", "entry_time", "historical_pit_eligible", "open", "close", "entry_open", "forward_open_to_close_1h"]].merge(
            g[["date", "raw", "zscore"]], on="date", how="left", validate="one_to_one").sort_values("date")
        for signal_name in ("raw", "zscore"):
            x["position"] = np.sign(pd.to_numeric(x[signal_name], errors="coerce")).fillna(0.0)
            x["gross_return"] = x["position"] * x["forward_open_to_close_1h"]
            x["turnover"] = (x["position"] - x["position"].shift(1).fillna(0.0)).abs()
            x["fee_return"] = x["turnover"] * FEE_BPS / 10000.0
            x["net_return"] = x["gross_return"] - x["fee_return"]
            x["factor"], x["asset"], x["signal"] = factor, asset, signal_name
            rows.append(x[["date", "entry_time", "factor", "asset", "signal", "position", "forward_open_to_close_1h", "gross_return", "turnover", "fee_return", "net_return", "historical_pit_eligible"]])
    return pd.concat(rows, ignore_index=True)


def strategy_summary(returns: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (factor, asset, signal), g in returns.groupby(["factor", "asset", "signal"], sort=True):
        for sample, x in (("snapshot", g), ("pit_eligible_only", g[g["historical_pit_eligible"]])):
            x = x.loc[x["forward_open_to_close_1h"].notna() & x["position"].ne(0)]
            net = x["net_return"]
            rows.append({
                "factor": factor, "asset": asset, "signal": signal, "sample": sample,
                "observations": int(len(x)), "mean_net_return": float(net.mean()) if len(x) else np.nan,
                "volatility_net_return": float(net.std(ddof=1)) if len(net) > 1 else np.nan,
                "annualized_sharpe": float(net.mean() / net.std(ddof=1) * math.sqrt(8760)) if len(net) > 1 and net.std(ddof=1) > 0 else np.nan,
                "hit_rate": float((net > 0).mean()) if len(x) else np.nan,
                "compounded_net_return": float((1 + net).prod() - 1) if len(x) else np.nan,
                "total_turnover": float(x["turnover"].sum()) if len(x) else 0.0,
                "fee_bps_per_turnover": FEE_BPS,
                "strict_pit_eligible": sample == "pit_eligible_only",
            })
    return pd.DataFrame(rows)


def run() -> dict:
    aligned, factors = load()
    events = event_results(aligned, factors)
    hourly = one_hour_strategies(aligned, factors)
    summary = strategy_summary(hourly)
    OUT.mkdir(exist_ok=True)
    events.to_csv(OUT / "event_forward_returns.csv", index=False, float_format="%.12g")
    hourly.to_csv(OUT / "hourly_strategy_returns.csv", index=False, float_format="%.12g")
    summary.to_csv(OUT / "backtest_summary.csv", index=False, float_format="%.12g")
    manifest = {
        "frequency": "1h", "timezone": "UTC", "signal_rule": "factor at completed hour t",
        "execution_rule": "enter at next hour open; exit at close after the stated horizon",
        "horizons_hours": list(HORIZONS), "fee_bps_per_turnover": FEE_BPS,
        "snapshot_results_are_pit_qualified": False,
        "pit_eligible_rows": int(aligned["strict_pit_eligible"].fillna(False).sum()),
        "chain_complete_hours": int(aligned["chain_inputs_complete"].sum()),
        "output_hours": int(len(aligned)),
        "source_files": {
            "aligned_hourly": {"path": "output_hourly/aligned_hourly.csv", "sha256": sha256(OUT / "aligned_hourly.csv")},
            "factors_long": {"path": "output_hourly/factors_long.csv", "sha256": sha256(OUT / "factors_long.csv")},
        },
        "limitations": [
            "Santiment flow snapshot was downloaded after the sample; strict historical PIT eligibility is false.",
            "660 target hours have missing chain inputs and are excluded from factor events; no fill or daily disaggregation is used.",
            "Overlapping multi-hour event labels are descriptive and are not compounded into a tradable portfolio.",
            "The one-hour strategy includes a 2 bps turnover cost but no spread, slippage, funding, borrow, or market impact model.",
        ],
    }
    (OUT / "hourly_backtest_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.parse_args()
    print(json.dumps(run(), ensure_ascii=False, indent=2))
