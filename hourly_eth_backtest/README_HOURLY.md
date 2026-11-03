# ETH 小时版运行说明

小时行情与原八个链上因子的计算框架已经完成。正式样本保持 **2026-07-01 00:00 UTC 至 2026-10-30 00:00 UTC（不含终点）**，即 121 天、2,904 小时。预热从 4 月 15 日开始，共 4,752 小时行情，已与原 198 天日行情对账一致。

**当前小时链上输入来自 Santiment Public GraphQL。** `exchange_inflow` 和 `exchange_outflow` 原始单位为 ETH，项目按同小时 `price_usd` 换算为 USD，并使用同小时 `marketcap_usd`。正式区间有 2,244/2,904 个完整小时；缺失的 660 小时保留空值，不使用日流量拆分或填零。默认 `include_market_factors=false`，不把主动买卖成交额替代为交易所地址流入流出。

## 运行

Python 需安装 `numpy`、`pandas`；下载器其余依赖为标准库。在项目目录运行：

```bash
python3 hourly_prices.py --help
python3 hourly_prices.py
python3 hourly_pipeline.py --config hourly_config.json
python3 hourly_backtest.py
```

`hourly_prices.py` 默认复用并校验已有源响应缓存；`--refresh` 重新请求 Binance，另存一次源快照。价格下载器当前固定本次日期区间，若改样本范围，需同时修改下载器参数常量及配置，不应只改输出日期。完整 UTC 日的聚合检查只基于小时行情自身，不依赖日频数据集。

小时处理结果写入 `output_hourly/`，行情和小时链上输入写入 `data/hourly/`。

## 当前配置

| 参数 | 值 | 含义 |
|---|---:|---|
| `short_window_hours` | 72 | 保留原 3 天观察时长 |
| `long_window_hours` | 336 | 保留原 14 天观察时长 |
| `preprocess_window_hours` | 1440 | 过去 60 天，不含本小时 |
| `preprocess_min_observations` | 720 | 历史窗口至少 30 天数量的有效小时值 |
| `mad_multiplier` | 3.0 | 边界为中位数 ± 3 × 1.4826 × MAD |
| `support_band_fraction` | 0.02 | 距 2300／2400 参考价的连续衰减尺度 |
| `neutralize` | false | 单币时间序列不做横截面中性化 |

3／14 小时是一组不同的短线定义，不能在说明不变的情况下替换 72／336。历史缺失保留在完整 UTC 小时日历中，不向更早历史补凑，也不当作 0。

## 小时链上输入来源

模板：`data/hourly_chain_input_template.csv`。字段如下：

| 字段 | 约定 |
|---|---|
| `date` | UTC 整点小时起点，例如 `2026-07-01T09:00:00Z` 对应 `[09:00,10:00)` |
| `asset` | 本次填写 `ETH` |
| `flow_in_usd` | 同一小时真实交易所链上流入美元价值；不得从日流量拆分 |
| `flow_out_usd` | 同一小时真实交易所链上流出美元价值 |
| `market_cap_usd` | 本行小时结束时的正市值；框架自动滞后一小时作分母 |
| `flow_available_at` | 完整小时流量可用时间，UTC；不得早于小时结束 |
| `cap_available_at` | 本行小时末市值可用时间，UTC；不得早于小时结束 |
| `flow_version_at` | 使用的流量版本时间，UTC；没有证据就保留缺失 |
| `cap_version_at` | 使用的市值版本时间，UTC；没有证据就保留缺失 |
| `source_url` | 必填来源或可审计引用 |

当前已接入 `data/hourly/santiment_hourly_chain.csv`，清单为 `data/hourly/santiment_hourly_chain_manifest.json`。原始响应和覆盖审计保存在 `outputs/eth_hourly_complete_20261101/support/chain_followup_raw/`。输入中的 `flow_in_usd` / `flow_out_usd` 是 Santiment ETH 流量乘同小时 USD 价格得到的派生 USD 值；不是 Coin Metrics 日频字段，也不是 Binance 成交额。

若替换为其他供应商，保存真实导出并将配置中的 `hourly_chain_csv` 改为该路径后运行。相对路径按 `eth_factor_project/` 解析。需要包含正式样本以前的预热，以及第一小时分母所需的前小时市值；代码不会通过填零、日流量复制、插值来补足。

来源需要确认交易所范围、交易所互转排除规则、标签修订规则、美元折算方式和小时边界。不同来源的定义不一致时，不能直接拼接。时间戳本身不证明历史当时可见，当前框架仍保留 `strict_pit_eligible=false`。

## 输出与检查

- `aligned_hourly.csv`／`aligned_hourly_with_warmup.csv`：价格、链上输入、CLV、价位距离、数据完整性和已知可用时间下界。
- `factors_long.csv`：原始因子、去极值值、z-score、MAD、裁剪边界、`clipped`、`status`、缺失原因。
- `event_forward_returns.csv`：从下一小时开盘入场的 1/3/6/12/24/72 小时事件收益统计。
- `hourly_strategy_returns.csv`：逐小时 1 小时信号收益，含 2 bps 换手成本。
- `backtest_summary.csv`：描述性快照回测与严格 PIT 筛选汇总。
- `hourly_backtest_manifest.json`：回测对齐规则、输入哈希和可用性限制。
- `matrices/chain/{raw,winsorized,zscore}/`：每个原因子各一张“小时 × 资产”矩阵。
- `run_manifest.json`：数据数量、各因子的缺失状态、裁剪情况和小时行情的 UTC 日聚合检查。
- `factor_catalog.json`：公式和所需输入。
- `data/hourly/hourly_prices_manifest.json`：源请求、下载时间、文件哈希。
- `data/hourly/reconciliation.json`：198 天小时汇总与原日行情的对账。

当前预期：`output_hours=2904`、`price_complete_hours=2904`、`chain_complete_hours=0`；八因子原始值和 z-score 均没有有效值。得到空表符合真实数据状态，不代表程序应改成填 0。

后四项有历史 MAD 接近零而导致裁剪饱和的风险。接入真实输入后需检查诊断；改成小时频率不会自动修复这一问题。不要相加 24 个小时的比值、CLV 或 z-score 充当日频因子，也不要把 24 倍行数视为 24 倍独立样本。

完整中文解释见工作区 `outputs/eth_hourly_20261101/小时版说明与日频区别.md`；数据可得性证据见同目录 `support/data_audit/AUDIT.md`。
