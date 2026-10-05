# 开发日志 · 增量累积与数据资产化（feature/incremental-pipeline）

> 分支：`feature/incremental-pipeline` · 环境：`whale_alert_project`（Python 3.11）
> 本日志记录**本次增量改造**的完整开发过程：背景目标、数据模型、关键决策、踩坑与修复、
> 5 个新增 Panel 的 SQL 与计算逻辑、验证结果、运行与调度。
> 基础版（「单次运行 + 快照数据」）的设计见主分支 `main` 的 `docs/development-log.md`。

---

## 一、背景与目标

主分支 `main` 的检查器每轮轮询都重扫「最近 `SCAN_BLOCK_WINDOW` 个区块」，数据库本质是一份**滚动快照**：
- 数据不累积，时间序列（DoD / 7 日 / 30 日）分析无从谈起；
- 区块区间重叠、重复扫描，免费 API 额度被浪费。

**本次目标**：把「快照式看板」升级为「**持续累积的数据资产**」——
1. 每次只扫「上次扫描区块之后」的新数据；
2. 以 `tx_hash` 幂等去重后**追加**；
3. 用检查点 `scan_state.last_scanned_block` 断点续扫；
4. 支持 **cron / Cline Schedule 每日调度**；
5. 看板新增 5 个时间序列面板，默认 **Last 7 days**。

---

## 二、数据模型改造（`database.py`）

### 2.1 `whale_transfers.tx_hash` 加唯一约束 + `INSERT OR IGNORE`

```sql
CREATE TABLE IF NOT EXISTS whale_transfers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_hash      TEXT    NOT NULL UNIQUE,   -- 唯一约束：保证追加写入幂等
    block_number INTEGER,
    timestamp    INTEGER,
    from_address TEXT,
    to_address   TEXT,
    value_eth    REAL,
    value_usd    REAL,
    direction    TEXT,
    created_at   TEXT DEFAULT (datetime('now'))
);

-- 兼容旧库（CREATE TABLE IF NOT EXISTS 不会给已存在的表补列级约束）
CREATE UNIQUE INDEX IF NOT EXISTS idx_whale_tx_hash ON whale_transfers(tx_hash);
```

写入由「`INSERT` + 捕获 `IntegrityError`」改为直接忽略：

```python
cur = self.conn.execute(
    """
    INSERT OR IGNORE INTO whale_transfers
        (tx_hash, block_number, timestamp, from_address, to_address,
         value_eth, value_usd, direction)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (...),
)
self.conn.commit()
return cur.rowcount > 0   # 0 表示重复被忽略
```

### 2.2 新增检查点表 `scan_state`

```sql
CREATE TABLE IF NOT EXISTS scan_state (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),  -- 单行
    last_scanned_block INTEGER NOT NULL DEFAULT 0,
    updated_at         TEXT DEFAULT (datetime('now'))
);
```

配套读写方法：

```python
def get_last_scanned_block(self) -> int:
    row = self.conn.execute(
        "SELECT last_scanned_block FROM scan_state WHERE id = 1"
    ).fetchone()
    if row is not None:
        return int(row[0])
    # 首次运行：用已有数据里的最大区块初始化，避免重扫快照里的历史
    seed = self.conn.execute(
        "SELECT COALESCE(MAX(block_number), 0) FROM whale_transfers"
    ).fetchone()
    value = int(seed[0] or 0)
    self.set_last_scanned_block(value)
    return value

def set_last_scanned_block(self, block_number: int) -> None:
    self.conn.execute(
        """
        INSERT INTO scan_state (id, last_scanned_block, updated_at)
        VALUES (1, ?, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            last_scanned_block = excluded.last_scanned_block,
            updated_at = datetime('now')
        """,
        (int(block_number),),
    )
    self.conn.commit()
```

---

## 三、扫描流程改造（`whale_alert.py` / `etherscan_client.py` / `config.py`）

**`etherscan_client.py`** 新增按显式区间扫描的函数（区别于固定的「最近窗口」）：

```python
def scan_block_range(api_key, threshold_usd, start_block, end_block):
    eth_price_usd = get_eth_price_usd(api_key)
    transfers = fetch_block_transfers(api_key, start_block, end_block)
    return detect_whales(transfers, threshold_usd=threshold_usd, eth_price_usd=eth_price_usd)
```

**`config.py`** 新增免费额度保护上限：

```python
SCAN_MAX_BLOCK_SPAN = _get_int("SCAN_MAX_BLOCK_SPAN", 2000)
```

**`whale_alert.py`** 的 `run_scan` 改为「读检查点 → 续扫新块 → 写库 → 推进检查点」：

