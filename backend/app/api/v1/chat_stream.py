# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""统一对话接口：意图识别 + SSE 统一分发输出。

SSE 事件协议（前端 lib/api.ts 按此解析）：
  meta        -> { message_id, intent }
  delta       -> { text }                      主叙述流式增量
  card        -> PolymorphicMessage            最终多态卡片
  track_delta -> { index, model_name, text }   多模型分轨增量
  evidence    -> { list[EvidenceRef] }         音画证据链
  done        -> {}
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

import app.db.relational as repo
from app.api.v1.auth import AuthUser, current_user_optional
from app.core.billing import DailyCapExceeded, account_key, get_ledger
from app.core.circuit import LLMUnavailable
from app.core.membership import daily_cap_for, plan_for
from app.core.security import SlidingWindowLimiter
from app.core.tenancy import DEFAULT_TENANT, multi_tenant_enabled, resolve_tenant
from app.services.llm.provider import get_tier_provider
from app.models.domain import (
    ChatRequest,
    ComparePayload,
    CompareTrack,
    EvidenceRef,
    Intent,
    MessageType,
    PolymorphicMessage,
    QuizPayload,
    SolvePayload,
    UsageInfo,
)
from app.core.config import get_settings
from app.services.agent.quiz_generator import QuizGenerator, correct_option_index, grade_objective
from app.services.agent.socratic_tutor import SocraticTutor
from app.services.extractor import difficulty as difficulty_svc
from app.services.extractor.pitfall import extract_from_chunks
from app.services.rag import packer
from app.services.rag.chunker import Chunk
from app.services.rag.retriever import get_retriever
from app.services.llm.mock_engine import (
    SOLVE_PITFALLS,
    SOLVE_STEPS,
)
from app.services.llm.provider import get_provider
from app.services.router.intent_classifier import classify
from app.services.tokens import estimate_tokens

router = APIRouter()
_logger = logging.getLogger("app.api.chat_stream")

# 流式对话限流：按会员档位分配速率（SSE 单次连接计数一次）。
#
# key 用 IP + 用户身份，**不能用 req.session_id**——session_id 由客户端
# 自由生成，攻击者每次换一个 id 就能把限流窗口刷成全新计数，限流形同虚设。
# 匿名用户无 id 时退化为纯 IP 键（与计费账户口径一致）。
_tier_limiters: dict[str, SlidingWindowLimiter] = {}


def _limiter_key(ip: str, user_id: str | None) -> str:
    return f"{ip}:{user_id}" if user_id else f"ip:{ip}"


def _limiter_for(tier: str) -> SlidingWindowLimiter:
    if tier not in _tier_limiters:
        _tier_limiters[tier] = SlidingWindowLimiter(
            max_events=int(plan_for(tier).get("chat_per_min", 10)), window_seconds=60
        )
    return _tier_limiters[tier]


# 单次问答的预冻结额度上限（额度单位）。真实计价表接入后替换此常量即可。
ASK_HOLD_AMOUNT = 4000

