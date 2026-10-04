# 开发日志 & 数据洞察 (Development Log & Insights)

> 项目：链上巨鲸行为预警系统 · 环境：`whale_alert_project`（Python 3.11）
> 本日志记录每个模块的实现思路、关键决策，以及从 Grafana 看板提炼的 Insights 与 data-driven suggestions。

---

## 一、总体设计目标

1. 用 Etherscan API 获取大额 ETH 转账，阈值与 API Key 从 `.env` 读取。
2. 检测超过 `WHAILIE THRESHOLD_USD` 的巨鲸交易 → 写入 SQLite + 终端预警。
3. Docker Compose 编排 Python 检查器与 Grafana。
4. Grafana provisioning 自动加载 SQLite 数据源 + 基础看板（金额分布 / 流向占比 / 活动时间线）。
5. pytest 覆盖 `detect_whales`。
6. README 含 Mermaid 架构/数据流图 + 运行说明。

---

## 二、模块实现思路与关键决策

### 1. `config.py` —— 配置加载

**思路**：所有下游模块从这里拿配置，方便测试时 mock。

**关键决策**：
- 环境文件是用户手动创建的，文件名带空格 `environment .env`。加载优先级为
  `environment .env` → `.env` → OS 环境变量（Docker 通过 `env_file` 注入即是 OS 环境变量）。
- 提供 `_get_int()` 把字符串强转成 int，避免类型错误。

### 2. `etherscan_client.py` —— Etherscan V2 客户端 + `detect_whales`

**思路**：把「网络 IO」与「纯判断逻辑」分离，纯函数便于单测。

**关键决策（踩坑记录，重要）**：
- **必须用 Etherscan API V2**。实测 V1 端点（`https://api.etherscan.io/api`）已废弃，返回
  `"You are using a deprecated V1 endpoint, switch to Etherscan API V2"`。改用
  `https://api.etherscan.io/v2/api`，并增加 `chainid=1` 参数。
- **`txlistinternal` 是 Pro 专属**，免费 key 会返回 `NOTOK: API Pro endpoint`。
  因此改用免费的 `proxy/eth_getBlockByNumber`，逐个读取区块，抽取带 `value` 的对外 ETH 转账。
- 区块返回的 `value` 是 **hex（如 `0x0`）**，需 `int(value, 16)` 再除以 `1e18` 转成 ETH。
- **免费额度约 5 req/s**。并发回填时大量触发 `rate limit reached`（此时 `result` 是字符串），
  因此在 `_get()` 中增加：对非 dict 的 `result` 跳过 + 遇到 rate limit 自动退避重试。
- `detect_whales(transfers, threshold_usd, eth_price_usd)`：`value_usd = value_eth * price`，
  当 `value_usd >= threshold` 判为巨鲸，并附加 `direction`（交易所流向分类）。纯函数，不修改入参。

**交换所流向分类**：内置一份精简的交易所地址集（Binance / Coinbase / Kraken），
根据 `to` / `from` 判断 `exchange_inflow` / `exchange_outflow` / `peer_to_peer`。

### 3. `database.py` —— SQLite 存储

**思路**：`WhaleDatabase` 类封装 `sqlite3`，建表幂等。

**关键决策**：
- 表 `whale_transfers`，关键字段：`tx_hash / block_number / timestamp / from/to / value_eth / value_usd / direction / created_at`。
- `tx_hash` 建**唯一索引**实现去重，重复插入返回 `False`（防止轮询重复入库）。
- 开启 **WAL 模式**，允许 Python 写入与 Grafana 只读并发访问。
- DB 放在 `./data/whale_alert.db`，与 Grafana 容器用 bind-mount `./data` 共享。

### 4. `whale_alert.py` —— 主入口 / 预警

**思路**：循环轮询（默认 `POLL_INTERVAL_SECONDS`），每次：取价格 → 取最新区块 → 扫最近
`SCAN_BLOCK_WINDOW` 个区块 → `detect_whales` → 写库 → 终端打印预警。

**关键决策**：
- 提供 `--once` 一次性模式（便于测试/定时任务）。
- 巨鲸记录通过 `tx_hash` 去重后才 `insert`；即使重复轮询，DB 也累积不重复。
- 终端预警打印明确的交易方向、发送/接收方与 hash。

