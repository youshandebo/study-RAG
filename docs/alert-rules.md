# 入库流水线告警规则（Prometheus / Alertmanager）

规则里的每个指标名、标签键、取值域都按当前代码实抄，可直接粘贴进 rules 文件：

- 指标注册与文本渲染：`backend/app/core/metrics.py`
- 埋点与收尸：`backend/app/api/v1/ingest.py`、`backend/app/main.py`
- 默认策略参数：`backend/app/core/task_policy.py`

---

## 0. 先读这节：两个会让告警"假绿 / 假红"的前提

### 0.1 收尸由抓取动作驱动 —— 监控挂了，僵尸告警必然失明

`/metrics` 端点在被拉取时**内联执行一次收尸巡检**（`app/main.py::metrics_endpoint`
→ `ingest.reap_zombie_tasks()`）。这是 1C2G 上的刻意取舍：不养常驻线程，用 Prometheus
的拉取节奏当巡检频率（15~60s 对"心跳停摆十分钟才算死"绰绰有余）。代价是：

- **Prometheus 挂了 / target 掉了 → 僵尸既不收尸、也不计数** → 下面的 R1（僵尸占比）
  永远不会响，而且这恰恰表现为"一切太平"，值班最容易直接读成"没有僵尸"。
- 因此**必须**配一条元告警兜底（建议 severity 不低于 R1）：

```yaml
- alert: IngestMetricsScrapeDown
  expr: absent(up{job="<job 名>"}) or up{job="<job 名>"} == 0
  for: 5m
  annotations:
    summary: "/metrics 拉取中断：僵尸停止收尸且不再计数，R1~R4 全部失明"
```

**结论：看到 R1 静默，先确认抓取在不在，再谈"没有僵尸"。**

### 0.2 阈值是先验，不是标定值

目前还没有真实样本，所以下面所有 `> 0.01`、`0.8 ×`、`> 0.05` 都是**工程先验**，
不是统计结论。它们的用途是"先把盘挂上"，等攒够样本后按 §3 的口径重新标定。
（P0 概率标定仍未闭环——这是已知未决项，不是本文档能替你决定的。）

---

## 1. 指标口径

| 指标 | 类型 | 标签 | 语义 |
|---|---|---|---|
| `ingest_task_duration_seconds` | histogram | `media_type` ∈ audio/board/text/other；`outcome` ∈ succeeded/failed/queue_timeout | 任务全栈耗时（置 running → 终态，含排队） |
| `ingest_phase_duration_seconds` | histogram | `phase` ∈ queue/pipeline | 单相位耗时：queue=抢槽排队，pipeline=流水线执行 |
| `ingest_tasks_zombie_total` | counter | `media_type` | 被判定僵尸并收尸的任务数 |
| `ingest_phase_budget_exceeded_total` | counter | `phase` | 阶段耗时超过软预算的次数（只计数，不硬杀） |
| `ingest_tasks_active` | gauge | — | 当前积压（仍在 running 的任务数） |

直方图按 Prometheus 约定暴露 `_bucket{le}` / `_sum` / `_count`，其中 **`_sum`、`_count`
不带 `le`**。两套直方图共用同一组桶（同量级域、边界一致才能横向比）：

```
1, 5, 15, 30, 60, 120, 300, 600, 900, 1800, 3600   （秒）
```

**R1 的分母为什么是 `count + zombie`**：正常终态（succeeded / failed / queue_timeout）
计入 `_count`；僵尸是"活着但心跳停了"，流水线没走到终点、不写耗时样本，只由收尸计入
`zombie_total`。两者互斥，合起来就是"已终结的全部任务"，所以这个比值等于
**僵尸占终结任务的比例**。

---

## 2. 告警规则

### R1 僵尸占比过高 —— 核心信号：误杀在爬升

```yaml
- alert: IngestZombieRatioTooHigh
  expr: |
    (
      sum(rate(ingest_tasks_zombie_total[1h]))
      /
      (
        sum(rate(ingest_task_duration_seconds_count[1h]))
        + sum(rate(ingest_tasks_zombie_total[1h]))
      )
    ) > 0.01
  for: 30m
  labels: {severity: warning, subsystem: ingest}
  annotations:
    summary: "过去 1h 每 100 个终结任务里有超过 1 个被误判为僵尸"
```

- **为什么用比例而不是绝对值**：僵尸绝对数会随流量涨，绝对值告警在高峰期常态化→被静音→失效。
  比例把流量这个公共因子约掉了。
- **低流量误报防护（建议开）**：极小样本下单个僵尸就能把比例顶得很高
  （2 个任务里 1 个僵尸 = 50%）。低流量部署追加样本下限：

```yaml
    ...同上...
    ) > 0.01
    and sum(increase(ingest_task_duration_seconds_count[1h])) >= 10
```

- **空闲期不会误报**：全空时分子分母都缺席，表达式不产生样本；即使都为 0，`0/0 = NaN`
  而 `NaN > 0.01` 为假。但这也意味着"没流量"和"健康"在图上长得一模一样——这正是
  §0.1 元告警要兜的底。
- **处置**：先看 R2（是不是正常耗时的尾巴已经顶到收尸线），再决定是抬
  `task_zombie_timeout_s` 还是让任务变快。抬阈值前务必先看一眼是否只是某个介质在拖
  （`topk` 归因见 §3）。