SOLVE_SYSTEM = (
    "你是一位大学课堂的专属助教。请严格依据提供的课堂切片（老师原话与板书）所体现的解法和口吻来解题，"
    "步骤清晰、使用 LaTeX 公式，并在结尾标注所用课堂方法。"
)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _retrieve_evidence(
    query: str,
    top_k: int | None = None,
    min_score: float | None = None,
    course_id: str | None = None,
    retrieval_mode: str = "lecture",
    time_alpha_override: float | None = None,
    canonical_bonus_override: float | None = None,
    chapter: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> tuple[list[EvidenceRef], list]:
    # 切片数由部署档位驱动（eco=3 / standard=5 / performance=8），
    # 调用方不传时跟随档位；显式传入的 top_k 优先（如考试评分等固定口径）。
    if top_k is None:
        from app.core import profiles

        top_k = int(profiles.effective()["final_top_k"])
    retriever = await get_retriever()
    chunks = await retriever.retrieve(
        query, top_k=top_k, min_score=min_score, course_id=course_id or None,
        retrieval_mode=retrieval_mode, with_context_window=False,
        time_alpha_override=time_alpha_override,
        canonical_bonus_override=canonical_bonus_override,
        chapter=chapter or None,
        tenant_id=tenant_id,
    )
    refs = [
        EvidenceRef(
            audio_id=c.audio_id,
            timestamp_range=(c.start, c.end),
            audio_snippet_url=f"/api/v1/evidence/audio/{c.id}",
            board_image_url=f"/static/boards/board_{(c.board_index or 1):02d}.svg",
            transcript_snippet=c.text[:80],
            board_caption=c.board_caption,
        )
        for c in chunks
    ]
    return refs, chunks


# 普通对话注入知识库上下文的最低融合相关性分（检索得分 = 0.62向量 + 0.28词面 + 元数据加成）
GENERAL_RELEVANCE_FLOOR = 0.42


def _is_relevant(query: str, chunk) -> bool:
    """轻量二次判定：查询词与切片文本存在实词交叠即视为相关。

    用 bigram 口径（与 BM25 一致）——`_tokenize` 对中文会切出整句单 token，
    交集恒为空，等于该判定永远返回 False。
    """
    import app.services.rag.embedder as emb

    qtokens = {t for t in emb.bm25_tokenize(query) if len(t) >= 2}
    if not qtokens:
        return False
    hay = set(emb.bm25_tokenize(chunk.text)) | set(emb.bm25_tokenize(chunk.exam_point or ""))
    return bool(qtokens & hay)


async def _history_messages(session_id: str, max_turns: int = 6) -> list[dict]:
    """最近 N 轮 user/assistant 对话 → LLM messages。

    多轮上下文贯通：此前每个意图调 LLM 都是拿当前一条消息现造 messages 数组，
    "再讲讲第二步"这类追问模型根本接不住。此处取历史（调用方在 append 当前
    消息之前调用），每条截断防爆 token。
    """
    try:
        msgs = await repo.list_messages(session_id)
    except Exception:
        return []
    turns: list[dict] = []
    for m in reversed(msgs):
        if len(turns) >= max_turns * 2:
            break
        role = m.get("role")
        content = str(m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        turns.append({"role": role, "content": content[:2000]})
    turns.reverse()
    return turns


async def _pack_for_prompt(
    chunks: list[Chunk], system_prompt: str, query: str, history: list[dict],
    tenant_id: str = DEFAULT_TENANT,
) -> list[Chunk]:
    """按模型上下文预算装箱切片；失败时原样返回（装箱是优化，不该拖垮主链路）。"""
    if not chunks:
        return chunks
    try:
        limit = _context_limit()
        budget = packer.compute_budget(limit, system_prompt, query, history)
        # 骨架用于邻近切片扩展：必须限本租户，否则扩展出的"邻居"
        # 会把别的机构内容拼进本题上下文（比检索泄漏更隐蔽）
        skeleton = {c.id: c for c in await (await get_retriever()).all_chunks(tenant_id=tenant_id)}
        result = packer.pack_context(
            [(1.0 - i * 1e-6, c) for i, c in enumerate(chunks)],  # 保留检索顺序作为优先级
            budget,
            skeleton=skeleton,
        )
        if result.dropped:
            _logger.info(
                "上下文装箱：预算 %d token，入选 %d 条（扩展 %d 条），丢弃 %d 条",
                result.budget, len(result.chunks), len(result.expanded), result.dropped,
            )
        return result.chunks
    except Exception as exc:
        _logger.warning("上下文装箱失败，回退原始切片列表: %s", exc)
        return chunks


def _user_query(req: ChatRequest, ocr_text: str | None, history: list[dict]) -> str:
    """当前问题：原文 > OCR 识别 > 最近一轮提问 > 演示兜底。"""
    if req.text.strip():
        return req.text.strip()
    if ocr_text:
        return ocr_text
    for m in reversed(history):
        if m.get("role") == "user":
            return str(m.get("content") or "")
    return "反常积分收敛性判定"


def _context_limit() -> int:
    """上下文容量上限：读 CONTEXT_LIMIT 配置（默认 128K），换模型时同步调整即可。"""
    try:
        return max(1024, int(get_settings().context_limit))
    except (TypeError, ValueError):
        return 131072


async def _build_usage(session_id: str, system_text: str, retrieval_text: str, output: str, duration_ms: int) -> UsageInfo:
    """估算本轮 token 用量与上下文容量占比（无真实 tokenizer，按中英字重近似）。"""
    hist = sum(
        estimate_tokens(str(m.get("content", ""))) for m in await repo.list_messages(session_id)
    )
    sys_t = estimate_tokens(system_text)
    ret_t = estimate_tokens(retrieval_text)
    out_t = estimate_tokens(output)
    other = 40
    inp = sys_t + hist + ret_t + other
    # 演示口径的伪缓存命中率：每会话固定档位，展示用
    seed = int(hashlib.md5(session_id.encode()).hexdigest()[:4], 16)
    cache_rate = round(0.88 + (seed % 900) / 10000, 3)
    return UsageInfo(
        input=inp,
        output=out_t,
        total=inp + out_t,
        duration_ms=max(1, duration_ms),
        cache_hit_rate=cache_rate,
        context_used=inp,
        context_limit=_context_limit(),
        context_breakdown=[
            {"label": "消息", "tokens": hist},
            {"label": "系统提示词", "tokens": sys_t},
            {"label": "检索上下文", "tokens": ret_t},
            {"label": "回复输出", "tokens": out_t},
            {"label": "其他", "tokens": other},
        ],
    )


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request, user: AuthUser = Depends(current_user_optional)) -> StreamingResponse:
    ip = request.client.host if request.client else "unknown"
    # 会员会话隔离：注册用户只能在自己绑定的会话里提问
    if not user.anonymous:
        owner = await repo.get_session_owner(req.session_id)
        if owner and owner != user.id:
            raise HTTPException(status_code=403, detail="无权在该会话中提问")
    # 档位限速：key 必须绑定不可伪造的身份（IP + 用户 id），不能绑 session_id
    key = _limiter_key(ip, None if user.anonymous else user.id)
    limiter = _limiter_for(user.tier)
    if not limiter.check(key):
        retry = limiter.retry_after(key)
        raise HTTPException(
            status_code=429,
            detail=f"提问过于频繁（{user.tier} 档限 {limiter.max_events} 次/分钟），请 {retry} 秒后再试",
            headers={"Retry-After": str(retry)},
        )

    # 租户：由服务端从签名 JWT + 用户记录解析，客户端不可指定（见 core/tenancy.py）
    tenant = resolve_tenant(user)

    # 额度预冻结：先占用上限，再在流结束时按实际产出结算/退回。
    # 冻结失败（余额不足）直接 402，不进入生成——避免"先干活后收不到钱"。
    #
    # 日消耗硬熔断与冻结**同属一次原子动作**：脚本就算并发开成百上千条 SSE，
    # 也只能在 ceil(cap / 单次冻结) 条之后被挡住。若在冻结之后再数一笔，
    # 攻击者可以靠"同时在途"把检查架空——这也是不单独加一步"预检查"的原因。
    ledger = get_ledger()
    account = account_key(None if user.anonymous else user.id, ip)
    try:
        hold = ledger.reserve(
            account,
            ASK_HOLD_AMOUNT,
            request_id=req.request_id or "",
            units=1,
            daily_cap=daily_cap_for(user.tier),
            # 多租户模式下按机构共用一份日预算；单租户必须按账户，
            # 否则所有用户会共享同一份配额、互相挤占。
            daily_bucket=tenant if multi_tenant_enabled() else "",
        )
    except DailyCapExceeded as exc:
        label = plan_for(user.tier).get("label") or user.tier
        raise HTTPException(
            status_code=429,
            detail=f"今日额度已用尽（{label} 上限 {exc.cap}），将于次日 00:00 重置",
            headers={"Retry-After": str(exc.retry_after_s)},
        )
    if hold is None:
        raise HTTPException(
            status_code=402,
            detail="额度不足，请充值或升级档位后再提问",
        )

    # 档位模型：plan.model 非空时用主通道凭证换档位模型名（服务端注入，客户端不可选）
    tier_model = str(plan_for(user.tier).get("model") or "")
    return StreamingResponse(
        _stream(req, tier_model, request, hold=hold, tenant=tenant),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


async def _stream(
    req: ChatRequest,
    tier_model: str = "",
    request: Request | None = None,
    hold=None,
    tenant: str = DEFAULT_TENANT,
):
    """结算外壳：把生成体包进 try/finally，保证**任何**退出路径都落终态。

    为什么结算必须放在 finally
    --------------------------
    流式生成的退出路径有四条：正常跑完 / 客户端断开 break / 模型异常 /
    生成器被 GC。把结算写在正常路径末尾，其余三条就会漏账——预冻结的额度
    永远退不回来，用户莫名其妙被扣。finally 对这四条一视同仁。

    有效产出取 `len(u_out)` 估算：只有真正吐给客户端的字符才算数，
    中断时止步于中断点，天然符合"按交付量收费"。
    """
    produced = 0
    try:
        async for event in _stream_body(req, tier_model, request, hold=hold, tenant=tenant):
            # 从 delta 事件里累计产出字符数，作为结算依据
            if event.startswith("event: delta"):
                try:
                    payload = json.loads(event.split("data: ", 1)[1].split("\n\n", 1)[0])
                    produced += len(str(payload.get("text") or ""))
                except Exception:
                    pass
            yield event
    finally:
        if hold is not None:
            ledger = get_ledger()
            if produced > 0:
                ledger.settle(
                    hold,
                    effective_tokens=estimate_tokens("x" * produced),
                    request_id=req.request_id or "",
                    reason=f"流结束，产出 {produced} 字符",
                )
            else:
                # 零产出（未进入生成 / 立即异常）→ 全额退回
                ledger.release(hold, reason="无任何产出")


async def _stream_body(
    req: ChatRequest,
    tier_model: str = "",
    request: Request | None = None,
    hold=None,
    tenant: str = DEFAULT_TENANT,
):
    """流式生成。request 用于连接存活检测——客户端关页面/点停止后必须中断生成，
    否则 LLM 会在后台跑完全文，白烧 token 且占住连接（Ghost Generation）。
    """

    async def _aborted() -> bool:
        if request is None:
            return False
        try:
            return await request.is_disconnected()
        except Exception:  # 连接对象已销毁等，一律按未断开处理，不打断正常流程
            return False

    t0 = time.time()
    u_sys = u_ret = u_out = ""
    intent = classify(req.text, has_image=bool(req.image_b64), force=req.force_intent)

    # 多轮上下文：先取历史（此刻当前消息尚未入库），所有意图的 LLM 调用共享
    history = await _history_messages(req.session_id)

    user_msg = PolymorphicMessage(
        session_id=req.session_id,
        role="user",
        type=MessageType.general_text,
        content=req.text or "[图片]",
        intent=intent,
    )
    await repo.append_message(req.session_id, user_msg.model_dump(mode="json"))

    ocr_text = None
    if req.image_b64 and intent == Intent.solve:
        from app.services.vlm.ocr_engine import OCREngine

        ocr = await OCREngine().recognize(req.image_b64)
        ocr_text = ocr.get("problem_text")

    query = _user_query(req, ocr_text, history)
    assistant = PolymorphicMessage(
        session_id=req.session_id,
        role="assistant",
        type=MessageType.general_text,
        content="",
        intent=intent,
    )
    yield _sse("meta", {"message_id": assistant.id, "intent": intent.value})

    # ---------------------------------------------------------- solve ----
    if intent == Intent.solve:
        refs, chunks = await _retrieve_evidence(
            query, course_id=req.course_id, retrieval_mode=req.retrieval_mode,
            time_alpha_override=req.time_alpha_override,
            canonical_bonus_override=req.canonical_bonus_override,
            chapter=req.chapter,
            tenant_id=tenant,
        )
        yield _sse("evidence", {"list": [r.model_dump(mode="json") for r in refs]})

        # Token 预算装箱：按可用窗口贪心选片，并在预算允许时补前后邻近切片，
        # 替代原先"取前 N 条直接拼"——避免长切片顶爆窗口、短句浪费空间。
        chunks = await _pack_for_prompt(chunks, SOLVE_SYSTEM, query, history, tenant_id=tenant)
        context = packer.serialize(chunks)
        u_sys, u_ret = SOLVE_SYSTEM, context
        provider = get_tier_provider(tier_model)
        messages = [
            {"role": "system", "content": SOLVE_SYSTEM},
            *history,
            {"role": "user", "content": f"{query}\n\n【课堂切片上下文】\n{context}"},
        ]
        full = ""
        try:
            async for piece in provider.stream_chat(messages):
                if await _aborted():
                    _logger.info("客户端断开，中断 solve 生成（已产出 %d 字）", len(full))
                    break
                full += piece
                yield _sse("delta", {"text": piece})
        except LLMUnavailable as exc:
            # 不再用演示文案补尾：模型挂了就如实告知。
            # 旧行为会把硬编码的 SOLVE_MARKDOWN 当成答案吐给用户——
            # 用户以为拿到了正解，平台还按正常产出计费，两头都不诚实。
            _logger.error("solve 生成不可用，停止本次回答: %s", exc)
            yield _sse("error", {"message": "模型服务暂时不可用，本次回答未完整生成"})
        except Exception as exc:  # noqa: BLE001 - 流式生成异常类型不可枚举
            _logger.exception("solve 生成异常: %s", exc)
            yield _sse("error", {"message": "生成过程出错，本次回答未完整生成"})
        u_out = full

        pitfalls = extract_from_chunks(chunks)[:3] or SOLVE_PITFALLS
        exam_point = chunks[0].exam_point if chunks else "p-反常积分比较审敛法"
        level = difficulty_svc.score(full + "".join(pitfalls))

        assistant.type = MessageType.solve_card
        assistant.content = full
        assistant.solve_payload = SolvePayload(
            exam_point=exam_point,
            difficulty=level,
            pitfalls=pitfalls,
            steps=SOLVE_STEPS,
            evidence_list=refs,
        )

    # -------------------------------------------------------- socratic ---
    elif intent == Intent.socratic:
        # 检索课堂切片做主题锚定，真实模型可用时引导问题现场生成（不再走死脚本）
        _refs, _chunks = await _retrieve_evidence(
            query, top_k=3, course_id=req.course_id, retrieval_mode=req.retrieval_mode,
            time_alpha_override=req.time_alpha_override,
            canonical_bonus_override=req.canonical_bonus_override, chapter=req.chapter,
            tenant_id=tenant,
        )
        socratic_ctx = "\n---\n".join(
            f"[{_c.board_caption or _c.exam_point}] {_c.text[:200]}" for _c in _chunks[:3]
        )
        payload = await SocraticTutor().start_or_advance(
            req.session_id, req.text if req.text else None,
            topic=query, context=socratic_ctx, history=history,
        )
        yield _sse("delta", {"text": payload.guiding_question})
        assistant.type = MessageType.socratic_card
        assistant.content = payload.guiding_question
        assistant.socratic_payload = payload
        u_out = payload.guiding_question

    # ------------------------------------------------------------ quiz ---
    elif intent == Intent.quiz:
        # 出题锚定当前所学：检索相关切片，把考点/易错点喂给出题器
        _refs, _chunks = await _retrieve_evidence(
            query, top_k=3, course_id=req.course_id, retrieval_mode=req.retrieval_mode,
            time_alpha_override=req.time_alpha_override,
            canonical_bonus_override=req.canonical_bonus_override, chapter=req.chapter,
            tenant_id=tenant,
        )
        _pitfalls = extract_from_chunks(_chunks)
        _exam_point = _chunks[0].exam_point if _chunks else ""
        quiz_ctx = "\n---\n".join(
            f"[{_c.board_caption or _c.exam_point}] {_c.text[:200]}" for _c in _chunks[:3]
        )
        payload: QuizPayload = await QuizGenerator().generate(
            target_pitfall=_pitfalls[0] if _pitfalls else None,
            exam_point=_exam_point,
            context=quiz_ctx,
        )
        yield _sse("delta", {"text": payload.question_text})
        assistant.type = MessageType.quiz_card
        assistant.content = payload.question_text
        assistant.quiz_payload = payload
        u_out = payload.question_text + "".join(payload.options or []) + payload.explanation

    # --------------------------------------------------------- general ---
    elif intent == Intent.general:
        # 普通提问同样自动检索知识库：相关性达标的课堂切片注入上下文并暴露证据链
        refs, chunks = await _retrieve_evidence(
            query, min_score=GENERAL_RELEVANCE_FLOOR,  # top_k 跟随部署档位
            course_id=req.course_id, retrieval_mode=req.retrieval_mode,
            time_alpha_override=req.time_alpha_override,
            canonical_bonus_override=req.canonical_bonus_override,
            chapter=req.chapter,
            tenant_id=tenant,
        )
        # 过滤在 chunk 集合上做；refs 与 chunks 按 audio_snippet_url 中的 chunk_id
        # 一一对应，避免"取前 N 个"导致前端证据与上下文错位
        relevant = [c for c in chunks if _is_relevant(query, c)] or chunks
        if relevant:
            relevant_ids = {c.id for c in relevant}
            relevant_refs = [
                r for r in refs
                if r.audio_snippet_url.rsplit("/", 1)[-1] in relevant_ids
            ] or refs[:1]
            yield _sse("evidence", {"list": [r.model_dump(mode="json") for r in relevant_refs]})

        provider = get_tier_provider(tier_model)
        full = ""
        user_content = req.text or "你好"
        ret_ctx = ""
        if relevant:
            # 同上：general 是长文档问答最容易被顶爆窗口的路径。
            # 该分支没有独立 system prompt（通识问答直出），预算给空串即可。
            relevant = await _pack_for_prompt(relevant, "", req.text or "", history, tenant_id=tenant)
            ret_ctx = packer.serialize(relevant)
            u_ret = ret_ctx
            user_content = (
                f"{user_content}\n\n【知识库命中的课堂资料，请优先依据这些内容回答；"
                f"不相关时再按通识作答】\n{ret_ctx}"
            )
        try:
            async for piece in provider.stream_chat([*history, {"role": "user", "content": user_content}]):
                if await _aborted():
                    _logger.info("客户端断开，中断 general 生成（已产出 %d 字）", len(full))
                    break
                full += piece
                yield _sse("delta", {"text": piece})
        except LLMUnavailable as exc:
            _logger.error("general 生成不可用: %s", exc)
            yield _sse("error", {"message": "模型服务暂时不可用，本次回答未完整生成"})
        except Exception as exc:  # noqa: BLE001 - 同上
            _logger.exception("general 生成异常: %s", exc)
            yield _sse("error", {"message": "生成过程出错，本次回答未完整生成"})
        u_out = full
        assistant.type = MessageType.general_text
        assistant.content = full

    # --------------------------------------------------------- compare ---
    else:  # Intent.compare
        from app.services.llm.dispatch import TrackDispatcher

        dispatcher = TrackDispatcher()
        track_defs = dispatcher.track_names(["gpt", "claude", "qwen"])
        tracks = [
            CompareTrack(model_name=display, content="", status="streaming")
            for _key, display in track_defs
        ]
        assistant.type = MessageType.parallel_compare
        assistant.compare_payload = ComparePayload(tracks=tracks)
        yield _sse("card", assistant.to_client())

        queue: asyncio.Queue = asyncio.Queue()

        async def pump(index: int, key: str, display: str) -> None:
            try:
                async for piece in dispatcher.stream(key, req.text or query, history):
                    if await _aborted():
                        break
                    await queue.put((index, display, piece))
            except Exception:
                pass
            finally:
                await queue.put((index, display, None))

        tasks = [asyncio.create_task(pump(i, k, d)) for i, (k, d) in enumerate(track_defs)]
        finished = 0
        while finished < len(tasks):
            index, display, piece = await queue.get()
            if piece is None:
                finished += 1
                tracks[index].status = "done"
                continue
            if await _aborted():
                _logger.info("客户端断开，中断对比生成（%d/%d 轨完成）", finished, len(tasks))
                break
            tracks[index].content += piece
            yield _sse("track_delta", {"index": index, "model_name": display, "text": piece})
        for t in tasks:
            t.cancel()

        assistant.compare_payload = ComparePayload(tracks=tracks)
        u_out = "".join(tr.content for tr in tracks)

    # ------------------------------------------------------------ usage --
    usage = await _build_usage(req.session_id, u_sys, u_ret, u_out, int((time.time() - t0) * 1000))
    assistant.usage = usage

    await repo.append_message(req.session_id, assistant.to_client())
    yield _sse("usage", {"usage": json.loads(usage.model_dump_json())})
    yield _sse("card", assistant.to_client())
    yield _sse("done", {})


async def _last_quiz_payload(session_id: str) -> QuizPayload | None:
    """取该会话最近一道自测题（含出題时落库的真实答案），作为判分依据。"""
    if not session_id:
        return None
    try:
        messages = await repo.list_messages(session_id)
    except Exception:
        return None
    for m in reversed(messages or []):
        if m.get("role") != "assistant" or m.get("type") != MessageType.quiz_card.value:
            continue
        raw = m.get("quiz_payload")
        if not raw:
            continue
        try:
            return QuizPayload(**raw)
        except Exception:
            continue  # 脏数据/旧结构，继续往前找
    return None


@router.post("/quiz/grade")
async def grade_quiz(
    option_index: int,
    session_id: str = "",
    user: AuthUser = Depends(current_user_optional),
):
    """按本次真实生成的题目判分。

    判分依据取自出题时随消息落库的 QuizPayload（含 answer），与演示内容无关。
    会话归属校验同 /chat/stream；找不到题目或本题无标准答案时 correct=None，
    前端按"需复核"处理，不再给出可能错误的对错结论。
    """
    if session_id and not user.anonymous:
        owner = await repo.get_session_owner(session_id)
        if owner and owner != user.id:
            raise HTTPException(status_code=403, detail="无权作答该会话的题目")

    chosen = chr(ord("A") + option_index) if 0 <= option_index < 26 else "?"
    payload = await _last_quiz_payload(session_id)
    if payload is None:
        return {
            "correct": None,
            "chosen": chosen,
            "correct_index": None,
            "attribution": "未能定位本题的标准答案，请重新出一道自测题。",
            "suggestion": "当前会话没有可判分的自测题，或消息存储暂不可用。",
        }

    correct, msg = grade_objective(payload, chosen)
    if correct is None:
        attribution = msg
        suggestion = "本题需结合解析人工复核。"
    else:
        attribution = payload.explanation or msg
        if not correct:
            attribution = f"{msg}。{attribution}"
        suggestion = (
            f"本题针对易错点「{payload.target_pitfall}」，难度 {payload.difficulty}/5。"
            + ("答对了，可换同型变式再巩固一次。" if correct else "建议回看解析后重做同型变式。")
        )
    return {
        "correct": correct,
        "chosen": chosen,
        "correct_index": correct_option_index(payload),
        "attribution": attribution,
        "suggestion": suggestion,
    }


@router.post("/socratic/reply")
async def socratic_reply(
    session_id: str,
    reply: str,
    user: AuthUser = Depends(current_user_optional),
):
    """学生作答后推进引导：与首轮一样带上最近对话与课堂检索上下文，避免引导脱题。"""
    if not user.anonymous:
        owner = await repo.get_session_owner(session_id)
        if owner and owner != user.id:
            raise HTTPException(status_code=403, detail="无权在该会话中继续")

    history = await _history_messages(session_id)
    topic = ""
    for m in reversed(history):
        if m.get("role") == "user":
            topic = str(m.get("content") or "")
            break
    context = ""
    if topic:
        try:
            _refs, _chunks = await _retrieve_evidence(
                topic, top_k=3, tenant_id=resolve_tenant(user)
            )
            context = "\n---\n".join(
                f"[{_c.board_caption or _c.exam_point}] {_c.text[:200]}" for _c in _chunks[:3]
            )
        except Exception:
            context = ""
    payload = await SocraticTutor().start_or_advance(
        session_id, reply, topic=topic or reply, context=context, history=history,
    )
    return payload