### 5. `backfill.py` —— 历史回填工具

**思路**：实时检查器每次只扫很小区间，`$10M` 级别巨鲸相对稀少，看板可能在较长时间内没有数据。
用线程池并发扫描更大历史窗口，把真实巨鲸一次性灌入 DB，快速填充看板。

**关键决策**：并发度 `MAX_WORKERS=3`，配合 `_get()` 的退避重试以尽量不触发免费额度限制；
单位块失败（rate limit）自动跳过，不影响整体。

### 6. Docker / Compose 编排

**思路**：
- `whale-checker`：`python:3.11-slim`，`CMD python whale_alert.py`，密钥/阈值经 `env_file` 注入（不入镜像）。
- `grafana`：`grafana/grafana:11.1.1`，通过 `GF_INSTALL_PLUGINS=frser-sqlite-datasource` 安装社区 SQLite 数据源插件（官方 Grafana 无内置 SQLite 连接器）。
- `./data` 同时挂到 checker（读写）与 grafana（只读 → `/data`），两者读写同一个 `whale_alert.db`。

**关键决策**：Grafana 数据源 `path: /data/whale_alert.db`，previsioning 自动加载，无需手动配置。

### 7. 看板模板（`grafana/dashboards/whale_dashboard.json`）

**思路**：`frser-sqlite-datasource` 通过 `rawSql` 查询 SQLite。为降低对宏的依赖、保证开箱可用，
面板 SQL 使用 `strftime('%s','now','-24 hours')` 显式取“最近 24h”。

三个面板：
1. **24h 巨鲸金额分布**：按小时 `SUM(value_usd)`，把该小时起始时刻转成 `time`（毫秒）作时间轴。
2. **按交易所流向占比**：`COUNT(*)` 按 `direction` 分组，饼图展示 `inflow/outflow/peer`。
3. **巨鲸活动时间线**：明细表（时间 / 发送 / 接收 / ETH / USD / 流向），按时间倒序。

### 8. 测试 `tests/test_whale_alert.py`

**思路**：只测纯函数 `detect_whales`（无网络/DB），快速稳定。
用例：全低于阈值、命中阈值、边界值等于阈值、入参不被修改、方向分类。

---

## 三、Grafana 看板 Insights（基于实际落库数据）

> 数据源：`data/whale_alert.db`。回填窗口约 **2500 个区块（≈8 小时链上时间）**，
> ETH 价格 ~2,682 USD，阈值 $10,000,000。共入库 **5** 笔巨鲸交易。

### 实测明细（真实数据）

| 时间 (本地) | ETH | USD | 方向 | 路径 (from → to) |
| --- | --- | --- | --- | --- |
| 18:22:23 | 15,340 | $41.14M | exchange_outflow | `0x28c6c0…`(Binance) → `0xdfd529…` |
| 11:08:11 | 15,613 | $41.88M | peer_to_peer | `0xb0a270…` → `0x0003b5…` |
| 11:12:59 | 15,613 | $41.88M | peer_to_peer | `0x0003b5…` → `0xa9ac43…` |
| 18:37:59 | 14,565 | $39.06M | peer_to_peer | `0xb0a270…` → `0x0003b5…` |
| 18:40:11 | 14,565 | $39.06M | peer_to_peer | `0x0003b5…` → `0xa9ac43…` |

### 核心发现

1. **存在结构化的“巨鲸接力链”**：`0xb0a270…` → `0x0003b5…` → `0xa9ac43…` 三地址在
   **两个独立时段**重复出现，且前后两笔金额**完全相等**（11:08/11:12 各 15,613 ETH；
   18:37/18:40 各 14,565 ETH），相隔仅 2–5 分钟。这是典型的**中间地址过账/接力转账**模式
   （可能为机构/做市商内部归集或链上中转），说明单看 tx_hash 会错过“同一巨鲸动作的完整链路”。
2. **本窗口净流向为「出所 + 点对点」**：1 笔从 Binance 提币至外部地址，其余为链上点对点，
   未见 `exchange_inflow`——即本时段巨鲸倾向于**从交易所提走 / 链上转移**而非“入所待抛”。
