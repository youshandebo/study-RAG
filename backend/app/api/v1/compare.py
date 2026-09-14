# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""多模型并发派发与聚合接口：单 SSE 连接内并行推送多条模型轨道。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.v1.auth import AuthUser, current_user_optional
from app.core.billing import DailyCapExceeded, account_key, get_ledger
from app.core.membership import daily_cap_for, plan_for
from app.core.security import SlidingWindowLimiter
from app.core.tenancy import multi_tenant_enabled, resolve_tenant
from app.services.llm.dispatch import TrackDispatcher
from app.services.tokens import estimate_tokens


class CompareBody(BaseModel):
    question: str
    model_keys: list[str] | None = None  # gpt | claude | qwen | deepseek
    request_id: str = ""                 # 客户端幂等键（空=不启用幂等）


router = APIRouter()

# 分屏比对是并发放大器：限流比对聊天更严（10 次/分钟）。
# 注意：下面的 check 会按**实际轨道数**放大计数——一次比对并发 N 条模型
# 就是 N 倍成本，若仍只记 1 次，用户可用 10 次额度撬动 10×N 次 LLM 调用。
_COMPARE_UNIT_LIMITER = SlidingWindowLimiter(max_events=10, window_seconds=60)

# 单条轨道的预冻结额度上限（额度单位）
TRACK_HOLD_AMOUNT = 4000


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/compare/stream")
async def compare_stream(
    req: CompareBody,
    request: Request,
    user: AuthUser = Depends(current_user_optional),
) -> StreamingResponse:
    ip = request.client.host if request.client else "unknown"
    dispatcher = TrackDispatcher()
    track_defs = dispatcher.track_names(req.model_keys)
    units = max(1, len(track_defs))

    # 限流按轨道数放大：N 条轨道消耗 N 次配额。逐次 check 而非一次
    # check(N)，这样即使余额只够 3 条也能明确拒绝，而不是先放行再超支。
    key = f"{ip}:{None if user.anonymous else user.id}"
    for _ in range(units):
        if not _COMPARE_UNIT_LIMITER.check(key):
            retry = _COMPARE_UNIT_LIMITER.retry_after(key)
            raise HTTPException(
                status_code=429,
                detail=f"比对请求过于频繁（{units} 条轨道计入 {units} 次配额，限 10 次/分钟）",
                headers={"Retry-After": str(retry)},
            )

    # 预冻结：units 条轨道 = units 倍成本，冻结额度同倍——多倍成本在
    # 冻结阶段就如实反映，不会到结算时才暴露"收不抵支"。
    # 日消耗硬熔断同在这一步生效：N 条轨道会一次性占掉 N×Hold 的日配额，
    # 比对是最高倍的烧钱入口，不能等到结算才记账。
    ledger = get_ledger()
    account = account_key(None if user.anonymous else user.id, ip)
    try:
        hold = ledger.reserve(
            account,
            TRACK_HOLD_AMOUNT,
            request_id=req.request_id or "",
            units=units,
            daily_cap=daily_cap_for(user.tier),
            daily_bucket=resolve_tenant(user) if multi_tenant_enabled() else "",
        )
    except DailyCapExceeded as exc:
        label = plan_for(user.tier).get("label") or user.tier
        raise HTTPException(
            status_code=429,
            detail=f"今日额度已用尽（{label} 上限 {exc.cap}），将于次日 00:00 重置",
            headers={"Retry-After": str(exc.retry_after_s)},
        )
    if hold is None:
        raise HTTPException(status_code=402, detail="额度不足，无法发起多模型比对")

    async def gen():
        queue: asyncio.Queue = asyncio.Queue()
        produced = 0

        async def pump(index: int, key_: str, display: str) -> None:
            nonlocal produced
            try:
                async for piece in dispatcher.stream(key_, req.question):
                    produced += len(piece or "")
                    await queue.put(("track_delta", {"index": index, "model_name": display, "text": piece}))
            except Exception:
                pass
            finally:
                await queue.put(("track_done", {"index": index, "model_name": display}))

        try:
            yield _sse("meta", {"tracks": [d for _, d in track_defs]})
            tasks = [asyncio.create_task(pump(i, k, d)) for i, (k, d) in enumerate(track_defs)]
            done = 0
            while done < len(tasks):
                event, data = await queue.get()
                if event == "track_done":
                    done += 1
                yield _sse(event, data)
            for t in tasks:
                t.cancel()
            yield _sse("done", {})
        finally:
            # 与 chat_stream 同款终态结算：任何退出路径都落到 settled / released
            if produced > 0:
                ledger.settle(
                    hold,
                    effective_tokens=estimate_tokens("x" * produced),
                    request_id=req.request_id or "",
                    reason=f"比对结束，{units} 轨道共产出 {produced} 字符",
                )
            else:
                ledger.release(hold, reason="比对无产出，全额退回")

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
