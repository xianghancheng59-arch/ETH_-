"""Hourly ETH research using observed hourly chain inputs.

Santiment's public GraphQL hourly exchange flows are converted from ETH to USD
using the same-hour Santiment USD price. Missing source hours remain NaN; daily
flows are never disaggregated or forward-filled. Market microstructure factors
are a separate, explicitly opted-in family, never aliases for exchange flows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

if __package__:
    from .preprocessing import temporal_preprocess
else:
    from preprocessing import temporal_preprocess

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output_hourly"
HOUR = pd.Timedelta(hours=1)


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def load_prices(config: dict) -> pd.DataFrame:
    source = ROOT / "data/hourly/hourly_prices.csv"
    d = pd.read_csv(source)
    d["date"] = pd.to_datetime(d["date"], utc=True)
    if d["date"].duplicated().any():
        raise ValueError("Duplicate price hour")
    if set(d["asset"].dropna()) != {config["asset"]}:
        raise ValueError("Hourly price file asset does not match configuration")
    for col in ("period_start", "period_end_exclusive", "market_close_at"):
        d[col] = pd.to_datetime(d[col], utc=True)
    if not d["date"].eq(d["date"].dt.floor("h")).all():
        raise ValueError("Price times must be exact UTC hour starts")
    remaining = d["date"] + HOUR - d["market_close_at"]
    if not (d["period_start"].eq(d["date"]) &
            d["period_end_exclusive"].eq(d["date"] + HOUR) &
            (remaining > pd.Timedelta(0)) & (remaining <= pd.Timedelta(milliseconds=1))).all():
        raise ValueError("Hourly price period or market close timestamp is misaligned")
    calendar = pd.date_range(config["data_start"], config["output_end_exclusive"], freq="h", inclusive="left", name="date")
    d = d.set_index("date").sort_index().reindex(calendar)
    d["asset"], d["symbol"] = config["asset"], config["symbol"]
    d["period_start"], d["period_end_exclusive"] = d.index, d.index + HOUR
    if (d["market_close_at"].dropna() >= pd.Timestamp.now(tz="UTC")).any():
        raise ValueError("Incomplete hourly candle")
    for col in ("open", "high", "low", "close"):
        if (d[col].dropna() <= 0).any():
            raise ValueError(f"Invalid price: {col}")
    if ((d["high"] < d[["open", "close", "low"]].max(axis=1)) |
        (d["low"] > d[["open", "close", "high"]].min(axis=1))).any():
        raise ValueError("OHLC bounds invalid")
    for col in ("volume_eth", "quote_volume_usdt", "taker_buy_volume_eth", "taker_buy_quote_usdt"):
        if (d[col].dropna() < 0).any():
            raise ValueError(f"Negative volume: {col}")
    if ((d["taker_buy_quote_usdt"] > d["quote_volume_usdt"] + 1e-6) |
        (d["taker_buy_volume_eth"] > d["volume_eth"] + 1e-9)).any():
        raise ValueError("Taker-buy volume exceeds total")
    d["price_complete"] = d[["open", "high", "low", "close", "quote_volume_usdt", "taker_buy_quote_usdt"]].notna().all(axis=1)
    d["taker_sell_quote_usdt"] = d["quote_volume_usdt"] - d["taker_buy_quote_usdt"]
    spread = (d["high"] - d["low"]).where(lambda s: s > 0)
    d["clv"] = (2 * d["close"] - d["high"] - d["low"]) / spread
    d["return_1h"] = d["close"] / d["close"].shift(1) - 1
    for level in config["support_levels_usdt"]:
        d[f"distance_{level}"] = d["close"] / level - 1
        d[f"proximity_{level}"] = np.exp(-d[f"distance_{level}"].abs() / config["support_band_fraction"])
        # A descriptive event flag, not a declaration of support. Missing stays missing.
        d[f"close_within_2pct_{level}"] = (d[f"distance_{level}"].abs() <= config["support_band_fraction"]).where(d["close"].notna())
    return d


def attach_chain(d: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Join actual hourly inputs by period, preserving absence and provenance.

    market_cap_usd is the end-of-hour stock; denominators use the previous hour.
    available_at columns describe source timing, not proof of historical vintages.
    """
    d = d.copy()
    fields = ["flow_in_usd", "flow_out_usd", "market_cap_usd"]
    timestamps = ["flow_available_at", "cap_available_at", "flow_version_at", "cap_version_at"]
    for col in fields:
        d[col] = np.nan
    for col in timestamps:
        d[col] = pd.NaT
    d["chain_source_url"] = ""
    if config.get("hourly_chain_csv"):
        path = Path(config["hourly_chain_csv"])
        if not path.is_absolute():
            path = ROOT / path
        inp = pd.read_csv(path)
        required = ["date", "asset", *fields, "flow_available_at", "cap_available_at", "source_url"]
        if set(required) - set(inp.columns):
            raise ValueError(f"Hourly chain CSV missing columns: {set(required) - set(inp.columns)}")
        inp["date"] = pd.to_datetime(inp["date"], utc=True)
        if inp.duplicated(["date", "asset"]).any():
            raise ValueError("Duplicate hourly chain key")
        if not inp["date"].eq(inp["date"].dt.floor("h")).all():
            raise ValueError("Chain time must label an exact UTC hour start")
        inp = inp.loc[inp["asset"] == config["asset"]].set_index("date").sort_index()
        if inp.empty:
            raise ValueError("Hourly chain CSV has no configured asset")
        for col in fields:
            inp[col] = pd.to_numeric(inp[col], errors="raise")
            if np.isinf(inp[col]).any() or (inp[col].dropna() < 0).any():
                raise ValueError(f"Invalid hourly chain input: {col}")
            d[col] = inp[col].reindex(d.index)
        if (inp["market_cap_usd"].dropna() <= 0).any():
            raise ValueError("Market cap must be positive or missing")
        for col in timestamps:
            if col in inp:
                inp[col] = pd.to_datetime(inp[col], utc=True)
                d[col] = inp[col].reindex(d.index)
        for col in ("flow_available_at", "cap_available_at"):
            if (inp[col] < inp.index + HOUR).any():
                raise ValueError("An hour's complete input cannot be available before that hour ends")
        if inp["source_url"].fillna("").str.strip().eq("").any():
            raise ValueError("Hourly chain rows require a source reference")
        d["chain_source_url"] = inp["source_url"].reindex(d.index).fillna("")
    d["market_cap_previous_hour_usd"] = d["market_cap_usd"].shift(1)
    d["market_cap_previous_hour_available_at"] = pd.to_datetime(d["cap_available_at"], utc=True).shift(1)
    d["market_cap_previous_hour_version_at"] = pd.to_datetime(d["cap_version_at"], utc=True).shift(1)
    d["chain_inputs_complete"] = d[["flow_in_usd", "flow_out_usd", "market_cap_previous_hour_usd"]].notna().all(axis=1)
    timing = pd.DataFrame({"hour_end": d["period_end_exclusive"],
                           "flow_publication": pd.to_datetime(d["flow_available_at"], utc=True),
                           "cap_publication": d["market_cap_previous_hour_available_at"],
                           "flow_version": pd.to_datetime(d["flow_version_at"], utc=True),
                           "cap_version": d["market_cap_previous_hour_version_at"]}, index=d.index)
    d["chain_known_input_time_lower_bound"] = timing.max(axis=1).where(d["chain_inputs_complete"])
    d["chain_input_timing_complete"] = timing.notna().all(axis=1) & d["chain_inputs_complete"]
    # Current files are descriptive snapshots. A timestamp alone is not historical PIT evidence.
    d["strict_pit_eligible"] = False
    return d