3. **峰值金额 $41.88M、集中在 15,613 / 15,340 / 14,565 ETH** 三个档位，量级高度一致，
   提示这些是本轮巨鲸“整百/整千 ETH”式的大额批处理操作。

### 三条 Data-driven Suggestions

1. **按“资金流链路”而非“单笔”建立预警/关联模型**
   实测证明同一中间地址在分钟级内接收并转发相同金额。建议对 `from–to–金额-时间窗` 做图聚类，
   把 `A→中转→B` 合并为一个“巨鲸动作”，并对其中的**重复中转地址**建立监控白名单/黑名单特征。

2. **把「交易所净流入/流出」与价格叠加成抛压信号**
   当 `exchange_inflow` 小时金额放大且 ETH 处于相对高位时，往往是短期抛压的领先指标。
   建议新增面板：交易所净流入曲线 + ETH 价格叠加，并设置阈值告警；同时追踪“交易所提币大户”去向。

3. **多级阈值 + 扩充交易所地址清单**
   仅 1/5 笔命中内置交易所地址集（覆盖率不足）。建议新增 `$1M`「关注级」，并把
   `EXCHANGE_ADDRESSES` 扩成外部可维护清单（更多交易所/做市商/矿池），提升流向归因覆盖率。

> 说明：以上为当前回填窗口的实测快照；随着持续轮询或更大回填窗口（`python backfill.py --blocks N`），
> 命中数与占比会动态变化，可重新运行看板查询得出新结论。

---

## 五、本次优化 (v2) —— 思路、决策与新增能力

> 本次优化聚焦：**更低的巨鲸阈值、自动分页回填、巨鲸地址画像、价格曲线采样、更丰富的看板**。

### 5.1 阈值下调：`WHALE_THRESHOLD_USD 10000000 → 500000`
- **决策**：初版 $10M 粒度的“巨鲸”在大时间窗内稀疏（2h 仅 3 笔），不利于实时预警与看板分析。
  下调至 `$500,000` 可捕获更多“中大型”链本币转账，且更贴近“机构/做市商级动作”。
- **实现**：修改 `.env.example` 与 `config.py` 的兜底默认值。注意——`environment .env` 是用户实测文件，
  若其中显式写了 `WHALE_THRESHOLD_USD`，会覆盖默认值；本优化只改默认（README 已说明）。

### 5.2 `backfill.py` 自动分页
- **需求**：用 `startblock`/`endblock` 循环调用 Etherscan，每次请求 `offset=1000`，自动翻页直到取完区块范围。
- **实现**：按“页”遍历 `[start_block, end_block]`，每页最多处理 `--offset`（默认 1000）个区块，
  同一页内并发拉取区块，页末自动 `cursor = page_end + 1` 前移到下一页，直到 `page_end >= end_block` 结束。
- **关键决策（实测约束）**：Etherscan 的 `account/txlistinternal`（原生按区块区间批量查询）为 **Pro 专属**，
  免费 key 不可用，因此把“按页翻取”落在免费的 `proxy/eth_getBlockByNumber` 上（每页=1000 区块）。
  每页写一条 `eth_price_ticks` 采样，支撑看板价格曲线。

### 5.3 `src/address_profiler.py` —— 巨鲸地址画像
- **思路**：高级分析关注的“链上身份证”：知道它有多少钱、持仓什么稳定币、动得多频繁、是不是合约。
- **数据维度**（每个巨鲸地址）：
  - ETH 余额 `/account/balance`（wei→ETH）与 **ETH 美元敞口 = ETH余额 × 现价**；
  - 三大稳定币持仓 `/account/tokenbalance`（USDT / USDC / DAI，6/6/18 位小数缩放）+ 稳定币合计 USD；
  - 交易频次 `/account/txlist` 统计 **24h / 7d 频次、最近活跃时间、观测到的流入/流出 ETH、平均/最大单笔**；
  - **合约探测** `/contract/getsourcecode`、**链上标签** `/account/getaddresstag`（分析师的“身份线索”）。
- **写入**：`address_profiles` 表，`address` 为主键（INSERT OR REPLACE，可增量）。
- **触发**：主检查器每轮对新地址画像；`backfill` 回填后自动画像；也可 `python -m src.address_profiler [--force]` 独立运行。