### R2 P99 逼近收尸阈值 —— 在"该调阈值"之前报警

```yaml
- alert: IngestTaskP99ApproachingZombieThreshold
  expr: |
    histogram_quantile(
      0.99,
      sum by (le) (rate(ingest_task_duration_seconds_bucket[30m]))
    ) > 480
  for: 30m
  labels: {severity: warning, subsystem: ingest}
  annotations:
    summary: "任务耗时 P99 已达收尸阈值的 80%，正常尾巴开始和判死线重叠"
```

阈值取自 `task_zombie_timeout_s` 的 0.8 倍，按部署档位取值：

| Profile | `task_zombie_timeout_s` | 告警阈值（×0.8） |
|---|---|---|
| eco | 600 | **480** |
| standard | 600 | **480** |
| performance | 900 | **720** |

若运行时热调过 `task_zombie_timeout_s`，按实际值重算 `0.8 × 实际值`。

- **为什么是 0.8**：正常任务的 P99 一旦逼近判死线，"慢"与"死"就在同一区间里开始混淆，
  僵尸会增多。留 20% 余量是为了**在误判发生之前**给处置窗口。
- **分辨率提示**：480 落在 300~600s 桶之间，`histogram_quantile` 在该区间线性内插；
  触发点实际是"P99 越过 300s 并继续上行"。样本稀疏时插值抖动较大，故 `for: 30m`。
- **误读提示**：这是"该调阈值"的信号，**不是**"系统故障"的信号。

### R3 排队饱和 —— 派生于阶段拆分直方图

```yaml
- alert: IngestQueueSaturation
  expr: |
    (
      sum(rate(ingest_task_duration_seconds_count{outcome="queue_timeout"}[15m]))
      /
      sum(rate(ingest_task_duration_seconds_count[15m]))
    ) > 0.05
  for: 20m
  labels: {severity: warning, subsystem: ingest}
  annotations:
    summary: "抢不到槽而排队的任务占比超过 5%"
```

伴随指标（同一现象的早期信号，可作面板曲线而非告警）：

```promql
histogram_quantile(0.95, sum by (le) (rate(ingest_phase_duration_seconds_bucket{phase="queue"}[15m])))
```

- **处置方向**：扩容 `ingest_max_concurrency`（上限 8）或升档；先看 `_GUARDRAIL`
  是否因宿主机内存把并发压回了 1（此时加不起作用）。
- **务必排除的误读**：queue 与 pipeline 常常**一起涨**——槽被慢任务占着，排队自然变长。
  若 pipeline P95 同步抬升，真因是 ASR/Embedding 慢，**此时扩并发只会让更多任务同时
  抢 CPU，方向相反**。这就是把两个相位拆成两条直方图的全部理由。

### R4 阶段持续超软预算

```yaml
- alert: IngestPhaseBudgetExceeded
  expr: increase(ingest_phase_budget_exceeded_total[6h]) > 0
  for: 0m
  labels: {severity: info, subsystem: ingest}
  annotations:
    summary: "有阶段持续跑过软预算（task_phase_timeout_s），需要容量归因"
```

- 软预算**不是超时，也不杀任务**：它只是在每次阶段推进时回溯上一阶段耗时，超过就
  告警 + 计数。死亡判定归 `task_zombie_timeout_s` 与收尸。
- 意义在于归因：一眼看出慢的是 `transcribing`（ASR）还是 `indexing`（Embedding/写库）。

---

## 3. 归因速查（面板 / 排障即席查询）

```promql
# 哪类介质在被误杀
topk(3, sum by (media_type) (increase(ingest_tasks_zombie_total[24h])))

# 哪个阶段在超软预算
topk(3, sum by (phase) (increase(ingest_phase_budget_exceeded_total[24h])))

# 分介质的耗时 P99（看是不是某一类拖垮整体）
histogram_quantile(0.99, sum by (le, media_type) (rate(ingest_task_duration_seconds_bucket[30m])))

# 终态分布（确认 queue_timeout 是否真的被算进 _count）
sum by (outcome) (increase(ingest_task_duration_seconds_count[24h]))
```

---

## 4. 多副本与重启的算术注意

- **多副本**：Prometheus 自动附加 `job` / `instance` 标签，多副本时同一个指标会有 N 条重复序列；
  R1、R2 已用外层 `sum()` 聚合掉，自写查询时务必同样聚合，否则每个副本各算一份。
- **进程重启归零**：计数器只在进程内累积，重启即归零；`rate()` / `increase()` 会自动识别
  重置，不必特殊处理（但重启当窗口的比值会偏小，重标定时应跳过这些窗口）。
- **抓取间隔建议 ≤30s，别低于 15s**：每次抓取要跑一次收尸巡检 + O(序列数) 的文本渲染，
  都是同步动作，1C2G 上没有让它更密的预算。
- **"抓取正常但收尸静默失败"在 /metrics 上不可见**：收尸异常被 try/except 吞掉是有意为之
  （不能让旁路动作把 /metrics 打成 5xx，否则黄金指标和收尸会一起失明）。怀疑收尸没生效时，
  只能看服务端日志。