def chain_factors(d: pd.DataFrame, config: dict) -> tuple[pd.DataFrame, list[dict]]:
    inflow, outflow = d["flow_in_usd"], d["flow_out_usd"]
    cap = d["market_cap_previous_hour_usd"].where(lambda x: x > 0)
    total, q = inflow + outflow, (outflow - inflow) / cap
    short, long = config["short_window_hours"], config["long_window_hours"]
    f = pd.DataFrame(index=d.index)
    f["net_outflow_cap"] = q
    f["flow_imbalance"] = (outflow - inflow) / total.where(total > 0)
    f["outflow_momentum_72_336h"] = q.rolling(short, min_periods=short).mean() - q.rolling(long, min_periods=long).mean()
    f["gross_flow_cap"] = total / cap
    entries = [
        ("net_outflow_cap", "小时净流出市值比", "(O_h-I_h)/M_(h-1)", "真实小时链上流入、流出；前小时市值"),
        ("flow_imbalance", "小时流出流入失衡", "(O_h-I_h)/(O_h+I_h)", "真实小时链上流入、流出"),
        ("outflow_momentum_72_336h", "净流出动量72/336小时", f"mean(q,{short}h)-mean(q,{long}h)", "真实小时净流出市值比连续历史"),
        ("gross_flow_cap", "小时交易所流转强度", "(O_h+I_h)/M_(h-1)", "真实小时链上流入、流出；前小时市值")]
    for level in config["support_levels_usdt"]:
        f[f"support_outflow_{level}"] = d[f"proximity_{level}"] * q
        f[f"support_resilience_{level}"] = d[f"proximity_{level}"] * d["clv"] * inflow / cap
        entries += [
            (f"support_outflow_{level}", f"{level}价位加权净流出", f"P_h({level})*q_h", "真实小时链上流量、前小时市值、小时收盘"),
            (f"support_resilience_{level}", f"{level}价位加权承接代理", f"P_h({level})*CLV_h*I_h/M_(h-1)", "真实小时链上流入、前小时市值、小时高低收")]
    return f, [dict(factor=a, name_zh=b, formula=c, inputs=e, family="chain") for a,b,c,e in entries]


