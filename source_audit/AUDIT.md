# ETH 小时因子数据可得性调查

调查时间：2026-11-01（UTC），所有请求不带 API key 或认证头。这里只读调查，不改主代码、不生成虚构小时流量。原始响应与哈希见 `manifest.json`、`extra_manifest.json`。

## 结论

**当前已验证的公开数据不足以计算原有八个链上流量因子的真实小时值。**原有所有八个因子均依赖 ETH 交易所流入/流出中的至少一项；这些字段在当前可访问 Coin Metrics 目录中只列日频。日数据不能拆成 24 份，也不能按小时成交量分配后宣称真实交易所存取流量。

小时行情可独立取到，但成交量、主动买卖量与交易所地址存取流量不是同一经济对象。如果另外生成基于行情的小时因子，必须明确改名、说明代理含义，不能冒充原八因子改频成功。

## 直接 API 验证

| 来源/字段 | 请求与结果 | 可得性结论 |
|---|---|---|
| Coin Metrics `FlowInExUSD` | `/v4/timeseries/asset-metrics?assets=eth&metrics=FlowInExUSD,FlowOutExUSD&frequency=1h...` → 403 | 当前 community 端点不能返回小时流入 |
| Coin Metrics `FlowOutExUSD` | 单独请求同字段 `frequency=1h` → 403 | 当前 community 端点不能返回小时流出 |
| Coin Metrics `CapMrktCurUSD` | `frequency=1h` → 400，所有请求字段不支持 ETH/1h | 该字段当前目录不支持小时 |
| Coin Metrics `PriceUSD` | `frequency=1h` → 403 | 此字段在本凭证条件下不可取，行情可改用 Binance 真实 1h K 线 |
| Coin Metrics PIT flow | `FlowInExPitUSD,FlowOutExPitUSD`、1h → 403 | 没有获得历史当时可见版本 |
| CryptoQuant ETH exchange inflow | `/v1/eth/exchange-flows/inflow?exchange=all_exchange&window=hour&from=20260701&to=20260702&limit=24` → 401、明确缺 API token | 无凭证不能下载；尚未验证具体套餐及该口径的历史小时覆盖 |
| Glassnode ETH transfer volume to exchanges | `/v1/metrics/transactions/transfers_volume_to_exchanges_sum?a=ETH&i=1h...&c=USD` → 401 | 无凭证不能下载；尚未验证套餐、历史小时覆盖、去交易所互转口径 |

Coin Metrics 403 只说明该请求不能在当前条件下使用。目录进一步显示其三个原字段仅列 1d，因此**不可声称“购买 Coin Metrics 就一定能解锁原字段 1h”**；需先取得供应商确认。CryptoQuant 文档旧地址返回 404；Glassnode文档访问遭403；没有将这些错误页面当成数据证据或绕过访问控制。

## 覆盖范围与样本边界

原 `config.json` 的正式样本不是截图显示的 19 天，而是 **2026-07-01 至 2026-10-29，共 121 天**。现有缓存还含 2026-04-15 起的预热，共 **198 个完整日记录**；三项核心字段每天都有值。

`catalog-v2/asset-metrics?assets=eth&metrics=FlowInExUSD,FlowOutExUSD,CapMrktCurUSD` 的三个字段均显示：

- frequency: `1d`
- min_time: `2015-08-08T00:00:00Z`
- max_time: `2026-10-30T00:00:00Z`
- community: `true`

以上可以证实日频目录覆盖用户已有样本和预热区间；**不能证实小时记录存在**。在保留原正式样本的前提下，小时日历有 `121 × 24 = 2904` 行；198天完整输入日历有4752小时。完整日历的存在不等于每个小时有链上数据，缺失应保持为空。

## 时间对齐及发布时点

官方 FAQ 对 UpperCamelCase asset metrics 使用 beginning-of-interval：`2026-07-01T00:00Z` 的日流量统计整个 UTC 7月1日，而不是 00:00 时已经知道的值。小时聚合若取到，也应核实 `09:00` 是否代表 `[09:00,10:00)`，完整小时结束前不能用其最终 H/L/C 和累计流量。

有四个不同时间，必须分列：

1. 统计区间起点。
2. 统计区间终点。
3. 数据供应商发布/版本时间（如果可查）。
4. 策略决策/可用时间。

原缓存 `FlowInExUSD-status-time` / `FlowOutExUSD-status-time` 有事后版本时间，例如2026-04-15记录的当前版本日期是2026-09-17。这验证原数据是 latest-revised 研究口径，不是严格历史当时可知数据。`AssetEODCompletionTime` 是日末计算完成信息，不能代替所有未来修订的历史可用时间，更不能作为小时流量可用时点。原配置24小时发布时间延迟是研究假设，不是供应商首发历史证据。

小时市值可考虑用“已知的最近一次日市值”作为明确的混频分母，但须按真实可用时间 as-of 对齐，不能在当天00:00就用当天结束才形成的日市值。本轮没有取得完整市值历史首发时点，因此不能将此路径认证为严格可交易回测。若采用上一小时价格×滞后已知供给估计小时市值，必须命名为估计值，不能冒充 `CapMrktCurUSD(1h)`。

## 可行路径

- 立即可交付：完整真实1h OHLCV、小时区间与可用性列、原8因子的小时计算框架、缺失原因、空值而非伪值、原样本范围及预热说明。
- 原8因子的真实值：用户已有合规小时流量导出或有权限供应商 API。接入前必须确认 ETH/全交易所范围、排除交易所互转规则、ETH/USD计价、小时边界、修订规则及发布时点。不同供应商不可无说明拼接成同一序列。
- 如用户接受改变指标：真实小时成交额、主动买卖失衡、价格承接等行情因子，可以当作另一组因子独立完成；它们不等于链上流入流出。
- 不推荐此轮临时自建链上标签：裸Ethereum交易可公开获取，但要可靠识别全交易所地址、内部转账、合约内部ETH转移及标签版本需要额外数据工程，不能用几条已知地址覆盖冒充全市场流量。

## 官方资料

- https://community-api.coinmetrics.io/v4/catalog-v2/asset-metrics?assets=eth&metrics=FlowInExUSD%2CFlowOutExUSD%2CCapMrktCurUSD
- https://gitbook-docs.coinmetrics.io/service-and-support/faqs
- https://gitbook-docs.coinmetrics.io/network-data/network-data-overview/exchange/deposits
- https://gitbook-docs.coinmetrics.io/network-data/network-data-overview/exchange/point-in-time-flows
- https://api.cryptoquant.com/v1/eth/exchange-flows/inflow
- https://api.glassnode.com/v1/metrics/transactions/transfers_volume_to_exchanges_sum
