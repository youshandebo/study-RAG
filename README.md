# AI Classroom Tutor · 课堂原法统一工作台

> **⚖️ 双重许可**：开源遵循 **AGPL-3.0**（网络分发/SaaS 必须开源全部代码）；闭源商用、私有化部署、OEM 贴牌需购买**商业授权**（梯度见[文末](#-商业授权与收费梯队)）。联系：**fennengxiong@qq.com**

> ⚠️ **免责声明**：AI 生成的解题步骤与教学内容仅供参考，可能存在错误，使用前请自行核验；详见 [LICENSE](LICENSE) 免责条款。

将 **拍照解题、老师原法 RAG、考点提炼、难度评级、易错预警、音画溯源、苏格拉底伴学、靶向自测、会话隔离、多模型分屏比对与多模态资产入库** 收敛至同一个统一对话工作台（Unified Omni-Chat Workspace）。

## 快速开始

```bash
# 1. 后端（无任何 API Key 也可运行，自动进入内置演示引擎模式）
cd backend
pip install -r requirements.txt
uvicorn app.main:app --port 8000

# 2. 前端
cd frontend
npm install
npm run dev            # http://localhost:3000
```

Docker 一键编排（含 Qdrant/Postgres/Redis/MinIO）：

```bash
cp .env.example .env
docker compose up -d                 # 基础版（backend + frontend + redis）
docker compose --profile full up -d  # 全量版（含向量库/关系库/对象存储）
```

### 🚀 单容器部署

```bash
docker run -d --name studay-rag --restart always   -p 3000:3000 -v studay-data:/app/data   ghcr.io/youshandebo/studay-rag:latest
```

浏览器打开 `http://服务器IP:3000`，**首次访问自动进入初始化向导**：网页里创建
管理员账号（旗舰档 + 同时解锁管理控制台），完成后学生从登录页注册使用。
数据（SQLite + 配置 + 素材）全部落在 `studay-data` 卷，重启/升级不丢。
需要更强检索与多副本时，再用下面的 compose 全栈编排（Postgres/Qdrant/Redis）。

### 🚀 服务器一键部署（镜像由 GitHub Actions 云端构建，服务器免装构建环境）

每次 push 到 `main`，[Docker 工作流](.github/workflows/docker.yml) 自动构建前后端镜像并推送至 GHCR。服务器上：

```bash
# 1. 拉取仓库（或仅复制 docker-compose.prod.yml 与 .env）
git clone https://github.com/youshandebo/studay-RAG.git && cd studay-RAG

# 2. 配置环境（务必改掉 POSTGRES/MINIO 弱口令，设置 ADMIN_PASSWORD 与 ADMIN_JWT_SECRET）
cp .env.example .env && vi .env

# 3. 拉取云端构建好的镜像并启动全栈（backend/frontend/postgres/redis/qdrant/minio）
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d

# 升级版本：git pull 后重复 pull + up -d（数据全在具名卷中，不会丢失）
```

- 访问 `http://<服务器IP>:3000`；API 经前端 `/api/proxy` 同源转发，无 CORS 问题
- **端口冲突**：对外端口完全由你定——`.env` 里 `FRONTEND_PORT=任意空闲端口`
  后重启即可（容器内固定 3000，外面随你换）。backend 只绑 127.0.0.1 回环
  不对公网暴露。建议流程：`ss -tlnp | grep -E ':3000|:8080'` 先确认空闲再定；
  要绑域名 + HTTPS 建议前面加一层 Caddy/Nginx，让 80/443 归它管
- 数据持久化：Postgres/Redis/Qdrant/MinIO 各自具名卷；`backend-data` 卷保存
  runtime_config 与 JWT 密钥（勿删，否则已签发登录态失效）
- GHCR 包默认私有：仓库 Settings → Packages 可改 Public，或在服务器
  `docker login ghcr.io` 后再 pull
- **监控端点只应内网可达**：`/metrics` 挂在后端**根路径**（不在 `/api/v1` 前缀下），
  按设计无鉴权，后端本身也只绑 127.0.0.1 回环。前端 `/api/proxy` 是全路径转发，
  已加闸拒绝 `..` 穿越并显式拒绝 `/metrics`、`/docs`、`/redoc`、`/openapi.json`
  等运维路径（由 `backend/tests/test_proxy_guard.py` 钉住，删掉会红）。
  若你在前面又加了一层 Nginx/Caddy 反向代理，请同样只放行 `/api/` 业务路径，
  **不要把后端 8000 端口直接对外暴露**；Prometheus 抓取建议走内网或 SSH 隧道
- `NEXT_PUBLIC_API_BASE` 已在云端构建时固定为 `/api/proxy`，无需配置

## 模型接入

**方式一：管理后台（推荐）** —— 访问 `http://localhost:3000/admin`（或点击工作台右上角 ⚙️ 管理后台），登录后在面板中在线配置以下五类模型，保存即时热生效、无需重启，配置持久化于 `backend/data/runtime_config.json`：

| 配置分区 | 用途 | 协议 |
| --- | --- | --- |
| 大语言模型（LLM） | 对话/解题主脑 | OpenAI 兼容 或 Anthropic |
| 嵌入模型 | 切片与提问向量化检索 | OpenAI 兼容 `/embeddings` |
| 语音转文字模型 | 课堂录音转录 | OpenAI 兼容 `/audio/transcriptions` |
| 多模态识图模型 | 板书/题目照片 OCR | OpenAI 兼容视觉端点 / Anthropic |
| 重排模型（Rerank） | 二阶段精排，提升首屏命中率 | Jina / SiliconFlow / Cohere |

- 默认管理员口令 `admin123`（可用环境变量 `ADMIN_PASSWORD` 覆盖），首次登录后请在「管理员密码」分区修改。
- 面板内每类模型均有「测试连通性」按钮；提交掩码 Key = 保持不变，清空 = 回落 `.env` 环境变量默认。
- 替换嵌入模型后，旧向量维度不再匹配，请重新上传素材以重建切片向量。

**方式二：环境变量**（首次部署/容器编排常用）——所有模型调用点均为 Provider 抽象层，配置任一 Key 即自动切换真实 API，未配置时回落本地流式演示引擎：

| 环境变量 | 服务商 |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI 及兼容网关 |
| `ANTHROPIC_API_KEY` | Claude 系列 |
| `DASHSCOPE_API_KEY` | 通义千问（含 Qwen-VL 板书识别） |
| `DEEPSEEK_API_KEY` | DeepSeek |
| `RERANK_API_BASE` / `RERANK_API_KEY` | 二阶段精排（可选，见下节） |
| `MULTI_TENANT_MODE` | 多租户隔离开关（可选，见下节；默认关闭） |
| `ADMIN_PASSWORD` | 管理员初始口令（默认 admin123） |

## 检索：多路召回与混合打分

检索分两阶段：**召回**（找出候选）→ **打分**（排出顺序）。

### 召回：Dense 与 Sparse 双路并集

| 路 | 信号 | 擅长 |
| --- | --- | --- |
| Dense | 向量余弦（嵌入模型） | 语义泛化、同义改写、口语提问 |
| Sparse | BM25 倒排 | 术语精确命中、生僻词、公式/编号 |

两路各自独立召回 Top-N，**并集**进候选池。为什么不是"只召回向量、再用 BM25 重排"：
加权公式里的 BM25 项只对**已在池中**的切片有值——向量漏掉的关键切片永远拿不到分，
也永远进不了结果。**混合重排必须先有混合召回**，否则关键词通道只是装饰。

### 中文分词：为什么 BM25 之前形同虚设

倒排索引依赖分词。`str.isalnum()` 对汉字返回 True，若按"连续字母数字段"切分，
`梯度下降的学习率衰减策略` 会变成**一整个 token**，与查询 `学习率衰减` 永不相等：

```
旧口径  查询 ['学习率衰减']   文档 ['梯度下降的学习率衰减策略']   交集 []        ← 通道恒失效
bigram  查询 ['学习','习率','率衰','衰减']  文档 […'学习','习率','率衰','衰减'…]  交集 4 个 ✓
```

因此 BM25 与词面通道统一改用**字符 bigram**（中文）+ **整词**（英文，bigram 会把
`gradient` 切碎反而降低区分度）。零新依赖、无需词典，对未登录词天然友好。

夹具实测（`scripts/measure_recall_mrr.py`）：**粗排 MRR 0.7333 → 0.8000**。

> IDF 刻意保持**全库统计**而非租户内统计：租户语料小的时候，某个词在一两份文档里
> 出现就会让 IDF 剧烈失真，"普遍出现的词"被算成"罕见词"从而被过度加权。

### 打分：RRF 融合（倒数排名融合）

候选池内的多路信号用 **RRF** 合成，而不是加权求和：

```
RRF(d) = Σ_m  w_m / (k + rank_m(d))          k = 60（Cormack et al. 2009）
```

**为什么不用加权和**：`0.5·向量 + 0.2·词面 + 0.2·BM25` 要求三路分值**量纲可比**，
但实际并非如此——长查询的 BM25 天然偏高、短查询的词面重叠率天然偏高，同一个
权重在不同长度的提问下含义不同。BM25 长期失效时这个问题被掩盖（另两路也恒为 0），
一旦稀疏通道真正通电就立刻暴露：**排序被高频词覆盖但答意稍偏的切片挤占**。

RRF 只看名次、不看分值，等比缩放任何一路的分数都不改变结果——不需要为不同
场景手工配平权重。夹具实测 **MRR 0.80（加权和）→ 0.90（RRF）**。

> **语义调制（不可省略）**：纯 RRF 有个致命副作用——任何查询都会有一个
> "第一名"，哪怕全库都与它无关。实测 65 条候选里 41 条越过 0.42 阈值，
> `min_score` / `GENERAL_RELEVANCE_FLOOR` 这类**绝对阈值**彻底失效。
> 因此在 RRF 分上乘一个语义调制因子，把"名次"重新锚回"绝对相关性"：
> `base = rrf_norm × (0.5 + 0.5 × max(0, cos))`。锚点 0.5 是在
> 「MRR 不变的平台区」内选的中性值（实测 0.85~0.2 区间 MRR 恒为 0.90）。

排序完成后施加**时间衰减**（仅非定版切片，定版不随时间贬值）与**版本去重**
（`supersedes` 链上的旧解法直接出局，避免 LLM 串戏）。

`retrieval.fusion` 可切回 `weighted`（加权和）；权重仍沿用 `strict` / `balanced` /
`explore` 三档预设并支持后台逐项覆盖，热生效。改完用
`python scripts/measure_recall_mrr.py` 量化，别凭感觉。

## 二阶段精排（Rerank）

召回之后再精排：由重排模型做交叉注意力打分，最后经**门控权威加权**决定最终顺序：

```
S_final = S_sem × (1 + β)   if  is_canonical 且 S_sem ≥ τ
S_final = S_sem             否则
```

**为什么是门控而不是让重排模型直接决定排序**：教学场景学生提问口语化，未校准的
交叉注意力会偏好口语重合词多的原始课堂切片，把措辞凝练的教师定版答案挤出首屏。
因此"相关度判断"（重排模型）与"可信度赋权"（定版门控）严格解耦——重排只回答
"有多相关"，永远不决定"哪条是权威解法"。

**为什么走远程 API 而不是本地 ONNX**：本项目面向单机小内存 VPS 部署，本地
Cross-Encoder 需要几百 MB 权重 + 常驻内存 + CPU 推理，与多租户运营目标冲突。
重排是**可选增强**（不配也能跑），必须廉价、可关、可降级。

| 项 | 说明 |
| --- | --- |
| 支持协议 | `jina`（默认）/ `siliconflow` / `cohere` |
| 依赖 | 仅 `httpx`（已在基础依赖内），**无需** onnxruntime / transformers |
| 超时 | 默认 8 秒，可在面板调整（1–60） |
| 失败行为 | 网络异常/限流/超时 → **静默回退粗排顺序**，绝不阻断问答 |
| 未配置时 | 走 `NoopReranker`，行为与引入精排前完全一致 |

配置方式三选一：管理后台「检索设置」填写；或环境变量 `RERANK_API_BASE` /
`RERANK_API_KEY`；或离线标定脚本用 `--api-base` / `--api-key` 一次性指定。

> τ / β 不要凭感觉设。仓库提供真实打分取证 + 网格标定两段式工具链：
> `scripts/extract_rerank_dataset.py`（真实召回 → 真实打分 → 弱标注）
> → `scripts/eval_rerank_params.py`（网格搜索 + 可解性诊断）。
> 标定脚本会检测 `τ` 是否**可辨识**——若对抗样本未落入模糊带，会明确
> 报告"τ 不可标定"，而不是输出一个看似合理的假阈值。

## 额度计费

配额（`membership.py`）管"能存多少 / 能多快问"，计费（`core/billing.py`）管
"这一次消耗多少额度"，两者正交。

计费采用**预冻结 + 终态结算**，请求开始冻结上限额度，流结束后按**实际有效
产出**结算；低于 50 token 视为未成功交付，全额退回。任何一次请求的额度只会
落入三种终态之一且只落一次：`settled` / `released` / `expired`。

这样设计是为了避开几类典型事故：

| 风险 | 对策 |
| --- | --- |
| 流式中断：预扣未退 **或** 后扣被白嫖 | 结算写在 `finally`，四条退出路径一视同仁 |
| 多模型比对：N 倍成本只扣 1 次 | 冻结与限流均按轨道数（`units`）放大 |
| 一次提问含多道题 | 计费单位可表意多题，与意图解耦 |
| 重试重复扣费 / 崩溃悬挂扣费 | `request_id` 幂等键 + 冻结 TTL 超时自动全额退回 |
| 多副本并发下超额 | 配置 `REDIS_URL` 后走 Redis 原子扣减；未配置则进程内锁 |

> 配额单位为抽象额度，`ASK_HOLD_AMOUNT` / `TRACK_HOLD_AMOUNT` 是当前保守口径。
> 接入真实计价表时只需替换这两个常量与 `settle()` 里的折算公式。

## 多租户隔离

知识库有两层作用域，**租户在外、课程在内**：

```
tenant_id  →  course_id  →  chunk
（机构边界）   （课程边界）
```

### 为什么 course_id 不够

`course_id` 是客户端的表单参数（默认 `"default"`）。它隐含假设"能说出这个
course_id 的人都已经过授权"。在单机构部署里成立；在 SaaS 多租户场景下不成立——
租户 A 只要在请求里填上租户 B 的 `course_id` 就能读到 B 的知识库。

**授权边界必须是服务端权威、客户端无法指定的一等公民**，这一层就是 `tenant_id`。

### 三条不可越狱规则

1. **来源权威**：`tenant_id` 只从签名 JWT → 用户记录解析，**绝不**读请求体、
   查询参数、请求头或 Cookie 里的任何租户字段。
2. **注入靠下**：过滤发生在数据层——Qdrant 走 `must` 硬过滤，内存向量库走等价的
   子集收窄，BM25 走租户分区。检索层**根本看不到**跨租户数据，而不是"查出来再筛"
   （后者一旦某个分支漏判就是数据泄漏）。
3. **失败关闭**：多租户模式开启但解析不出租户时收窄到默认租户，而非放宽为全库。

### 覆盖范围

| 层 | 隔离点 |
| --- | --- |
| 召回 | Qdrant payload filter / 内存库子集收窄（Dense 与 Sparse 两路都过滤） |
| 打分 | BM25 只对租户内文档计分 |
| 上下文装箱 | 邻近扩展的骨架限本租户（否则会把他人内容拼进本题上下文） |
| 证据链 | `/evidence/*`（含音频切片流）按租户收窄——这里泄漏的是**原始录音** |
| 定版管理 | `set_canonical` 不跨租户降级他人定版（写越权防护） |
| 写入 | `ingest` 的租户由服务端注入，客户端表单无法影响 |

### 开启步骤

```bash
# 1) 先给存量切片打标（未打标的数据在开启后会不可见——这是有意的 fail-closed）
python scripts/migrate_tenants.py --dry-run     # 先看统计
python scripts/migrate_tenants.py               # 执行

# 2) 开启开关并重启后端
MULTI_TENANT_MODE=1

# 3) 在管理后台为每个用户分配租户：
#    POST /api/v1/admin/users/{user_id}/tenant  {"tenant_id": "org-a"}
```

**默认关闭**：未设置 `MULTI_TENANT_MODE` 时所有数据归入 `public` 单租户，
行为与引入前逐位一致，存量部署零影响。

> 刻意**不**做管理后台热开关：租户是隔离开关，运行中切换会让已有数据的归属语义
> 在请求之间突变（前一个请求按 public 写入、后一个按租户读取）。切换应当是受控的部署动作。

## 🖼️ 界面预览

**统一工作台**——左会话栏（搜索/隔离）+ 中全能对话 + 右证据抽屉：

![统一工作台](docs/screenshots/01-workspace.png)

**解题卡与音画证据链**——折叠胶囊时间戳徽章，点开即看波形回放、KaTeX 转录与板书定位：

![证据抽屉](docs/screenshots/02-evidence-drawer.png)

**板书 Pan & Zoom**——滚轮缩放（右上角实时倍率）、拖拽平移，细小角标看得清：

![板书缩放](docs/screenshots/03-board-zoom.png)

**管理员控制台**——黑板报登录 + 概览仪表盘（引擎模式/知识库统计/模型接入状态）：

<p align="center">
  <img src="docs/screenshots/04-admin-login.png" width="49%" alt="管理员登录">
  <img src="docs/screenshots/05-admin-console.png" width="49%" alt="管理员控制台">
</p>

**媒体压缩配置**——图片质量/最大分辨率/音频码率/体积上限，滑杆即调即生效：

![媒体压缩配置](docs/screenshots/06-admin-media.png)

---

## 功能矩阵

| 功能 | 触发方式 | 表现 |
| --- | --- | --- |
| 拍照原法解题 | 拖拽/粘贴题目图片 | OCR 提取公式 → 检索课堂切片 → 老师原法推导 + 考点徽章 + 难度星级 |
| 易错预警 | 随解题自动生成 | 推导下方高亮插入琥珀色警示卡 |
| 音画证据溯源 | 点击 🔊/🖼️ 证据标签 | 右侧抽屉滑出：波形播放 + 板书原图 + 原声转录 |
| 苏格拉底伴学 | 「教我 / 没懂」或操作条 | 分步设问状态机 + 渐进提示 + 作答评估 + 掌握度累计 |
| 靶向自测 | 「考我」或操作条 | 调取易错陷阱生成变式题，内嵌选项即时批改与归因 |
| 多模型分屏比对 | 「对比」或操作条 | 原地 2~4 栏并发流式渲染各模型解法 |
| 会话隔离 | 左侧栏切换 | sessionId 严格隔离 + **本地 IndexedDB 按账号分区**（owner 索引；登出/登录态失效自动重置本地视图，共享设备上账号间互不可见），持久化 |
| 多模态入库 | 右抽屉「入库」页签 | 录音/板书/文字粘贴上传 → ASR/VLM/切片 → 洞察提炼 → 向量入库；**任务化执行**（提交即返句柄、真实阶段进度、幂等去重、进程重启自动判死不悬挂） |
| 管理员后台 | `/admin` 或顶栏 ⚙️ | 四类模型在线配置（热生效）+ 连通性测试 + 口令管理 + 知识库统计 |
| 全局 RAG 问答 | 直接向 AI 提问 | 普通提问自动检索知识库，命中切片注入上下文并挂载证据链 |

## SSE 事件协议

`POST /api/v1/chat/stream`

```
event: meta         { message_id, intent }
event: evidence     { list: EvidenceRef[] }
event: delta        { text }                       # 主叙述流式增量
event: track_delta  { index, model_name, text }    # 多模型分轨增量
event: track_done   { index, model_name }          # 分屏单轨完成（比对路径）
event: usage        { usage: UsageInfo }           # 每轮用量（tokens/耗时/上下文占比）
event: error        { message }                   # 流中途服务端错误（连接未断、回答不完整）
event: card         PolymorphicMessage             # 最终多态卡片
event: done         {}
```

## 目录结构

```
backend/   FastAPI + 意图路由 + RAG(多路召回/切片/对齐/RRF 融合/精排) + 苏格拉底/出题/批改 Agent + 入库流水线
  migrations/   Alembic 迁移历史（可选依赖，见「数据库迁移」）
frontend/  Next.js 14 + Zustand 四状态机 + Dexie 离线库 + 多态卡片渲染 + KaTeX
scripts/   检索质量度量 / 重排参数标定 / 租户迁移
docker-compose.yml   一键编排
```

## 数据库迁移

两条路径并存，按部署成熟度选用：

| 路径 | 依赖 | 适用 |
| --- | --- | --- |
| **内置自动建表**（默认） | 无 | 单机 / 演示 / 快速启动 |
| **Alembic**（可选） | `pip install alembic` | 团队协作 / 需要可追溯、可回滚的 schema 历史 |

**内置路径**：`relational._ensure_tables()` 启动时 `create_all` 建缺失的表，
再按 `_SCHEMA_PATCHES` 幂等补列（`create_all` **不会**给已存在的表加列——
这是升级时最容易踩的坑）。列是否存在用 SQLAlchemy inspector 探测，
而不是"捕获异常当成功"，后者会把权限/连接类真实错误一并吞掉。

**Alembic 路径**：

```bash
cd backend
alembic upgrade head                    # 升到最新
alembic revision -m "描述"               # 新建 revision
alembic revision --autogenerate -m "描述" # 按模型差异自动生成
alembic downgrade -1                    # 回滚一步
alembic current / alembic history        # 查看状态
```

连接串**不在** `alembic.ini` 里维护，由 `migrations/env.py` 从项目统一配置解析
（`ALEMBIC_DATABASE_URL` > `POSTGRES_DSN` > 本地 SQLite），避免同一份信息两处漂移。
库是异步驱动（`sqlite+aiosqlite` / `postgresql+asyncpg`），env.py 走
`async_engine_from_config` + `run_sync`；SQLite 下启用 `render_as_batch`
（它不支持大多数 ALTER，batch 模式会重建表来模拟）。

**存量库接入**（表已由 `create_all` 建好，可能已含新列）：

```bash
alembic stamp head      # schema 已一致，直接记录为最新版本
```

> ⚠️ `alembic.ini` **必须保持纯 ASCII**。Alembic 用 `configparser` 以
> **locale 编码**读取它，中文 Windows 上 locale 是 GBK——配置里出现任何
> UTF-8 多字节字符，所有 alembic 命令都会直接 `UnicodeDecodeError`。
> 中文说明放在本文件与 `migrations/env.py`（Python 文件显式声明 UTF-8）。
> 该约束由 `test_alembic_ini_is_pure_ascii` 守住。

迁移历史：`0001` 基线四表（sessions / users / messages / assets）→
`0002` users 增加 `tenant_id`（多租户隔离）。新列一律作为**增量 revision**，
不塞进基线——否则存量库无法对齐。

## 兜底策略一览

- 无 LLM Key → 内置课堂剧本演示引擎（流式 token 化输出）
- 无嵌入 Key → 哈希兜底向量（降级模式：**写路径跳过向量入库、读路径跳过
  dense 通道**，判据是嵌入器自报的 degraded 状态而非向量维度——主检索空间
  永不被不同源向量污染，服务恢复后自动回到正常双路召回）
- 无 Qdrant → 进程内余弦向量索引
- 无 Postgres → 内存仓储
- 无 Whisper → 内置带毫秒时间戳的演示转录
- **无 Rerank API → 跳过精排，直接沿用粗排顺序**（零外部依赖，且不含任何本地模型推理）
- **未开启多租户 → 单租户 `public`**，行为与引入隔离前逐位一致；开启前需先跑 `scripts/migrate_tenants.py`
- **未装 alembic → 内置自动建表 + 幂等补列**（`create_all` + `ALTER TABLE` 探测），迁移能力零依赖可用
- **持久化为真实可选**：`POSTGRES_DSN` 需先 `pip install asyncpg "sqlalchemy[asyncio]>=2.0"`，`QDRANT_URL` 需先 `pip install qdrant-client`；未安装驱动/未配置时自动降级进程内存储（重启即失，部署多副本必须配置）
- 无 MinIO → 本地静态目录托管
- 无 Redis → 限流与计费走进程内实现（单副本正确；多副本需配置 `REDIS_URL` 以共享窗口与账本）

---

## 💼 商业授权与收费梯队

AGPL-3.0 义务不适合你的场景？购买商业授权即可豁免开源义务、移除署名并获得长期支持：

| 梯队 | 适用对象 | 包含权益 | 定价 |
| --- | --- | --- | --- |
| **社区版** | 个人学习 / 开源爱好者 | 免费使用全部功能，遵循 AGPL-3.0 义务（保留署名、网络分发开源） | ¥0 |
| **机构单实例版** | 学校 / 培训机构（单校区） | 私有化部署豁免开源 · 去除署名徽标 · 一年更新与工单支持 | ¥3,999 / 实例 / 年 |
| **SaaS 运营版** | 在线教育平台 | 多租户运营豁免 AGPL 网络条款 · 商用级 SLA · 版本随行升级 | ¥19,999 / 年 起 |
| **OEM 贴牌买断版** | 厂商二次分发 | 永久买断 · 品牌完全替换 · 源码级定制排期 · 法务合规包 | 面议 |

📩 **商务合作**：fennengxiong@qq.com（注明场景与规模，工作日 24h 内回复）

## 📄 许可与第三方

- 本项目代码：AGPL-3.0-or-later 或 商业授权（二选一），完整条款见 [LICENSE](LICENSE) 与 [LICENSES/AGPL-3.0.txt](LICENSES/AGPL-3.0.txt)
- 引用的开源模型/组件（Whisper MIT、Qwen Apache-2.0、Qdrant Apache-2.0 等）各自以其随附许可为准，详见 LICENSE 第三方声明
