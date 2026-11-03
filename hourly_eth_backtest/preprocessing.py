"""无前视预处理：单币时间序列，以及多资产同日横截面。

缺失值、无穷值、样本不足及退化分布均不会被替换成 0。MAD 使用
median(abs(x - median(x)))，稳健尺度为 1.4826 * MAD；所有标准差
使用总体口径 ddof=0。原始因子应在调用本模块前完成可得时间对齐。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


MAD_SCALE = 1.4826


def _numeric(series: pd.Series, name: str) -> pd.Series:
    if not isinstance(series, pd.Series):
        raise TypeError(f"{name} must be a pandas Series")
    if not series.index.is_unique:
        raise ValueError(f"{name} index must be unique")
    result = pd.to_numeric(series, errors="raise").astype(float)
    return result.where(np.isfinite(result))


def _positive_integer(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _check_k(k: float) -> None:
    if not np.isfinite(k) or k <= 0:
        raise ValueError("k must be finite and positive")


def temporal_preprocess(
    series: pd.Series,
    window: int = 60,
    min_periods: int = 30,
    k: float = 3.0,
    frequency: str = "D",
) -> pd.DataFrame:
    """以先前历史给当前原始值去极值、标准化，返回与输入同索引的表。

    frequency='D' 保留原日频默认；frequency='h' 使用完整 UTC 小时轴。
    小时索引必须是 UTC 整点；无时区索引约定为 UTC，非零 UTC 时差
    的索引需先显式转换。window/min_periods 均按频率对应的根数计量，
    小时版沿用 60 天/30 天时，应显式传入 window=1440,min_periods=720。

    输入需为已补齐日历、严格递增且无重复的 DatetimeIndex。窗口取前
    window 个日历行（不取前 window 个非空观测），再剔除缺失。时点
    t 的 median/MAD 仅来自 [t-window, t-1] 的原始值。当前值和同一
    历史样本使用同一阈值裁剪；mean/std 来自裁剪后的历史，zscore =
    (winsorized - mean) / std，均不使用当前或未来值。缺失小时须显式
    保留为 NaN，本函数不会补值或将缺失视为零。

    status: ok / missing_current / insufficient_history / zero_mad /
    zero_std。异常行的 winsorized 和 zscore 保留 NaN。n_history
    始终记录窗口中有效原始历史的数量；median 等诊断量在可计算时保留。
    """
    _positive_integer(window, "window")
    _positive_integer(min_periods, "min_periods")
    _check_k(k)
    if frequency not in ("D", "h"):
        raise ValueError("frequency must be 'D' or 'h'")
    if min_periods > window:
        raise ValueError("min_periods cannot exceed window")
    raw = _numeric(series, "series")
    if not isinstance(raw.index, pd.DatetimeIndex):
        raise TypeError("series must have a DatetimeIndex")
    if raw.index.hasnans or not raw.index.is_monotonic_increasing:
        raise ValueError("DatetimeIndex must be strictly increasing with no NaT")
    if frequency == "h":
        if raw.index.tz is not None and any(
            timestamp.utcoffset().total_seconds() != 0 for timestamp in raw.index
        ):
            raise ValueError("hourly DatetimeIndex must use UTC; convert to UTC explicitly")
        if not raw.index.equals(raw.index.floor("h")):
            raise ValueError("hourly DatetimeIndex must be aligned to whole UTC hours")
    if len(raw.index) and not raw.index.equals(
        pd.date_range(raw.index[0], raw.index[-1], freq=frequency, name=raw.index.name)
    ):
        calendar = "daily" if frequency == "D" else "hourly UTC"
        raise ValueError(
            f"temporal_preprocess requires a complete {calendar} calendar; retain missing values as NaN"
        )

    numeric_columns = [
        "winsorized", "zscore", "median", "mad", "lower", "upper", "mean", "std"
    ]
    out = pd.DataFrame(np.nan, index=raw.index, columns=numeric_columns)
    out["n_history"] = 0
    out["status"] = pd.Series("insufficient_history", index=raw.index, dtype=object)
    values = raw.to_numpy(dtype=float)
    for position, current in enumerate(values):
        history = values[max(0, position - window):position]
        history = history[np.isfinite(history)]
        date = raw.index[position]
        out.at[date, "n_history"] = len(history)
        if len(history) < min_periods:
            if not np.isfinite(current):
                out.at[date, "status"] = "missing_current"
            continue
        median = float(np.median(history))
        mad = float(np.median(np.abs(history - median)))
        lower = median - k * MAD_SCALE * mad
        upper = median + k * MAD_SCALE * mad
        out.loc[date, ["median", "mad", "lower", "upper"]] = [median, mad, lower, upper]
        if mad == 0:
            out.at[date, "status"] = "missing_current" if not np.isfinite(current) else "zero_mad"
            continue
        clipped_history = np.clip(history, lower, upper)
        mean = float(np.mean(clipped_history))
        std = float(np.std(clipped_history, ddof=0))
        out.loc[date, ["mean", "std"]] = [mean, std]
        if not np.isfinite(current):
            out.at[date, "status"] = "missing_current"
            continue
        if not np.isfinite(std) or std == 0:
            out.at[date, "status"] = "zero_std"
            continue
        winsorized = float(np.clip(current, lower, upper))
        out.at[date, "winsorized"] = winsorized
        out.at[date, "zscore"] = (winsorized - mean) / std
        out.at[date, "status"] = "ok"
    return out


def cross_section_preprocess(
    values: pd.Series,
    market_cap: pd.Series | None = None,
    industry: pd.Series | None = None,
    neutralize: bool = False,
    min_assets: int = 20,
    k: float = 3.0,
) -> pd.DataFrame:
    """对同一决策时点的多资产原始因子做 MAD -> z-score -> 可选中性化。

    全部有限原始值先执行横截面 MAD 裁剪及总体 z-score。中性化在
    暴露完整的样本上以等权 OLS 回归该 z-score，设计矩阵为截距、
    ln(market_cap) 及可选行业 K-1 哑变量，然后把残差再次标准化。
    market_cap 的单位应在资产间一致且必须为正；industry 可省略。
    行业/市值需为该决策时点可得的快照，本函数按资产索引对齐。

    min_assets 同时约束初始有效样本和回归完整样本；回归必须满秩
    且剩余自由度 >= 5。单资产即使 min_assets=1 也会明确返回
    insufficient_assets，不能用于 ETH 单币中性化。失败时保留已经
    成功计算的前置列，neutralized 为 NaN；不会把失败结果置零。

    status 还包括 missing_value / zero_mad / zero_std /
    missing_market_cap / invalid_market_cap / missing_industry /
    insufficient_assets / insufficient_neutralization_assets /
    rank_deficient / insufficient_dof / zero_residual_std / ok。
    """
    _positive_integer(min_assets, "min_assets")
    _check_k(k)
    raw = _numeric(values, "values")
    out = pd.DataFrame(np.nan, index=raw.index, columns=["winsorized", "zscore", "neutralized"])
    out["status"] = pd.Series("missing_value", index=raw.index, dtype=object)
    valid = raw.notna()
    assets = raw.index[valid]
    if len(assets) < max(2, min_assets):
        out.loc[assets, "status"] = "insufficient_assets"
        return out
    sample = raw.loc[assets].to_numpy(dtype=float)
    median = float(np.median(sample))
    mad = float(np.median(np.abs(sample - median)))
    if mad == 0:
        out.loc[assets, "status"] = "zero_mad"
        return out
    clipped = np.clip(sample, median - k * MAD_SCALE * mad, median + k * MAD_SCALE * mad)
    std = float(np.std(clipped, ddof=0))
    if not np.isfinite(std) or std == 0:
        out.loc[assets, "status"] = "zero_std"
        return out
    out.loc[assets, "winsorized"] = clipped
    out.loc[assets, "zscore"] = (clipped - np.mean(clipped)) / std
    out.loc[assets, "status"] = "ok"
    if not neutralize:
        return out
    if market_cap is None:
        out.loc[assets, "status"] = "missing_market_cap"
        return out

    caps = _numeric(market_cap, "market_cap").reindex(raw.index)
    out.loc[assets[caps.loc[assets].isna()], "status"] = "missing_market_cap"
    out.loc[assets[caps.loc[assets].notna() & (caps.loc[assets] <= 0)], "status"] = "invalid_market_cap"
    complete = valid & caps.notna() & (caps > 0)
    if industry is not None:
        if not isinstance(industry, pd.Series):
            raise TypeError("industry must be a pandas Series")
        if not industry.index.is_unique:
            raise ValueError("industry index must be unique")
        sectors = industry.reindex(raw.index)
        out.loc[raw.index[complete & sectors.isna()], "status"] = "missing_industry"
        complete &= sectors.notna()
    regression_assets = raw.index[complete]
    if len(regression_assets) < max(2, min_assets):
        out.loc[regression_assets, "status"] = "insufficient_neutralization_assets"
        return out

    # A constant plus K-1 dummies avoids the ordinary dummy-variable trap.
    exposures = [np.ones(len(regression_assets)), np.log(caps.loc[regression_assets].to_numpy())]
    if industry is not None:
        dummies = pd.get_dummies(sectors.loc[regression_assets].astype(object), drop_first=True, dtype=float)
        exposures.extend(dummies[column].to_numpy() for column in dummies.columns)
    design = np.column_stack(exposures)
    rank = int(np.linalg.matrix_rank(design))
    if rank < design.shape[1]:
        out.loc[regression_assets, "status"] = "rank_deficient"
        return out
    if len(regression_assets) - rank < 5:
        out.loc[regression_assets, "status"] = "insufficient_dof"
        return out
    dependent = out.loc[regression_assets, "zscore"].to_numpy()
    beta = np.linalg.lstsq(design, dependent, rcond=None)[0]
    residual = dependent - design @ beta
    residual_std = float(np.std(residual, ddof=0))
    # An exact fit may leave floating-point noise, which must not be amplified.
    tolerance = np.finfo(float).eps * max(1.0, float(np.linalg.norm(dependent))) * 100
    if not np.isfinite(residual_std) or residual_std <= tolerance:
        out.loc[regression_assets, "status"] = "zero_residual_std"
        return out
    out.loc[regression_assets, "neutralized"] = (residual - np.mean(residual)) / residual_std
    return out
