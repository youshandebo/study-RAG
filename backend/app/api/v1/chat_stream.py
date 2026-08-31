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
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

import app.db.relational as repo
from app.api.v1.auth import AuthUser, current_user_optional
from app.core.membership import plan_for
from app.core.security import SlidingWindowLimiter
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
from app.services.agent.evaluator import Evaluator
from app.services.agent.quiz_generator import QuizGenerator
from app.services.agent.socratic_tutor import SocraticTutor
from app.services.extractor import difficulty as difficulty_svc
from app.services.extractor.pitfall import extract_from_chunks
from app.services.llm.mock_engine import (
    SOLVE_MARKDOWN,
    SOLVE_PITFALLS,
    SOLVE_STEPS,
)
from app.services.llm.provider import get_provider
from app.services.router.intent_classifier import classify
from app.services.rag.retriever import get_retriever
from app.services.tokens import estimate_tokens

router = APIRouter()

# 流式对话限流：按会员档位分配速率（SSE 单次连接计数一次），key=IP+会话
_tier_limiters: dict[str, SlidingWindowLimiter] = {}


def _limiter_for(tier: str) -> SlidingWindowLimiter:
    if tier not in _tier_limiters:
        _tier_limiters[tier] = SlidingWindowLimiter(
            max_events=int(plan_for(tier).get("chat_per_min", 10)), window_seconds=60
        )
    return _tier_limiters[tier]

SOLVE_SYSTEM = (
    "你是一位大学课堂的专属助教。请严格依据提供的课堂切片（老师原话与板书）所体现的解法和口吻来解题，"
    "步骤清晰、使用 LaTeX 公式，并在结尾标注所用课堂方法。"
)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _retrieve_evidence(
    query: str,
    top_k: int = 3,
    min_score: float | None = None,
    course_id: str | None = None,
    retrieval_mode: str = "lecture",
    time_alpha_override: float | None = None,
    canonical_bonus_override: float | None = None,
    chapter: str | None = None,
) -> tuple[list[EvidenceRef], list]:
    retriever = await get_retriever()
    chunks = await retriever.retrieve(
        query, top_k=top_k, min_score=min_score, course_id=course_id or None,
        retrieval_mode=retrieval_mode, with_context_window=False,
        time_alpha_override=time_alpha_override,
        canonical_bonus_override=canonical_bonus_override,
        chapter=chapter or None,
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
    """轻量二次判定：查询词与切片文本存在实词交叠即视为相关。"""
    import app.services.rag.embedder as emb

    qtokens = {t for t in emb._tokenize(query) if len(t) >= 2}
    if not qtokens:
        return False
    hay = set(emb._tokenize(chunk.text)) | set(emb._tokenize(chunk.exam_point or ""))
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


_CONTEXT_LIMIT = 131072  # 演示口径：128K


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
        context_limit=_CONTEXT_LIMIT,
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
    # 档位限速
    limiter = _limiter_for(user.tier)
    if not limiter.check(f"{ip}:{req.session_id}"):
        retry = limiter.retry_after(f"{ip}:{req.session_id}")
        raise HTTPException(
            status_code=429,
            detail=f"提问过于频繁（{user.tier} 档限 {limiter.max_events} 次/分钟），请 {retry} 秒后再试",
            headers={"Retry-After": str(retry)},
        )
    # 档位模型：plan.model 非空时用主通道凭证换档位模型名（服务端注入，客户端不可选）
    tier_model = str(plan_for(user.tier).get("model") or "")
    return StreamingResponse(
        _stream(req, tier_model), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


async def _stream(req: ChatRequest, tier_model: str = ""):
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
        )
        yield _sse("evidence", {"list": [r.model_dump(mode="json") for r in refs]})

        context = "\n---\n".join(f"[{c.start}-{c.end}] {c.text}" for c in chunks)
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
                full += piece
                yield _sse("delta", {"text": piece})
        except Exception:
            fallback = SOLVE_MARKDOWN[len(full) :]
            full += fallback
            yield _sse("delta", {"text": fallback})
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
            query, top_k=3, min_score=GENERAL_RELEVANCE_FLOOR,
            course_id=req.course_id, retrieval_mode=req.retrieval_mode,
            time_alpha_override=req.time_alpha_override,
            canonical_bonus_override=req.canonical_bonus_override,
            chapter=req.chapter,
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
            ret_ctx = "\n---\n".join(
                f"[{c.board_caption or '课堂切片'}·{c.start}-{c.end}] {c.text}" for c in relevant
            )
            u_ret = ret_ctx
            user_content = (
                f"{user_content}\n\n【知识库命中的课堂资料，请优先依据这些内容回答；"
                f"不相关时再按通识作答】\n{ret_ctx}"
            )
        try:
            async for piece in provider.stream_chat([*history, {"role": "user", "content": user_content}]):
                full += piece
                yield _sse("delta", {"text": piece})
        except Exception:
            pass
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


_evaluator = Evaluator()


@router.post("/quiz/grade")
async def grade_quiz(option_index: int, session_id: str = ""):
    result = _evaluator.grade_option(option_index)
    return {
        "correct": result.correct,
        "chosen": result.chosen,
        "attribution": result.attribution,
        "suggestion": result.suggestion,
    }


@router.post("/socratic/reply")
async def socratic_reply(session_id: str, reply: str):
    payload = await SocraticTutor().start_or_advance(session_id, reply)
    return payload
