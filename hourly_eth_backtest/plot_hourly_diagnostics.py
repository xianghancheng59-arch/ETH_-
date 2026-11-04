"""Create the three core hourly factor diagnostics.

Outputs are deliberately limited to: IC, quantile-group returns, and
monotonicity. Quantile groups use only the previous 1,440 hours of each
factor, so the grouping step does not inspect future factor values.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output_hourly"
PLOTS = OUT / "plots"
HORIZONS = [1, 3, 6, 12, 24, 72]
HIST_WINDOW = 1440
MIN_HISTORY = 720

FACTOR_ORDER = [
    "flow_imbalance", "gross_flow_cap", "net_outflow_cap",
    "outflow_momentum_72_336h", "support_outflow_2300",
    "support_outflow_2400", "support_resilience_2300",
    "support_resilience_2400",
]


def future_returns(aligned: pd.DataFrame) -> pd.DataFrame:
    d = aligned.sort_values("date").copy()
    d["entry_open"] = d["open"].shift(-1)
    for h in HORIZONS:
        d[f"fwd_{h}h"] = d["close"].shift(-h) / d["entry_open"] - 1.0
    return d[["date"] + [f"fwd_{h}h" for h in HORIZONS]]


def historical_percentile(s: pd.Series) -> pd.Series:
    """Current value's percentile among prior values only."""
    values = s.to_numpy(dtype=float)
    result = np.full(len(values), np.nan)
    for i, value in enumerate(values):
        if not np.isfinite(value):
            continue
        start = max(0, i - HIST_WINDOW)
        history = values[start:i]
        history = history[np.isfinite(history)]
        if len(history) >= MIN_HISTORY:
            result[i] = np.mean(history < value)
    return pd.Series(result, index=s.index)


def load_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    aligned = pd.read_csv(OUT / "aligned_hourly.csv", parse_dates=["date"])
    factors = pd.read_csv(OUT / "factors_long.csv", parse_dates=["date"])
    aligned["date"] = pd.to_datetime(aligned["date"], utc=True)
    factors["date"] = pd.to_datetime(factors["date"], utc=True)
    labels = future_returns(aligned)
    wide = factors.pivot_table(index="date", columns="factor", values="zscore", aggfunc="first")
    wide = wide.reindex(columns=FACTOR_ORDER)
    wide = wide.join(labels.set_index("date"), how="left")
    return aligned, wide