```python
latest = get_latest_block(api_key)
last_scanned = db.get_last_scanned_block()
start = last_scanned + 1
# 离线过久时，把一次补扫限制在最新 SCAN_MAX_BLOCK_SPAN 个区块内
floor = max(latest - config.SCAN_MAX_BLOCK_SPAN + 1, 0)
if start < floor:
    start = floor
if start > latest:
    whales = []                       # 没有新块
else:
    whales = scan_block_range(api_key, config.WHALE_THRESHOLD_USD, start, latest)

for whale in whales:
    if db.insert_whale(whale):        # INSERT OR IGNORE
        saved += 1

if latest is not None:
    db.set_last_scanned_block(latest) # 扫描成功后才推进检查点
```

---

## 四、关键决策

1. **幂等优先**：把唯一性放在**列级约束** `tx_hash UNIQUE`，配合 `INSERT OR IGNORE`，
   写入天然幂等、可安全重放，即便调度重叠或重跑也不会产生脏数据。
2. **检查点而非「最近 N 块」**：用 `scan_state.last_scanned_block` 表达「已扫到哪」，
   语义清晰，且重启 / 漏跑后可自动续扫。
3. **检查点初始化取 `MAX(block_number)`**：仓库自带历史数据（回填产物），
   若从 0 开始会重扫全部历史；用已有最大区块初始化，首轮只补最新增量。
4. **一次补扫设上限 `SCAN_MAX_BLOCK_SPAN`（2000）**：避免离线很久后一次性打爆免费额度
   （Etherscan 免费 ~5 req/s、10 万/天）。
5. **检查点「成功才推进」**：扫描抛异常时保持原值，下轮自动重试，不丢区间。
6. **新增 Panel 刻意避开窗口函数**：改用**相关子查询**实现 DoD / 滚动平均 / 累计净额，
   不依赖插件底层 SQLite 的窗口函数支持，保证开箱即用。
7. **时间范围改 `now-7d`**：与「7 日时间序列」口径一致（原为 `now-31d`）。

---

## 五、踩坑与修复

| # | 现象 | 原因 | 修复 |
| --- | --- | --- | --- |
| 1 | 老库 `scan_state` 不存在 | `CREATE TABLE IF NOT EXISTS` 只对新库生效；快照库是旧 schema | `WhaleDatabase` 初始化时 `executescript` 自动补建 `scan_state`；列级 `UNIQUE` 则用 `CREATE UNIQUE INDEX IF NOT EXISTS` 兼容旧表 |
| 2 | 首轮增量扫了 1000+ 块 | 快照库最大区块落后当前链头数小时 | 检查点初始化用 `MAX(block_number)`；并用 `SCAN_MAX_BLOCK_SPAN` 兜底 |
| 3 | 两个分支容器互相冲突 | 容器名 / 端口重复（都叫 `whale-grafana`、都用 3000） | 增量分支改名 `whale-grafana-incremental` / `whale-checker-incremental`，端口改 **3001** |
| 4 | 增量容器启动还跑了回填 | 复用了主分支 entrypoint 的 `BOOTSTRAP_BLOCKS=2000` | 增量 compose 设 `BOOTSTRAP_BLOCKS=0`，改由检查点驱动 |
| 5 | 两个容器同 key 抢额度触发 429 | 主/增量同时回填 & 轮询，共享同一 API key | `_get()` 已有退避重试（`BASE_RATE_LIMIT_DELAY` + rate-limit backoff），慢但可自愈 |

---

## 六、5 个新增时间序列 Panel 的 SQL 与计算逻辑

> 面板默认时间范围 **Last 7 days**。为兼容插件底层 SQLite，全部用**相关子查询**而非窗口函数。

### 6.1 DoD 环比（每日巨鲸金额 + 日环比%）

```sql
WITH daily AS (
  SELECT date(timestamp,'unixepoch','localtime') AS d, SUM(value_usd) AS total
  FROM whale_transfers
  GROUP BY d
)
SELECT
  CAST(strftime('%s', d) AS INTEGER) AS time,
  ROUND(total, 0) AS '日总额(USD)',
  ROUND(
    (total - (SELECT total FROM daily p WHERE p.d < daily.d ORDER BY p.d DESC LIMIT 1))
    * 100.0
    / NULLIF((SELECT total FROM daily p WHERE p.d < daily.d ORDER BY p.d DESC LIMIT 1), 0),
    2
  ) AS 'DoD环比%'
FROM daily
ORDER BY d;
```
**计算逻辑**：先按天汇总金额；再用相关子查询取「比当天早的最近一天」的总额做分母，算 `(今日−昨日)/昨日×100%`。
**业务含义**：单日相对前一日的**变化速度**，判断巨鲸活动升温/降温。

### 6.2 7 日滚动平均

```sql
WITH daily AS (
  SELECT date(timestamp,'unixepoch','localtime') AS d, SUM(value_usd) AS total
  FROM whale_transfers
  GROUP BY d
)
SELECT
  CAST(strftime('%s', d) AS INTEGER) AS time,
  ROUND(total, 0) AS '日总额(USD)',
  ROUND((
    SELECT AVG(total) FROM daily w
    WHERE w.d BETWEEN date(daily.d, '-6 days') AND daily.d
  ), 0) AS '7日滚动平均(USD)'
FROM daily
ORDER BY d;
```
**计算逻辑**：对每天，取「当天及前 6 天」（`BETWEEN date(d,'-6 days') AND d`）的 `AVG(total)`，即 7 日滑动窗口均值。
**业务含义**：抹平单日噪声，识别**真实趋势**。

