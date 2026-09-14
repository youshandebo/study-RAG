# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""错题本与知识补救 API（P2-B）：到期复习 / 复习结算 / 变式衍生 / 手动收录。

隔离口径（重点）
----------------
错题是**比知识库更私密**的个人学习数据，所以这里是**双重隔离**：
- `tenant_id`：由 `resolve_tenant(user)` 从服务端解析（JWT + 用户记录）
- `user_id`：取当前登录用户 id

**接口不接受任何 `tenant_id` / `user_id` 参数**——那是可以伪造的越权口子。
跨租户/跨用户访问统一返回 404（而不是 403）：403 会泄露"这条记录存在"，
404 连存在性都不透露。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.v1.auth import AuthUser, current_user_optional
from app.core.tenancy import resolve_tenant
from app.services.agent.quiz_generator import QuizGenerator
from app.services.notebook import store
from app.services.notebook.grading import grade_with_fallback

router = APIRouter()


class ReviewBody(BaseModel):
    answer: str = ""


class NotebookAdd(BaseModel):
    question: str = ""
    answer: str = ""
    options: list[str] | None = None
    concept_tag: str = ""
    course_id: str = ""
    misconception: str = ""
    session_id: str = ""


def _identity(user: AuthUser) -> tuple[str, str]:
    """解析 (tenant, user_id)。匿名用户没有个人错题本——拒绝而不是共享一份。"""
    if user.anonymous or not user.id:
        raise HTTPException(status_code=401, detail="请先登录后使用错题本")
    return resolve_tenant(user), str(user.id)


@router.get("/notebook/due")
async def list_due(limit: int = 50, user: AuthUser = Depends(current_user_optional)):
    """按当前租户 + 用户拉取**已到期**待复习的错题（含逾期）。"""
    tenant, uid = _identity(user)
    limit = max(1, min(200, limit))
    items = await store.list_due(tenant, uid, limit=limit)
    return {"tenant_id": tenant, "count": len(items), "items": items}


@router.get("/notebook")
async def list_notebook(
    status: str = "", limit: int = 200, user: AuthUser = Depends(current_user_optional),
):
    """列出本人错题本（可按 active / mastered / archived 过滤）。"""
    tenant, uid = _identity(user)
    limit = max(1, min(500, limit))
    items = await store.list_for_user(tenant, uid, status=status, limit=limit)
    return {"tenant_id": tenant, "count": len(items), "items": items}


@router.post("/notebook")
async def add_to_notebook(body: NotebookAdd, user: AuthUser = Depends(current_user_optional)):
    """主动收录（`source_type=active_manual`）：学生在问答中点"加入错题本"。"""
    tenant, uid = _identity(user)
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="题目内容不能为空")
    item = await store.add_mistake(
        tenant_id=tenant, user_id=uid, course_id=body.course_id,
        concept_tag=body.concept_tag or "未标注",
        source_type=store.SOURCE_MANUAL,
        session_id=body.session_id, trace_id=body.session_id,
        question_context=body.question, reference_answer=body.answer,
        options=body.options, misconception=body.misconception,
    )
    return {"ok": True, "item": item}


@router.post("/notebook/{mistake_id}/review")
async def review(mistake_id: str, body: ReviewBody, user: AuthUser = Depends(current_user_optional)):
    """提交一次复习作答：服务端判分 + Leitner 升降盒。

    判分与调度**都在服务端**完成——客户端只说"我答了什么"，
    说了不算"我对不对"，否则改个请求体就能把盒子刷到 5。
    """
    tenant, uid = _identity(user)
    item = await store.get(tenant, mistake_id)
    if item is None or item.get("user_id") != uid:
        # 不存在 / 不属于本用户，一律 404（不泄露存在性）
        raise HTTPException(status_code=404, detail="错题不存在")

    correct, reason = await grade_with_fallback(
        reference_answer=item.get("reference_answer"),
        student_answer=body.answer,
        options=item.get("options"),
        question=item.get("question_context") or "",
    )
    updated = await store.apply_review(tenant, mistake_id, correct=correct)
    return {"correct": correct, "reason": reason, "item": updated}


@router.post("/notebook/{mistake_id}/generate-variant")
async def generate_variant(mistake_id: str, user: AuthUser = Depends(current_user_optional)):
    """按原题考点与误区出一道具**同质同构**的变式题，避免学生死记原题答案。

    只生成、不落库：变式题是否重新入错题本，交由学生/后续流程决定。
    """
    tenant, uid = _identity(user)
    item = await store.get(tenant, mistake_id)
    if item is None or item.get("user_id") != uid:
        raise HTTPException(status_code=404, detail="错题不存在")

    payload = await QuizGenerator().generate_variant(
        exam_point=item.get("concept_tag") or "综合考查",
        difficulty=None,
        avoid_pitfall=item.get("misconception") or None,
    )
    return {"ok": True, "source_id": mistake_id, "question": payload.model_dump()}