def market_factors(d: pd.DataFrame, config: dict) -> tuple[pd.DataFrame, list[dict]]:
    """Distinct optional family. Buyer/seller aggressor is not chain in/outflow."""
    b, s, v = d["taker_buy_quote_usdt"], d["taker_sell_quote_usdt"], d["quote_volume_usdt"]
    lookback = config["market_normalizer_hours"]
    prior_total = v.shift(1).rolling(lookback, min_periods=lookback).sum().where(lambda x: x > 0)
    q = (b - s) / prior_total
    short, long = config["short_window_hours"], config["long_window_hours"]
    f = pd.DataFrame(index=d.index)
    f["taker_net_buy_intensity"] = q
    f["taker_buy_sell_imbalance"] = (b - s) / v.where(v > 0)
    f["taker_momentum_72_336h"] = q.rolling(short, min_periods=short).mean() - q.rolling(long, min_periods=long).mean()
    f["trade_activity"] = v / (prior_total / lookback)
    entries = [
        ("taker_net_buy_intensity", "主动净买入强度", "(B_h-S_h)/sum(V_[h-24,h-1])", "小时主动买入、主动卖出成交额；此前24小时总成交额"),
        ("taker_buy_sell_imbalance", "主动买卖失衡", "(B_h-S_h)/V_h", "小时主动买入、主动卖出、总成交额"),
        ("taker_momentum_72_336h", "主动净买入动量72/336小时", f"mean(q,{short}h)-mean(q,{long}h)", "小时主动净买入强度连续历史"),
        ("trade_activity", "小时成交活跃度", "V_h/mean(V_[h-24,h-1])", "小时成交额；此前24小时平均成交额")]
    for level in config["support_levels_usdt"]:
        f[f"level_net_buy_{level}"] = d[f"proximity_{level}"] * q
        f[f"level_absorption_{level}"] = d[f"proximity_{level}"] * d["clv"] * s / prior_total
        entries += [
            (f"level_net_buy_{level}", f"{level}价位加权主动净买入", f"P_h({level})*q_h", "小时收盘、主动买卖成交额、此前24小时成交额"),
            (f"level_absorption_{level}", f"{level}价位加权卖压承接代理", f"P_h({level})*CLV_h*S_h/sum(V_[h-24,h-1])", "小时高低收、主动卖出成交额、此前24小时成交额")]
    return f, [dict(factor=a, name_zh=b, formula=c, inputs=e, family="market") for a,b,c,e in entries]


def process(raw: pd.DataFrame, config: dict, family: str) -> pd.DataFrame:
    rows = []
    for factor in raw:
        r = temporal_preprocess(raw[factor], window=config["preprocess_window_hours"],
                                min_periods=config["preprocess_min_observations"],
                                k=config["mad_multiplier"], frequency="h")
        r["raw"], r["factor"], r["asset"], r["family"] = raw[factor], factor, config["asset"], family
        r["clipped"] = r["raw"].notna() & r["winsorized"].notna() & r["raw"].ne(r["winsorized"])
        r["strict_pit_eligible"] = False
        r["reason"] = r["status"].map({"ok": "", "missing_current": "本小时所需输入缺失", "insufficient_history": "过去历史不足", "zero_mad": "历史MAD为0", "zero_std": "历史标准差为0"})
        rows.append(r.reset_index())
    return pd.concat(rows, ignore_index=True)


