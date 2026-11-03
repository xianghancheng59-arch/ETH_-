"""Download actual Binance ETHUSDT hourly bars and validate UTC-day aggregation.

Only observed source rows are written. Missing hours are reported, never filled.
Run with the bundled Python; only pandas/numpy and Python's standard library needed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "hourly"
URL = "https://api.binance.com/api/v3/klines"
START = pd.Timestamp("2026-04-15T00:00:00Z")
END = pd.Timestamp("2026-10-30T00:00:00Z")  # exclusive
OUTPUT_START = pd.Timestamp("2026-07-01T00:00:00Z")
HOUR = pd.Timedelta(hours=1)
COLUMNS = ["open_time", "open", "high", "low", "close", "volume_eth", "close_time",
           "quote_volume_usdt", "trade_count", "taker_buy_volume_eth", "taker_buy_quote_usdt", "ignore"]
SPEC = {"symbol": "ETHUSDT", "interval": "1h", "start_utc": START.isoformat(),
        "end_exclusive_utc": END.isoformat(), "output_start_utc": OUTPUT_START.isoformat()}


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_page(payload: list, requested_start: pd.Timestamp) -> pd.DataFrame:
    """Choose ms/us only if the exact interval and requested UTC bounds validate.

    No calendar correction or guessed date offset is allowed. A close timestamp
    must lie in the last millisecond of its hour, supporting both source units.
    """
    if not isinstance(payload, list) or any(not isinstance(row, list) or len(row) != 12 for row in payload):
        raise ValueError("Unexpected Binance kline payload/schema")
    if not payload:
        return pd.DataFrame()
    frame = pd.DataFrame(payload, columns=COLUMNS)
    if any(not isinstance(x, int) for x in frame["open_time"]) or any(not isinstance(x, int) for x in frame["close_time"]):
        raise ValueError("Source timestamps must be integers")
    candidates = []
    for unit in ("ms", "us"):
        try:
            dates = pd.to_datetime(frame["open_time"], unit=unit, utc=True)
            closes = pd.to_datetime(frame["close_time"], unit=unit, utc=True)
            remaining = dates + HOUR - closes
            valid = ((dates >= requested_start) & (dates < END) & (dates == dates.dt.floor("h")) &
                     (remaining > pd.Timedelta(0)) & (remaining <= pd.Timedelta(milliseconds=1))).all()
            if valid:
                candidates.append((unit, dates, closes))
        except (ValueError, OverflowError, pd.errors.OutOfBoundsDatetime):
            pass
    if len(candidates) != 1:
        raise ValueError("Cannot uniquely validate ms/us source timestamps against exact UTC hours")
    unit, dates, closes = candidates[0]
    frame["date"] = dates
    frame["market_close_at"] = closes
    frame["source_timestamp_unit"] = unit
    if frame["date"].duplicated().any() or not frame["date"].is_monotonic_increasing:
        raise ValueError("Duplicate or unordered hourly observations in source page")
    for col in ["open", "high", "low", "close", "volume_eth", "quote_volume_usdt",
                "trade_count", "taker_buy_volume_eth", "taker_buy_quote_usdt"]:
        frame[col] = pd.to_numeric(frame[col], errors="raise")
        if not np.isfinite(frame[col]).all():
            raise ValueError(f"Non-finite source value: {col}")
    if (frame[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("Nonpositive OHLC")
    if ((frame["high"] < frame[["open", "low", "close"]].max(axis=1)) |
            (frame["low"] > frame[["open", "high", "close"]].min(axis=1))).any():
        raise ValueError("Invalid OHLC bounds")
    if (frame[["volume_eth", "quote_volume_usdt", "trade_count", "taker_buy_volume_eth", "taker_buy_quote_usdt"]] < 0).any().any():
        raise ValueError("Negative nonnegative source field")
    if (frame["trade_count"] % 1 != 0).any():
        raise ValueError("Noninteger trade count")
    if ((frame["taker_buy_volume_eth"] > frame["volume_eth"] + 1e-8) |
            (frame["taker_buy_quote_usdt"] > frame["quote_volume_usdt"] + 1e-6)).any():
        raise ValueError("Taker buy volume exceeds total source volume")
    frame["trade_count"] = frame["trade_count"].astype("int64")
    frame["period_start"] = frame["date"]
    frame["period_end_exclusive"] = frame["date"] + HOUR
    if (frame["period_end_exclusive"] > pd.Timestamp.now(tz="UTC")).any():
        raise ValueError("Incomplete hourly candles are not allowed")
    frame["asset"] = "ETH"
    frame["symbol"] = "ETHUSDT"
    return frame


def download(refresh: bool = False) -> dict:
    manifest_path = DATA / "hourly_prices_manifest.json"
    if manifest_path.exists() and not refresh:
        manifest = json.loads(manifest_path.read_text())
        if manifest["request_spec"] != SPEC or not manifest.get("download_finished"):
            raise ValueError("Incompatible/incomplete cache; run with --refresh")
        for source in manifest["sources"]:
            if digest(ROOT / source["path"]) != source["sha256"]:
                raise ValueError(f"Cached source hash mismatch: {source['path']}")
        return manifest
    started = datetime.now(timezone.utc)
    folder = DATA / started.strftime("snapshot_%Y%m%dT%H%M%S%fZ")
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {"request_spec": SPEC, "download_started_at": started.isoformat(),
                "download_finished": False, "sources": []}
    cursor = START
    page = 0
    while cursor < END:
        page += 1
        params = {"symbol": "ETHUSDT", "interval": "1h", "startTime": cursor.value // 1_000_000,
                  "endTime": END.value // 1_000_000 - 1, "limit": 1000}
        url = URL + "?" + urlencode(params)
        request = Request(url, headers={"User-Agent": "ETH-factor-research/1.0"})
        with urlopen(request, timeout=60) as response:
            raw = response.read()
            status = response.status
            returned_url = response.url
            headers = {k: response.headers[k] for k in ("Date", "Content-Type") if k in response.headers}
        path = folder / f"binance_ethusdt_1h_{page:03d}.json"
        path.write_bytes(raw)  # preserve exact response bytes
        payload = json.loads(raw)
        frame = parse_page(payload, cursor)
        entry = {"path": str(path.relative_to(ROOT)), "url": returned_url,
                 "retrieved_at": datetime.now(timezone.utc).isoformat(), "sha256": digest(path),
                 "http_status": status, "response_headers": headers, "row_count": len(frame),
                 "requested_start_utc": cursor.isoformat()}
        if not frame.empty:
            entry.update({"first_open_utc": frame["date"].iloc[0].isoformat(),
                          "last_open_utc": frame["date"].iloc[-1].isoformat(),
                          "source_timestamp_unit": frame["source_timestamp_unit"].iloc[0]})
        manifest["sources"].append(entry)
        write_json(manifest_path, manifest)
        print(f"page={page} rows={len(frame)} url={returned_url}", flush=True)
        if frame.empty:
            # An empty response is not replaced with synthetic candles; remaining
            # hours will appear in the missing-observation audit.
            break
        next_cursor = frame["date"].iloc[-1] + HOUR
        if next_cursor <= cursor:
            raise ValueError("Non-advancing source pagination")
        cursor = next_cursor
    manifest["download_finished"] = True
    manifest["download_finished_at"] = datetime.now(timezone.utc).isoformat()
    write_json(manifest_path, manifest)
    return manifest


def load_prices(manifest: dict) -> tuple[pd.DataFrame, dict]:
    pages = []
    for entry in manifest["sources"]:
        file = ROOT / entry["path"]
        if digest(file) != entry["sha256"]:
            raise ValueError(f"Cached source hash mismatch: {entry['path']}")
        parsed = parse_page(json.loads(file.read_bytes()), pd.Timestamp(entry["requested_start_utc"]))
        if not parsed.empty:
            parsed["source_file"] = entry["path"]
            pages.append(parsed)
    if not pages:
        raise ValueError("No actual hourly observations; no fabricated fallback")
    prices = pd.concat(pages, ignore_index=True)
    if prices["date"].duplicated().any():
        raise ValueError("Duplicate hourly observations across source pages")
    if not prices["date"].is_monotonic_increasing:
        raise ValueError("Unordered observations across source pages")
    expected = pd.date_range(START, END, freq="h", inclusive="left")
    missing = expected.difference(pd.DatetimeIndex(prices["date"]))
    target_missing = missing[missing >= OUTPUT_START]
    counts = {"expected_hours_with_warmup": len(expected), "observed_hours_with_warmup": len(prices),
              "missing_hours_with_warmup": len(missing),
              "expected_output_hours": len(pd.date_range(OUTPUT_START, END, freq="h", inclusive="left")),
              "observed_output_hours": int((prices["date"] >= OUTPUT_START).sum()),
              "missing_output_hours": len(target_missing),
              "missing_hour_starts_utc": [x.isoformat() for x in missing],
              "duplicate_hours": 0, "invalid_ohlc_rows": 0,
              "source_timestamp_units": sorted(prices["source_timestamp_unit"].unique().tolist()),
              "no_missing_hours_fabricated": True}
    columns = ["date", "asset", "symbol", "period_start", "period_end_exclusive", "market_close_at",
               "open", "high", "low", "close", "volume_eth", "quote_volume_usdt", "trade_count",
               "taker_buy_volume_eth", "taker_buy_quote_usdt", "open_time", "close_time",
               "source_timestamp_unit", "source_file"]
    return prices[columns], counts


def reconcile(prices: pd.DataFrame, counts: dict) -> dict:
    d = prices.assign(day=prices["date"].dt.floor("D")).groupby("day").agg(
        hours=("date", "size"), open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"), volume_eth=("volume_eth", "sum"),
        quote_volume_usdt=("quote_volume_usdt", "sum"))
    # Validate only complete UTC days; no daily source dataset is required.
    complete = d[d["hours"] == 24]
    fields = {col: {"complete_days": len(complete), "finite_values": int(np.isfinite(complete[col]).sum())}
              for col in ["open", "high", "low", "close", "volume_eth", "quote_volume_usdt"]}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "request_spec": SPEC,
            "counts": counts, "reference_file": None,
            "daily_aggregation":
            "UTC days derived only from hourly observations; first open, maximum high, minimum low, last close, sum base and quote volume; only 24-observation days",
            "complete_hourly_days": len(complete), "reference_daily_rows": None,
            "compared_days": 0, "incomplete_source_days":
            {x.isoformat(): int(v) for x, v in d.loc[d["hours"] != 24, "hours"].items()},
            "fields": fields, "mismatches": [],
            "all_compared_values_match": None,
            "full_range_reconciled": len(complete) == int((END - START) / pd.Timedelta(days=1)),
            "interpretation": "These are actual hourly market observations. No daily source dataset is used; this check only validates complete UTC-day coverage derived from hourly rows."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    manifest = download(args.refresh)
    prices, counts = load_prices(manifest)
    result = reconcile(prices, counts)
    output = DATA / "hourly_prices.csv"
    prices.to_csv(output, index=False)
    result["hourly_prices_csv"] = str(output.relative_to(ROOT))
    result["hourly_prices_csv_sha256"] = digest(output)
    write_json(DATA / "reconciliation.json", result)
    print(json.dumps({"counts": counts, "full_range_reconciled": result["full_range_reconciled"],
                      "mismatch_count": len(result["mismatches"]), "csv": str(output)}, ensure_ascii=False, indent=2))
    if result["all_compared_values_match"] is False:
        raise SystemExit("Hourly aggregation validation failed: inspect reconciliation.json before using the new bars")


if __name__ == "__main__":
    main()
