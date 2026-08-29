# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""核心逻辑单元测试：检索打分 / 试卷拆题 / 客观判分 / 限流器 / JWT。

运行：cd backend && python -m pytest tests/ -q
（不依赖任何外部服务；LLM 相关路径全部走演示兜底）
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


# ---------------------------------------------------------------- 拆题 ----
class TestPaperSplitter:
    def test_split_two_questions_with_answers(self):
        from app.services.extractor.paper_splitter import split_paper

        paper = (
            "1.\n判断 $\int_1^{\\infty} x^{-2} dx$ 的敛散性。\n解：收敛，因为 p=2>1。\n"
            "2.\n质能方程是什么？\n答：E=mc²。\n"
        )
        qs = split_paper(paper)
        assert len(qs) == 2
        assert qs[0].index == 1 and "p=2>1" in (qs[0].student_answer or "")
        assert qs[1].student_answer == "E=mc²。"

    def test_no_numbering_falls_back_to_whole(self):
        from app.services.extractor.paper_splitter import split_paper

        qs = split_paper("一段没有题号的材料，讨论敛散性判别。")
        assert len(qs) == 1 and qs[0].index == 1

    def test_empty(self):
        from app.services.extractor.paper_splitter import split_paper

        assert split_paper("") == []


# ---------------------------------------------------------------- 判分 ----
class TestObjectiveGrading:
    def _payload(self, answer="A", options=None):
        from app.models.domain import QuizPayload

        return QuizPayload(
            question_text="q", options=options or ["对", "错1", "错2"],
            target_pitfall="t", explanation="e", answer=answer,
        )

    def test_letter_match(self):
        from app.services.agent.quiz_generator import grade_objective

        correct, _ = grade_objective(self._payload(), "A")
        assert correct is True

    def test_letter_mismatch(self):
        from app.services.agent.quiz_generator import grade_objective

        correct, msg = grade_objective(self._payload(), "B")
        assert correct is False and "正确答案" in msg

    def test_text_option_equivalence(self):
        from app.services.agent.quiz_generator import grade_objective

        correct, _ = grade_objective(self._payload(answer="对"), "对")
        assert correct is True

    def test_no_answer_needs_review(self):
        from app.services.agent.quiz_generator import grade_objective

        correct, _ = grade_objective(self._payload(answer=None), "A")
        assert correct is None

    def test_unanswered(self):
        from app.services.agent.quiz_generator import grade_objective

        correct, _ = grade_objective(self._payload(), "")
        assert correct is None


# ---------------------------------------------------------------- 限流 ----
class TestSlidingWindow:
    def test_window_semantics(self):
        from app.core.security import SlidingWindowLimiter

        lim = SlidingWindowLimiter(max_events=3, window_seconds=60)
        assert all(lim.check("k") for _ in range(3))
        assert lim.check("k") is False
        assert lim.retry_after("k") >= 1

    def test_keys_isolated(self):
        from app.core.security import SlidingWindowLimiter

        lim = SlidingWindowLimiter(max_events=1, window_seconds=60)
        assert lim.check("a") and lim.check("b")


# ---------------------------------------------------------------- JWT -----
class TestJwt:
    def test_roundtrip_and_tamper(self):
        from app.core.security import jwt_sign, jwt_verify

        token = jwt_sign({"role": "admin"}, 60)
        assert jwt_verify(token) is not None
        assert jwt_verify(token[:-2] + "xx") is None

    def test_expiry(self):
        import time as _t

        from app.core.security import jwt_sign, jwt_verify

        token = jwt_sign({"role": "admin"}, -1)  # 已过期
        _t.sleep(0.01)
        assert jwt_verify(token) is None


# ---------------------------------------------------------------- SSRF ----
class TestSsrfGuard:
    def test_blocks_private_and_metadata(self):
        from app.core.security import assert_safe_url

        for bad in (
            "http://127.0.0.1/v1", "http://10.0.0.5", "http://192.168.1.1/v1",
            "http://172.16.0.9", "http://169.254.169.254/latest", "file:///etc/passwd",
        ):
            with pytest.raises(ValueError):
                assert_safe_url(bad)

    def test_allows_official(self):
        from app.core.security import assert_safe_url

        assert_safe_url("https://api.openai.com/v1")


# ------------------------------------------------------------ 检索打分 ----
@pytest.mark.asyncio
class TestRetrievalScoring:
    @pytest.fixture()
    async def retriever(self):
        from app.services.rag.retriever import get_retriever

        return await get_retriever()

    async def test_course_scope_hard_isolation(self, retriever):
        hits = await retriever.retrieve_scored(
            "反常积分敛散性", course_id="physics-none", retrieval_mode="review"
        )
        assert hits == []

    async def test_canonical_immune_to_alpha(self, retriever):
        q = "p-积分判别法口诀"
        base = await retriever.retrieve_scored(q, retrieval_mode="lecture")
        zero = await retriever.retrieve_scored(q, retrieval_mode="lecture", time_alpha_override=0.0)
        base_map = {c.id: (s, c) for s, c in base}
        zero_map = {c.id: s for s, c in zero}
        canonical_seen = False
        for cid, (s_base, chunk) in base_map.items():
            if chunk.is_canonical:
                canonical_seen = True
                # 定版切片对 α 完全免疫
                assert base_map[cid][0] == zero_map[cid]
        assert canonical_seen  # 测试数据里确实有定版，断言才有效

    async def test_superseded_filtered(self, retriever):
        from app.services.rag.chunker import Chunk

        old = Chunk(
            id="ut-old", audio_id="ut", start="01:00", end="02:00", text="旧解法内容 ut。",
            course_id="ut-course", exam_point="ut 考点", is_canonical=True, method_version=1,
        )
        await retriever.register_chunks([old])
        await retriever.set_canonical("ut-old", False)
        new = Chunk(
            id="ut-new", audio_id="ut", start="03:00", end="04:00", text="ut 新解法内容。",
            course_id="ut-course", exam_point="ut 考点", is_canonical=True,
            method_version=2, supersedes="ut-old",
        )
        await retriever.register_chunks([new])
        hits = await retriever.retrieve_scored("ut 考点", course_id="ut-course", retrieval_mode="review")
        ids = [c.id for _, c in hits]
        assert "ut-old" not in ids and "ut-new" in ids