def run(config: dict) -> dict:
    if config["timezone"] != "UTC" or config["frequency"] != "h" or config["neutralize"]:
        raise ValueError("This pipeline uses UTC hourly ETH time-series preprocessing without neutralization")
    if config["short_window_hours"] >= config["long_window_hours"] or config["support_band_fraction"] <= 0:
        raise ValueError("Invalid factor windows/band")
    OUT.mkdir(parents=True, exist_ok=True)
    aligned = attach_chain(load_prices(config), config)
    raw, catalog = chain_factors(aligned, config)
    results = [process(raw, config, "chain")]
    if config.get("include_market_factors"):
        market_raw, market_catalog = market_factors(aligned, config)
        catalog += market_catalog
        results.append(process(market_raw, config, "market"))
    factors = pd.concat(results, ignore_index=True)
    start, end = pd.Timestamp(config["output_start"]), pd.Timestamp(config["output_end_exclusive"])
    factors = factors.loc[(factors["date"] >= start) & (factors["date"] < end)].copy()
    selected = aligned.loc[(aligned.index >= start) & (aligned.index < end)].copy()
    aligned.to_csv(OUT / "aligned_hourly_with_warmup.csv")
    selected.to_csv(OUT / "aligned_hourly.csv")
    factors.to_csv(OUT / "factors_long.csv", index=False)
    for (family, factor), g in factors.groupby(["family", "factor"]):
        for stage in ("raw", "winsorized", "zscore"):
            folder = OUT / "matrices" / family / stage
            folder.mkdir(parents=True, exist_ok=True)
            g.pivot(index="date", columns="asset", values=stage).to_csv(folder / f"{factor}.csv")
    summary = []
    for (family, factor), g in factors.groupby(["family", "factor"]):
        valid = int(g["zscore"].notna().sum())
        summary.append(dict(family=family, factor=factor, rows=len(g), raw_non_missing=int(g["raw"].notna().sum()),
                            zscore_non_missing=valid, clipped_rows=int(g["clipped"].sum()),
                            clipped_fraction=float(g["clipped"].sum()/valid) if valid else None,
                            status_counts={str(k):int(v) for k,v in g["status"].value_counts().items()}))
    # Only raw/price comparisons are meaningful; do not sum standardized factors.
    example_day = selected.loc["2026-08-27"]
    example = dict(date="2026-08-27", daily_high=float(example_day["high"].max()),
                   daily_low=float(example_day["low"].min()), daily_close=float(example_day["close"].iloc[-1]),
                   positive_hourly_clv=int((example_day["clv"] > 0).sum()), negative_hourly_clv=int((example_day["clv"] < 0).sum()))
    example["daily_clv"] = (2*example["daily_close"]-example["daily_high"]-example["daily_low"])/(example["daily_high"]-example["daily_low"])
    manifest = dict(config=config, generated_at=pd.Timestamp.now(tz="UTC").isoformat(),
                    output_hours=len(selected), output_days=len(selected)/24, warmup_and_output_hours=len(aligned),
                    price_complete_hours=int(selected["price_complete"].sum()),
                    chain_complete_hours=int(selected["chain_inputs_complete"].sum()), strict_pit_available=False,
                    summary=summary, example=example,
                    hourly_price_manifest="data/hourly/hourly_prices_manifest.json",
                    daily_reconciliation=json.loads((ROOT/"data/hourly/reconciliation.json").read_text()),
                    limitations=["小时链上流量来自 Santiment Public GraphQL，流量原始单位为 ETH，按同小时 Santiment USD 价格换算为 USD；不使用日流量拆分。",
                                 "Santiment 小时流量在正式区间只有 2244/2904 个完整小时，其余小时保留缺失。",
                                 "当前快照与供应商版本时间不能自动证明历史PIT可交易性。",
                                 "价位权重为对称连续权重，不证明触及或守住支撑。MAD截尾可能压缩这些因子的差异。",
                                 "小时成交量价组若启用，是另一套定义。主动买卖成交额不是交易所地址流入流出。"])
    save_json(OUT/"run_manifest.json", manifest)
    save_json(OUT/"factor_catalog.json", catalog)
    header = "date,asset,flow_in_usd,flow_out_usd,market_cap_usd,flow_available_at,cap_available_at,flow_version_at,cap_version_at,source_url\n"
    template = ROOT / "data/hourly_chain_input_template.csv"
    if not template.exists():
        template.write_text(header, encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT/"hourly_config.json")
    args = parser.parse_args()
    report = run(json.loads(args.config.read_text()))
    print(json.dumps({k:report[k] for k in ("output_hours", "output_days", "price_complete_hours", "chain_complete_hours", "summary", "example")}, ensure_ascii=False, indent=2))
