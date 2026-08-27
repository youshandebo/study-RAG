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

**方式一：管理后台（推荐）** —— 访问 `http://localhost:3000/admin`（或点击工作台右上角 ⚙️ 管理后台），登录后在面板中在线配置以下四类模型，保存即时热生效、无需重启，配置持久化于 `backend/data/runtime_config.json`：

| 配置分区 | 用途 | 协议 |
| --- | --- | --- |
| 大语言模型（LLM） | 对话/解题主脑 | OpenAI 兼容 或 Anthropic |
| 嵌入模型 | 切片与提问向量化检索 | OpenAI 兼容 `/embeddings` |
| 语音转文字模型 | 课堂录音转录 | OpenAI 兼容 `/audio/transcriptions` |
| 多模态识图模型 | 板书/题目照片 OCR | OpenAI 兼容视觉端点 / Anthropic |

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
| `ADMIN_PASSWORD` | 管理员初始口令（默认 admin123） |

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
| 多模态入库 | 右抽屉「入库」页签 | 录音/板书/文字粘贴上传 → ASR/VLM/切片 → 洞察提炼 → 向量入库 |
| 管理员后台 | `/admin` 或顶栏 ⚙️ | 四类模型在线配置（热生效）+ 连通性测试 + 口令管理 + 知识库统计 |
| 全局 RAG 问答 | 直接向 AI 提问 | 普通提问自动检索知识库，命中切片注入上下文并挂载证据链 |

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