### 5.4 价格采样 `eth_price_ticks`
- 每个扫描/回填页写入一条 `(ts, price_usd)`，用于看板“ETH 价格曲线 + 巨鲸金额散点”面板。

### 5.5 Docker / Grafana 插件
- `docker-compose.yml` 新增 `GF_PLUGINS_PREINSTALL`，并在 `GF_INSTALL_PLUGINS` 中加入 **Percentage Trend** 插件。
- **决策（插件 id 校准）**：需求中的 `grafana-percentage-trend-panel` 是查询到的“显示名”；
  实际可下载的官方社区插件（Grafana Labs，Niko Schmuck）id 为 **`nikosc-percenttrend-panel`**。
  为保证真实可安装、可渲染，`docker-compose.yml` 与看板 `type` 使用 `nikosc-percenttrend-panel`（已注明）。

### 5.6 看板扩展（3 → 8 面板）
1. 24h 巨鲸金额分布（柱）  2. 流向占比（饼）  3. 巨鲸活动时间线（表）
4. **ETH 价格曲线 + 巨鲸金额散点**（Timeseries，对数轴，`eth_price_ticks` 采样）
5. **今日巨鲸交易统计**（Stat：今日笔数/总额/单笔最大）
6. **按交易所流向的金额分布**（Bar Chart）
7. **巨鲸地址画像**（Table：余额/稳定币/频次/合约/ETH 敞口/标签）
8. **本周 vs 上周巨鲸交易金额环比**（Percentage Trend，`nikosc-percenttrend-panel`）

### 5.7 v2 数据洞察（基于 $500K 数据实测）
> 见下节「六、v2 实测 Insights」。

---

## 六、v2 实测 Insights（$500,000 阈值）

> 数据源：`data/whale_alert.db`；ETH 现价 ≈ $2,700，阈值 $500,000（≈185 ETH）。
> 采集方式：对若干**当前活跃**的交易所热钱包调用 `account/txlist`（`offset=1000`，`sort=desc`）取最近大额转账。

### 实测概览
- **巨鲸交易**：18 笔，其中 **11 笔发生在最近 24 小时**；总金额约 **$74.3M**。
- **按流向金额**：`exchange_inflow` $40.5M（8 笔）、`exchange_outflow` $28.4M（6 笔）、`peer_to_peer` $5.4M（4 笔）。
  → 本窗口**净流入交易所**（$40.5M 进 vs $28.4M 出），偏向「入所」，需结合价格判断抛压。
- **地址画像**：20 个地址；`0x28c6c0…`(Binance 热钱包) ETH 余额 **85,457 ETH**（≈$2.3 亿）、
  `0xdfd529…` 22,210 ETH、`0x21a31e…` 21,153 ETH、`0x9696f5…` 19,040 ETH——典型交易所/托管巨鲸特征（非合约、24h 活跃度高）。

### 三条 Data-driven Suggestions（v2）
1. **净流入交易所 = 潜在抛压**：实测净流入 $12.1M，若价格处于高位需警惕短期抛压；建议把「交易所净流入」做独立面板并叠加价格。
2. **监控高频热钱包**：$500K 阈值下，交易所热钱包贡献了绝大多数命中；建议对 `EXCHANGE_ADDRESSES` 中“24h 交易频次高”的地址加权监控。
3. **画像驱动风险分层**：以「ETH 敞口 + 稳定币持仓 + 是否合约」对地址分层，把“巨额 ETH 敞口 + 大量稳定币”的地址列为高优先级观察对象。

### 5.8 本轮踩坑与修复（重要）
- **frser-sqlite-datasource v4 查询模型变更**：v4.x 的查询字段是
  `rawQueryText` / `queryText` / `queryType`（"table" | "time series"）/ `timeColumns`，
  **不再是** v3 的 `rawSql` / `query` / `format`。用旧字段会导致所有面板查询返回空。
  看板 JSON 已按 v4 模型改写（11 个 target）。
- **`account/txlist` 默认 `sort=asc`**：会返回“最老”的交易，导致画像的 24h/7d 频次错误、
  取数取到多年前的数据。已改为默认 `sort=desc`。
- **区块级 `eth_getBlockByNumber` 极易触发免费额度 429**：新增 `BACKFILL_WORKERS`（默认 3）
  可下调并发（`=1` 最稳）以规避限流。