### 6.3 堆叠面积（每日流向金额构成）

```sql
SELECT
  CAST(strftime('%s', date(timestamp,'unixepoch','localtime')) AS INTEGER) AS time,
  ROUND(SUM(CASE WHEN direction='exchange_inflow'  THEN value_usd ELSE 0 END), 0) AS '交易所流入',
  ROUND(SUM(CASE WHEN direction='exchange_outflow' THEN value_usd ELSE 0 END), 0) AS '交易所流出',
  ROUND(SUM(CASE WHEN direction='peer_to_peer'     THEN value_usd ELSE 0 END), 0) AS '点对点'
FROM whale_transfers
GROUP BY time
ORDER BY time;
```
**计算逻辑**：按天 `GROUP BY`，用 `CASE WHEN` 把金额按 `direction` 透视成三列（宽表），面板 `stacking.mode="normal"` 堆叠。
**业务含义**：资金**结构变化**——入所（潜在抛压）vs 点对点转移。

### 6.4 热力图（巨鲸交易金额密度）

```sql
SELECT
  CAST(timestamp AS INTEGER) AS time,
  ROUND(value_usd, 0) AS '金额'
FROM whale_transfers
ORDER BY time;
```
**计算逻辑**：直接给「时间 + 单笔金额」，由 Grafana heatmap（`calculate: true`）按 X=时间、Y=金额自动分桶着色。
**业务含义**：快速定位**大额集中时段**与金额分布密度。

### 6.5 累计净额（交易所累计净流入）

```sql
SELECT
  t AS time,
  ROUND((
    SELECT SUM(CASE WHEN direction='exchange_inflow'  THEN value_usd
                    WHEN direction='exchange_outflow' THEN -value_usd
                    ELSE 0 END)
    FROM whale_transfers w2
    WHERE CAST(w2.timestamp AS INTEGER) <= t
  ), 0) AS '累计净流入交易所(USD)'
FROM (SELECT DISTINCT CAST(timestamp AS INTEGER) AS t FROM whale_transfers)
ORDER BY t;
```
**计算逻辑**：对每个时间点 `t`，用相关子查询累加「流入(+)/流出(−)」，得到**累计**净额曲线。
**业务含义**：区间内的**净抛压 / 承接**方向与强度。

---

## 七、每日运行可视化（`scripts/daily_report.py`）

为了让「每日定时任务」的**运行结果可视化**，新增 `scripts/daily_report.py`：

- 运行 `whale_alert.py --once`（增量扫描）；
- 查询 SQLite 汇总本次运行状态：运行状态 / 链上拉取状态 / 累积表写入状态 / Grafana 刷新状态 /
  运行时长 / 本次新增条数 / 最新记录时间戳 / 总记录数 / `last_scanned_block`；
- 生成 HTML 报告到 `reports/daily_report_YYYYMMDD.html`（含 Grafana 链接 http://localhost:3001）；
- macOS 上用 `osascript` 弹系统通知（成功：`新增X条，总记录Y条`；失败：`今日运行失败：<原因>`）；
- 成功后自动用 `webbrowser.open` 打开当天报告；
- **全程 try/except 包裹**：即使 API 配额耗尽 / 网络断开，也会走到「失败报告 + 失败通知」，绝不静默消失。

调度用法（Cline Schedule / cron）：

```bash
# Cline Schedule 的每日任务 Prompt
conda activate whale_alert_project && python scripts/daily_report.py

# 等价系统 cron
0 15 * * * cd ~/Desktop/whale-alert-system-incremental && \
  conda run -n whale_alert_project python scripts/daily_report.py
```

---

## 八、验证

- **单测**：`pytest` → **8 passed**（含 3 个增量单测：唯一约束/去重、检查点初始化、检查点 upsert）。
- **实测增量**：连续两次 `python whale_alert.py --once`，`scan_state` 从 `26127513` → `26127515`
  （第二轮仅扫新增 2 块），确认「只扫新块 + 状态结转」。
- **看板**：JSON 合法，13 个面板 + 1 个预留位；5 个新面板 SQL 均在快照库上执行通过。
- **双栈并存**：主分支 Grafana 3000、增量分支 Grafana 3001，同时运行互不冲突。

---

## 九、如何运行 / 调度

```bash
conda activate whale_alert_project

# 单次增量扫描
python whale_alert.py --once

# 持续增量轮询
python whale_alert.py

# 每日运行 + HTML 报告 + 系统通知（供定时任务调用）
python scripts/daily_report.py

# 全栈（Grafana 3001 + 检查器）
docker compose up -d --build
```

> 数据洞察先留空，见 [docs/data-insights.md](data-insights.md)。
