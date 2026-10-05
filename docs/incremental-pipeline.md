# 🌿 增量累积与数据资产化（feature/incremental-pipeline）

> 本文档是 `feature/incremental-pipeline` 分支的专属说明。
> 主分支（`main`）保留「单次运行 + 快照数据」的干净版本；本分支在此基础上加入
> **累积表 + 定时调度 + 时间序列分析**，把数据从「一次性快照」升级为「可长期累积的数据资产」。

---

## 1. 这个分支做了什么（一句话）

把「每次轮询都重扫固定窗口、快照式覆盖」改成「**只扫新块、结转状态、持续累加**」：

| 维度 | 主分支 main | 本分支 feature/incremental-pipeline |
| --- | --- | --- |
| 去重 | 唯一索引 + `INSERT` 捕获异常 | `tx_hash UNIQUE` + **`INSERT OR IGNORE`** |
| 扫描范围 | 固定「最近 N 块」 | **从 `last_scanned_block` 续扫到最新块** |
| 状态 | 无 | 新增 `scan_state` 表（`last_scanned_block`） |
| 调度 | 容器内持续轮询 | 增加**每日 15:00 定时任务**（`whale_alert.py --once`） |
| 看板 | 24h 视角为主 | 新增 **5 个时间序列面板**，默认 **Last 7 days** |
| 数据 | 快照 | **可长期累积的数据资产**（时间越久越厚） |

---

## 2. 如何 clone 这个分支

```bash
# 方式一：直接克隆分支
git clone -b feature/incremental-pipeline \
  ssh://git@ssh.github.com:443/DingBangBang/whale-alert-system.git \
  whale-alert-system-incremental
cd whale-alert-system-incremental

# 方式二：已 clone 主仓库的情况（推荐用 worktree，主分支目录与增量目录并存）
git clone ssh://git@ssh.github.com:443/DingBangBang/whale-alert-system.git whale-alert-system
cd whale-alert-system
git fetch origin feature/incremental-pipeline
git worktree add -b feature/incremental-pipeline ../whale-alert-system-incremental origin/feature/incremental-pipeline
cd ../whale-alert-system-incremental
```

> 环境准备与主分支一致（conda `whale_alert_project`，Python 3.11）：
> `pip install -r requirements.txt`，并把密钥写入 `environment .env`。

### 运行增量扫描

```bash
conda activate whale_alert_project

# 单次增量扫描（只扫 last_scanned_block 之后的新块）
python whale_alert.py --once

# 持续增量轮询
python whale_alert.py
```

---

## 3. Docker Hub 镜像 `daily-whale-scan`

本分支的定时扫描器已打包为镜像 **`daily-whale-scan`**，可直接拉取后按日运行，无需本地安装依赖。

```bash
# 拉取镜像（把 <dockerhub-user> 换成你自己的 Docker Hub 命名空间）
docker pull <dockerhub-user>/daily-whale-scan:latest

# 单次增量扫描（把宿主机 ./data 挂进去，密钥用 -e 注入）
docker run --rm \
  -v "$PWD/data:/app/data:rw" \
  -e ETHERSCAN_API_KEY=你的Key \
  -e WHALE_THRESHOLD_USD=500000 \
  <dockerhub-user>/daily-whale-scan:latest \
  python whale_alert.py --once
```

本地构建并推送到 Docker Hub 的方式：

```bash
docker build -t <dockerhub-user>/daily-whale-scan:latest .
docker push <dockerhub-user>/daily-whale-scan:latest
```

> 💡 也可以用 `docker compose` 全栈启动（Grafana + 检查器），镜像名即 compose 中的 `build:` 目标。
> 若只想复用镜像，可把 `docker-compose.yml` 里 `whale-checker` 的 `build: .` 换成
> `image: <dockerhub-user>/daily-whale-scan:latest`。

---

## 4. 每日定时任务（Cline Schedule · 15:00）

除了容器内的持续轮询，本分支还演示**每日定时调度**这一「数据资产化」关键路径：

- 触发时间：**每天 15:00**
- 运行目录：本增量文件夹（`whale-alert-system-incremental`）
- 执行命令：`conda run -n whale_alert_project python whale_alert.py --once`
- 效果：拉取自 `last_scanned_block` 起的新块 → `INSERT OR IGNORE` 写入 SQLite → 更新 `scan_state` → Grafana 看板自动刷新。

> 定时任务由 Cline 的 Schedule 能力托管（本机侧），每次运行都是独立的 `--once` 增量扫描，
> 因此即使中间某天错过，下一次也会自动续扫（受 `SCAN_MAX_BLOCK_SPAN` 上限保护）。

---

## 5. 7 天 / 30 天数据展示位（预留）

时间范围默认已设为 **Last 7 days**，并预留 **7d / 30d** 展示位（见看板底部「📅 7D / 30D 数据展示位（预留）」）。随着 `scan_state` 持续累积区块，7 天 / 30 天的历史会自动变厚，届时可直接把预留位替换为正式面板：

| 预留位 | 计划指标（待数据变厚后启用） |
| --- | --- |
| **7D** | 7 日累计巨鲸金额、7 日均值、7 日净流入趋势 |
| **30D** | 30 日巨鲸活跃度、30 日累计净额、月内 Top 地址 |

---

## 6. 数据洞察（先留空，待补）

> ⏳ **本节暂时留空**：增量数据需要持续累积数日后，`7d / 30d` 的时间序列才具备统计意义。
> 待累积足够天数后，将补充：
> 1. 净流入交易所 vs ETH 价格的相关性；
> 2. 巨鲸活跃时段热力图与价格波动的关系；
> 3. 高频巨鲸地址的行为画像与接力链聚类。
>
> 现有的「基于快照」洞察见主分支 [docs/development-log.md](development-log.md)。

