# AI Classroom Tutor · 课堂原法统一工作台

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

## 模型接入

所有模型调用点均为 Provider 抽象层，配置任一 Key 即自动切换真实 API，未配置时回落本地流式演示引擎：

| 环境变量 | 服务商 |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI 及兼容网关 |
| `ANTHROPIC_API_KEY` | Claude 系列 |
| `DASHSCOPE_API_KEY` | 通义千问（含 Qwen-VL 板书识别） |
| `DEEPSEEK_API_KEY` | DeepSeek |

## 功能矩阵

| 功能 | 触发方式 | 表现 |
| --- | --- | --- |
| 拍照原法解题 | 拖拽/粘贴题目图片 | OCR 提取公式 → 检索课堂切片 → 老师原法推导 + 考点徽章 + 难度星级 |
| 易错预警 | 随解题自动生成 | 推导下方高亮插入琥珀色警示卡 |
| 音画证据溯源 | 点击 🔊/🖼️ 证据标签 | 右侧抽屉滑出：波形播放 + 板书原图 + 原声转录 |
| 苏格拉底伴学 | 「教我 / 没懂」或操作条 | 分步设问状态机 + 渐进提示 + 作答评估 + 掌握度累计 |
| 靶向自测 | 「考我」或操作条 | 调取易错陷阱生成变式题，内嵌选项即时批改与归因 |
| 多模型分屏比对 | 「对比」或操作条 | 原地 2~4 栏并发流式渲染各模型解法 |
| 会话隔离 | 左侧栏切换 | sessionId 严格隔离，IndexedDB 持久化 |
| 多模态入库 | 右抽屉「入库」页签 | 录音/板书上传 → ASR/VLM → 切片对齐 → 洞察提炼 → 向量入库 |

## SSE 事件协议

`POST /api/v1/chat/stream`

```
event: meta         { message_id, intent }
event: evidence     { list: EvidenceRef[] }
event: delta        { text }                       # 主叙述流式增量
event: track_delta  { index, model_name, text }    # 多模型分轨增量
event: card         PolymorphicMessage             # 最终多态卡片
event: done         {}
```

## 目录结构

```
backend/   FastAPI + 意图路由 + RAG(向量化/切片/对齐/混合检索) + 苏格拉底/出题/批改 Agent + 入库流水线
frontend/  Next.js 14 + Zustand 四状态机 + Dexie 离线库 + 多态卡片渲染 + KaTeX
docker-compose.yml   一键编排
```

## 兜底策略一览

- 无 LLM Key → 内置课堂剧本演示引擎（流式 token 化输出）
- 无 Qdrant → 进程内余弦向量索引
- 无 Postgres → 内存仓储
- 无 Whisper → 内置带毫秒时间戳的演示转录
- 无 MinIO → 本地静态目录托管
