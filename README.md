# ETH 小时链上流量因子回测

本仓库只保留小时频率的因子与回测产物。

- 频率：1h，UTC
- 资产：ETH
- 行情：Binance ETHUSDT 小时 K 线
- 链上流量：Santiment Public GraphQL 的 `exchange_inflow` / `exchange_outflow`，原始单位 ETH
- USD 转换：同小时 `price_usd` 乘以 ETH 流量
- 市值：同小时 Santiment `marketcap_usd`，因子分母使用前一小时市值
- 正式样本：2026-07-01 00:00 至 2026-10-30 00:00 UTC，不含终点，共 2,904 小时

## 对齐规则

信号使用完整小时 `[t,t+1)` 的数据，在下一小时开盘执行。1 小时标签为 `close[t+1] / open[t+1] - 1`；更长标签仍从下一小时开盘开始，避免使用信号小时收盘作为入场价。小时流量缺失保持为空，不用日频流量拆分、前向填充或零替代。

## 可用性

Santiment 数据在正式区间有 2,244 个完整小时，660 个小时缺失。当前数据是 2026-11 下载的快照，没有历史首发版本证明，因此 `pit_eligible_only` 统计为空；`snapshot` 统计仅用于描述性研究，不能解释为严格历史可交易业绩。

## 运行

```bash
python3 hourly_prices.py
python3 hourly_pipeline.py --config hourly_config.json
python3 hourly_backtest.py
python3 -m unittest test_hourly_pipeline.py test_hourly_preprocessing.py
```

回测输出位于 `output_hourly/`：

- `event_forward_returns.csv`：1/3/6/12/24/72 小时的事件收益统计
- `hourly_strategy_returns.csv`：逐小时 1 小时策略收益，含 2 bps 换手成本
- `backtest_summary.csv`：汇总指标
- `hourly_backtest_manifest.json`：样本、哈希和限制

来源证据位于 `source_audit/`。
