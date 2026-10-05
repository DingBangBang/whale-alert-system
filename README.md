# 🐋 链上巨鲸行为预警系统 (On-chain Whale Behaviour Alert System)

[中文](README.md) | [English](README_en.md)

准实时（定时轮询）监控以太坊主网的大额 ETH 转账。当一笔转账的美元价值超过阈值时，系统把该“巨鲸交易”写入 SQLite，并在终端打印预警；同时通过 **Docker Compose** 编排 Grafana，用预制看板可视化近 24 小时的巨鲸活动。

- **数据源**：Etherscan API V2（免费 tier，通过 `proxy/eth_getBlockByNumber` 读取区块内 ETH 转账）
- **阈值**：`WHALE_THRESHOLD_USD`（默认 `500000`，即 50 万美元，较初版 $10M 下调以捕获更多巨鲸）——从 `environment .env` 读取
- **存储**：SQLite（`data/whale_alert.db`，含 `whale_transfers` / `address_profiles` / `eth_price_ticks`）
- **可视化**：Grafana 11 + `frser-sqlite-datasource` + `nikosc-percenttrend-panel`，provisioning 自动加载
- [**在线公开静态快照（无需本地运行即可查看）**](http://localhost:3000/dashboard/snapshot/GXcEjoseCUqtEZMv4TkGAxQ9YhwHnFeO)
- [**在线公开动态看板（无需本地运行即可查看）**](https://snapshots.raintank.io/dashboard/snapshot/YWCQi1i0cFvSQ2rOdIJi7lhtT9eR4oGC)

> 💡 **如需了解增量累积与数据资产化设计，请查看 `feature/incremental-pipeline` 分支。**

---

## 🌿 增量累积模式（feature/incremental-pipeline）

本项目支持**增量累积模式**，通过 **cron 每日调度持续更新**：

- **只拉新数据**：每次扫描**只拉取上次扫描区块之后的新数据**，基于 `tx_hash` 去重后**追加**到 `whale_transfers` 表
  （列级 `tx_hash UNIQUE` 约束 + `INSERT OR IGNORE`，幂等可重放）。
- **断点续扫**：扫描进度由 `scan_state.last_scanned_block` 记录，重启或漏跑后自动从上次位置续扫
  （`SCAN_MAX_BLOCK_SPAN` 防止一次补太多）。
- **每日调度**：可通过 **cron** 或 **Cline Schedule** 每日调度，**持续积累数据资产**。
- **内置历史数据**：仓库中的 `data/whale_alert.db` 已包含通过 `backfill.py` 回填的**历史数据**，
  用于展示**多日时间序列分析**效果。若要持续更新，请配置每日调度任务（见下）。

### 别人 clone 后想这样做，操作步骤

```bash
# 1) 克隆增量分支
git clone -b feature/incremental-pipeline \
  ssh://git@ssh.github.com:443/DingBangBang/whale-alert-system.git \
  whale-alert-system-incremental
cd whale-alert-system-incremental

# 2) 激活 conda 环境并安装依赖
conda activate whale_alert_project      # Python 3.11；没有就先 conda create -n whale_alert_project python=3.11
pip install -r requirements.txt
# 把 ETHERSCAN_API_KEY 写入 environment .env（参考 .env.example）

# 3) 配置每日调度（二选一）
# 3a) Cline Schedule（推荐，先创建一次）
cline schedule create "daily-whale-scan" \
  --cron "0 15 * * *" \
  --prompt "conda activate whale_alert_project && python whale_alert.py --once" \
  --workspace ~/Desktop/whale-alert-system-incremental

# 3b) 或系统 cron（每天 15:00）
# 0 15 * * * cd ~/Desktop/whale-alert-system-incremental && \
#   conda run -n whale_alert_project python whale_alert.py --once >> logs/cron.log 2>&1

# 4) 数据会自动增量写入 data/whale_alert.db，Grafana 看板随之刷新
```

### 🐳 直接拉取镜像（不想自己 build）

如果你不想自己 build 但又想拥有这个项目，可以直接拉取镜像：

```bash
docker pull bonnie333333333/daily-whale-scan:latest

# 单次增量扫描（挂载宿主 ./data，密钥用 -e 注入）
docker run --rm -v "$PWD/data:/app/data:rw" \
  -e ETHERSCAN_API_KEY=你的Key \
  bonnie333333333/daily-whale-scan:latest python whale_alert.py --once
```

### 📦 本地构建并推送（维护者）

```bash
docker build -t bonnie333333333/daily-whale-scan:latest .
docker push bonnie333333333/daily-whale-scan:latest
```

---

## Demo Data & Dashboard (7d / 30d)

<!--
  待 30 天累积数据生成后补充数据库文件与看板截图。
  计划在此处补充：
  - 30 天累积的 data/whale_alert.db（或压缩包）与下载说明
  - Last 7 days / Last 30 days 时间序列看板截图
  - 数据覆盖区间、总记录数、巨鲸笔数等统计
-->

> ⏳ 待 30 天累积数据生成后补充数据库文件与看板截图。

---

## 📈 时间序列分析

看板在原有 8 个面板基础上，新增以下 **5 个时间序列 Panel**（默认时间范围 **Last 7 days**），用于把「累积数据」变成可读的趋势：

| Panel | 类型 | 计算逻辑（SQL 摘要） | 业务含义 |
| --- | --- | --- | --- |
| **DoD 环比**（每日巨鲸金额 + 日环比%） | timeseries | 按天 `SUM(value_usd)`，并用相关子查询取「前一天总额」算 `(今日-昨日)/昨日×100%` | 看单日相对前一日的**变化速度**，判断巨鲸活动升温还是降温 |
| **7 日滚动平均** | timeseries | 按天汇总后，对每天取「当天及前 6 天」的 `AVG(total)` | 抹平单日噪声，识别**真实趋势**（而非被某天巨鲸暴击带偏） |
| **堆叠面积**（每日流向金额构成） | timeseries | 按天 `GROUP BY`，用 `CASE WHEN direction=...` 拆成「交易所流入 / 流出 / 点对点」三条序列并堆叠 | 看资金**结构变化**：是入所（潜在抛压）还是点对点转移 |
| **热力图**（巨鲸交易金额密度） | heatmap | 每条记录 `(timestamp, value_usd)`，交给 Grafana 按时间/金额自动分桶 | 快速定位**大额集中时段**、识别金额分布密度 |
| **累计净额**（交易所累计净流入） | timeseries | 对每个时间点用相关子查询累加 `流入(+)/流出(-)` | 衡量区间内的**净抛压 / 承接**方向与强度 |

> 具体 SQL 与逐条计算逻辑见 [docs/development-log.md](docs/development-log.md) 第七章。

---

## 🔔 每日运行反馈

每次定时任务（`python scripts/daily_report.py`）完成后：

- **生成 HTML 报告**到 `reports/daily_report_YYYYMMDD.html`；
- **弹出 macOS 系统通知**（`osascript`），例如「新增 X 条，总记录 Y 条」；任务失败时也会弹
  「今日运行失败：<原因>」而不会静默消失；
- 成功后在浏览器中**自动打开**当天的 HTML 报告。

报告包含字段：运行状态、链上拉取状态、累积表写入状态、Grafana 刷新状态、运行时长、
**本次新增条数**、**最新记录时间戳**、**总记录数**、`last_scanned_block`，以及 Grafana 链接
（http://localhost:3001）。详见 [scripts/daily_report.py](scripts/daily_report.py)。

---

## ✨ 主要特性

| 模块 | 说明 |
| --- | --- |
| `whale_alert.py` | 主入口：轮询 Etherscan，检测巨鲸，写入数据库并打印预警 |
| `etherscan_client.py` | Etherscan V2 客户端 + 纯函数 `detect_whales` / `classify_direction` |
| `database.py` | SQLite 建表 / 去重写入 / 地址画像与价格采样（WAL 模式） |
| `backfill.py` | 历史回填工具：**startblock/endblock 自动分页**（offset=1000/页）+ 地址画像 |
| `src/address_profiler.py` | 巨鲸地址画像：余额 / USDT-USDC-DAI 持仓 / 交易频次 / 合约探测等 |
| `Dockerfile` / `docker-compose.yml` | Python 检查器 + Grafana 编排 |
| `grafana/` | Provisioning：SQLite 数据源 + 基础看板模板 |
| `tests/` | pytest 单测，覆盖 `detect_whales` |

> ⚡ **本次优化（v2）**：①阈值默认下调至 `$500,000`；②`backfill.py` 支持 `startblock/endblock + offset` 自动分页；
> ③新增 `src/address_profiler.py` 巨鲸地址画像（ETH 余额 / USDT-USDC-DAI 持仓 / 交易频次 / 合约探测 / 近活跃时间等）并写入 `address_profiles` 表；
> ④新增 `eth_price_ticks` 价格采样表；⑤Grafana 看板扩展到 8 个面板（价格+散点、今日统计、流向柱图、地址画像、周环比）。详见 [docs/development-log.md](docs/development-log.md)。

---

## 🏗️ 架构图

```mermaid
flowchart LR
    subgraph Host["宿主机 (Docker Compose)"]
        C["whale-checker<br/>Python 3.11 镜像"]
        P["src/address_profiler.py<br/>巨鲸画像（随扫描调用）"]
        G["grafana 11 镜像<br/>sqlite + percent-trend 插件"]
        V[(./data/whale_alert.db<br/>bind-mount)]
    end

    ES[("Etherscan API V2")]

    C -- "eth_getBlockByNumber / ethprice / balance / tokenbalance / txlist" --> ES
    P -- "balance / tokenbalance / txlist / getaddresstag" --> ES
    C --> V
    P --> V
    C -. "写地址画像 + 价格采样" .-> P
    G -- "挂载只读 /data/whale_alert.db" --> V
    U["用户 / 浏览器"] -- "http://localhost:3000" --> G

    C -. "预警输出到 stdout / 日志" .-> T["终端 / Docker logs"]
```

---

## 🔄 数据流图

```mermaid
flowchart TD
    A["environment .env<br/>API Key / 阈值 / 轮询间隔"] --> B[config.py]
    B --> C[etherscan_client.py]
    C -->|1. 获取 ETH 价格| E1["stats/ethprice"]
    C -->|2. 获取最新区块| E2["proxy/eth_blockNumber"]
    C -->|3. 按区块读取转账| E3["proxy/eth_getBlockByNumber"]
    E1 & E2 & E3 --> D["转账记录<br/>value_eth / from / to / ts"]
    D --> F{"detect_whales<br/>value_usd >= 阈值?"}
    F -- "否" --> X["忽略"]
    F -- "是" --> H["classify_direction<br/>inflow/outflow/peer"]
    H --> I[WhaleDatabase<br/>写入 whale_alert.db]
    I --> J["终端预警 🔔"]
    I --> R[src/address_profiler.py<br/>余额 / 稳定币持仓 / 频次]
    R --> I

    E1 --> Pt["eth_price_ticks 采样"] --> I
    I --> K["Grafana<br/>金额分布 / 价格+散点 / 今日统计 / 流向柱图 / 地址画像 / 周环比"]
```

---

## 📦 环境准备

项目使用 conda 虚拟环境 `whale_alert_project`（Python 3.11，已存在）。

```bash
# 1) 创建并激活环境（所有命令都需要）
conda create -n whale_alert_project python=3.11
conda activate whale_alert_project

# 2) 安装依赖（在项目根目录执行）
cd whale-alert-system        # 或： cd /path/to/whale-alert-system
pip install -r requirements.txt

# 3) 安装Docker和Grafana服务
```

### 配置文件 `environment .env`

把真实值填到项目根目录的 `environment .env`（已被 `.gitignore` 忽略，不入库）：

```ini
ETHERSCAN_API_KEY=你的Etherscan V2 API Key
WHALE_THRESHOLD_USD=500000
POLL_INTERVAL_SECONDS=60
SCAN_BLOCK_WINDOW=20
CHAIN_ID=1
```

- `ETHERSCAN_API_KEY`：在 [etherscan.io](https://etherscan.io/apis) 创建。
- `WHALE_THRESHOLD_USD`：巨鲸判定阈值（美元），**默认 50 万**（`config.py` 兜底值；若 `environment .env` 显式赋值则以此为准）。
- `SCAN_BLOCK_WINDOW`：每次轮询扫描的最新区块数（每个区块 = 一次 API 请求，受免费额度限制，默认 20）。

> 参考模板见 [`.env.example`](./.env.example)。

---

## 🚀 运行

### 方式一：本地运行

```bash
# 单次扫描（测试用 / 定时任务）
python whale_alert.py --once

# 持续轮询（默认每 60s 一次）
python whale_alert.py

# 历史数据回填（自动分页：startblock..endblock，每页 offset=1000 区块）
python backfill.py --blocks 1500 --offset 1000

# 只回填、不做地址画像
python backfill.py --blocks 1000 --no-profile

# 巨鲸地址画像（独立运行，或由扫描/回填自动触发）
python -m src.address_profiler            # 增量画像
python -m src.address_profiler --force    # 全量重画像
```

### 方式二：Docker Compose（推荐）

```bash
git clone <repo>

cd whale-alert-system        # 或： cd /path/to/whale-alert-system

# 构建并启动docker容器，此时容器会自动运行轮询+回填数据
docker compose up -d --build

# 查看巨鲸预警日志
docker compose logs -f whale-checker
```

启动后访问 Grafana：**http://localhost:3000**

- 登录：默认 `admin` / `admin`
- 数据源与看板由 provisioning 自动加载，无需手动配置。
- **开箱即用（一键全自动）**：首次启动时 `docker-entrypoint.sh` 会先把仓库内置的快照数据（`seed/whale_alert.db`）灌入运行库，因此**哪怕还没配置 API Key，也能立刻看到有数据的看板**；随后自动回填最新 **2000 个区块**（`BOOTSTRAP_BLOCKS`，可用 `FORCE_BACKFILL=1` 重跑）并进入持续轮询。想要「持续更新」的实时数据，只需在 `environment .env` 里提供 `ETHERSCAN_API_KEY`。

> **插件说明**：需求中的 “Percentage Trend” 面板使用官方社区插件（Grafana Labs），其可安装 id 为
> `nikosc-percenttrend-panel`（`grafana-percentage-trend-panel` 是显示名，非实际插件 id）。
> `docker-compose.yml` 中已同时设置 `GF_INSTALL_PLUGINS`（运行时安装）与 `GF_PLUGINS_PREINSTALL`。

---

## 🕵️ 预期效果

终端预警示例：

```applescript
====================================================================
  🐋  3 笔巨鲸交易触发（阈值 ≥ $10,000,000 USD）
====================================================================
  [exchange_inflow] 4,850.00 ETH ≈ $13,012,000.00 USD
    from: 0xabc...
    to:   0x28c6c0...  (Binance)
    hash: 0x7f...
====================================================================
```

Grafana 看板（`whale-overview`）共 8 个面板：

1. **最近 24 小时巨鲸交易金额分布**（按小时柱状图，USD）
2. **按交易所流向占比**（饼图：流入 / 流出 / 点对点）
3. **巨鲸活动时间线**（明细表）
4. **ETH 价格曲线 + 巨鲸交易金额散点**（Timeseries，对数轴；价格源自 `eth_price_ticks` 采样）
5. **今日巨鲸交易统计**（Stat：今日笔数 / 总额 / 单笔最大）
6. **按交易所流向的金额分布**（Bar Chart）
7. **巨鲸地址画像**（Table：余额 / 稳定币 / 交易频次 / 合约探测 / ETH 敞口）
8. **本周 vs 上周巨鲸交易金额环比**（Percentage Trend）


---

## ⏱️ 关于「实时」与数据覆盖范围

本系统采用**准实时轮询架构（定时轮询 Polling）**，而不是实时流式推送（Streaming）；轮询间隔由 `.env` 中的 `POLL_INTERVAL_SECONDS` 控制。它通过可配置的轮询间隔持续从 Etherscan 拉取最新区块数据，写入 SQLite 供 Grafana 展示。**它不是真正的流式实时**（比如 WebSocket 订阅 `newHeads`），因为 Etherscan 免费 API 不提供 WebSocket 推送；但把轮询间隔缩短到 10–15 秒即可达到近似实时的监控效果。之所以选择该方案，是因为 Etherscan 免费 API 的限制——通过 Alchemy 或 Infura 的 WebSocket 订阅 `newHeads` 需要付费节点。

当前数据库中的巨鲸交易记录覆盖最近约 **7,200 个区块（约 24 小时）**的链上数据。回填范围可通过 `backfill.py --blocks N` 参数调整；若需覆盖更长时间窗口，建议结合 `startblock` 和 `endblock` 参数按需回填，并注意 Etherscan 免费 API 的每日调用限额（10 万次/天）。如果想让覆盖时间更长，只需修改 `backfill.py` 的区块范围，比如 `--blocks 10000` 就是大约 33 小时。

---

## 🧪 测试

```bash
conda activate whale_alert_project
python -m pytest -q
```

覆盖 `detect_whales` 的阈值判定、边界值、方向分类与输入不被修改等用例。

---

## 📁 目录结构

```
.
├── whale_alert.py            # 主入口（轮询 + 预警）
├── etherscan_client.py       # Etherscan V2 客户端 + detect_whales
├── database.py               # SQLite 存取
├── config.py                 # 配置加载（读 environment .env）
├── backfill.py               # 历史回填（auto-pagination, offset=1000/页）
├── src/address_profiler.py   # 巨鲸地址画像（余额/稳定币/频次/合约）
├── src/__init__.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example              # 环境变量模板（真实值在 environment .env）
├── environment .env          # 真实密钥/阈值（已 gitignore，不入库）
├── data/whale_alert.db       # SQLite 数据库（运行生成）
├── grafana/
│   ├── provisioning/
│   │   ├── datasources/whale_datastore.yaml
│   │   └── dashboards/whale_dashboards.yaml
│   └── dashboards/whale_dashboard.json
├── tests/test_whale_alert.py
└── docs/development-log.md   # 开发日志 + 看板 insights
```

---

## ⚠️ 注意事项

- **免费 API 额度**：Etherscan 免费 tier 约 5 请求/秒、10 万请求/天。`SCAN_BLOCK_WINDOW` 每次轮询会消耗该值个请求，长时间高频轮询前请核算预算。
- **内部交易（合约间转账）**：本项目基于 `eth_getBlockByNumber` 读取对外可见的 VALUE 转账，主要覆盖“以太坊本币转账”。若需追踪合约内部 ETH 流转，需升级 Etherscan API Pro（`txlistinternal`）。
- **交易所地址清单**：有限的内置地址集（Binance/Coinbase/Kraken），用于流向分类，可按需扩充 `EXCHANGE_ADDRESSES`。
- **时区**：看板使用浏览器时区，DB 内存储 unix 时间戳。

---

## 📄 文档

- [开发日志 + 数据洞察](docs/development-log.md)