def compute_diagnostics(wide: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    ic_rows, group_rows, mono_rows = [], [], []
    for factor in FACTOR_ORDER:
        s = wide[factor]
        for h in HORIZONS:
            r = wide[f"fwd_{h}h"]
            valid = pd.concat([s, r], axis=1).dropna()
            ic = valid.iloc[:, 0].corr(valid.iloc[:, 1], method="spearman") if len(valid) >= 3 else np.nan
            ic_rows.append({"factor": factor, "horizon_hours": h, "ic_spearman": ic, "observations": len(valid)})
        percentile = historical_percentile(s)
        groups = pd.cut(percentile, bins=[-np.inf, .2, .4, .6, .8, np.inf], labels=[1, 2, 3, 4, 5])
        wide[f"group_{factor}"] = groups.astype("float")
        for h in HORIZONS:
            r = wide[f"fwd_{h}h"]
            tmp = pd.DataFrame({"group": groups, "future_return": r}).dropna()
            means = tmp.groupby("group", observed=True)["future_return"].mean()
            counts = tmp.groupby("group", observed=True)["future_return"].size()
            for q in range(1, 6):
                group_rows.append({"factor": factor, "horizon_hours": h, "group": q,
                                   "mean_future_return": means.get(q, np.nan),
                                   "observations": int(counts.get(q, 0))})
            clean = means.reindex(range(1, 6)).dropna()
            mono = clean.index.to_numpy(dtype=float)
            mono_score = np.corrcoef(mono, clean.to_numpy(dtype=float))[0, 1] if len(clean) >= 3 else np.nan
            mono_rows.append({"factor": factor, "horizon_hours": h,
                              "monotonicity_spearman": mono_score,
                              "high_low_spread": (clean.iloc[-1] - clean.iloc[0]) if len(clean) >= 2 else np.nan,
                              "groups_present": len(clean)})
    return pd.DataFrame(ic_rows), pd.DataFrame(group_rows), pd.DataFrame(mono_rows)


def factor_label(name: str) -> str:
    return name.replace("_", "\n")


def plot_ic(ic: pd.DataFrame) -> None:
    table = ic.pivot(index="factor", columns="horizon_hours", values="ic_spearman").reindex(FACTOR_ORDER)
    fig, ax = plt.subplots(figsize=(11, 6.5))
    image = ax.imshow(table.to_numpy(dtype=float), aspect="auto", cmap="RdBu_r", vmin=-np.nanmax(abs(table.to_numpy())), vmax=np.nanmax(abs(table.to_numpy())))
    fig.colorbar(image, ax=ax, label="Spearman IC")
    for i in range(table.shape[0]):
        for j in range(table.shape[1]):
            ax.text(j, i, f"{table.iloc[i, j]:.3f}", ha="center", va="center", fontsize=9)
    ax.set_xticks(range(len(table.columns)), table.columns)
    ax.set_yticks(range(len(table.index)), [factor_label(x) for x in FACTOR_ORDER])
    ax.set_title("ETH 1H Factor IC vs Future Return")
    ax.set_xlabel("Future return horizon (hours)")
    ax.set_ylabel("Factor")
    ax.tick_params(axis="y", labelrotation=0)
    fig.tight_layout()
    fig.savefig(PLOTS / "01_ic_heatmap.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def plot_group_returns(groups: pd.DataFrame) -> None:
    table = groups[groups.horizon_hours.eq(1)].pivot(index="factor", columns="group", values="mean_future_return").reindex(FACTOR_ORDER)
    fig, ax = plt.subplots(figsize=(10.5, 6.5))
    values = table.to_numpy(dtype=float) * 100
    scale = np.nanmax(abs(values))
    image = ax.imshow(values, aspect="auto", cmap="RdBu_r", vmin=-scale, vmax=scale)
    fig.colorbar(image, ax=ax, label="Mean next-hour return (%)")
    for i in range(table.shape[0]):
        for j in range(table.shape[1]):
            ax.text(j, i, f"{values[i, j]:.3f}", ha="center", va="center", fontsize=9)
    ax.set_xticks(range(len(table.columns)), table.columns)
    ax.set_yticks(range(len(table.index)), [factor_label(x) for x in FACTOR_ORDER])
    ax.set_title("ETH 1H Quantile-Group Returns (z-score, 1H horizon)")
    ax.set_xlabel("Historical factor group: 1 = Low, 5 = High")
    ax.set_ylabel("Factor")
    ax.tick_params(axis="y", labelrotation=0)
    fig.tight_layout()
    fig.savefig(PLOTS / "02_group_returns_heatmap.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def plot_monotonicity(groups: pd.DataFrame) -> None:
    fig, axes = plt.subplots(2, 4, figsize=(15, 7), sharex=True, sharey=True)
    for ax, factor in zip(axes.flat, FACTOR_ORDER):
        x = groups[(groups.factor == factor) & (groups.horizon_hours == 1)].sort_values("group")
        ax.plot(x["group"], x["mean_future_return"] * 100, marker="o", color="#2563eb", linewidth=1.8)
        ax.axhline(0, color="#6b7280", linewidth=.8)
        ax.set_title(factor_label(factor), fontsize=9)
        ax.set_xticks([1, 2, 3, 4, 5])
        ax.grid(alpha=.2)
    fig.suptitle("ETH 1H Monotonicity by Historical Factor Group (1H horizon)", y=1.02)
    fig.supxlabel("Historical factor group: Low → High")
    fig.supylabel("Mean next-hour return (%)")
    fig.tight_layout()
    fig.savefig(PLOTS / "03_monotonicity_small_multiples.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    PLOTS.mkdir(parents=True, exist_ok=True)
    _, wide = load_data()
    ic, groups, mono = compute_diagnostics(wide)
    ic.to_csv(PLOTS / "ic.csv", index=False, float_format="%.12g")
    groups.to_csv(PLOTS / "group_returns.csv", index=False, float_format="%.12g")
    mono.to_csv(PLOTS / "monotonicity.csv", index=False, float_format="%.12g")
    plot_ic(ic)
    plot_group_returns(groups)
    plot_monotonicity(groups)
    print(f"wrote {PLOTS}")


if __name__ == "__main__":
    main()
