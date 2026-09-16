# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""exam.py 判分链路的三级回落测试。

钉住的是**链路**而非 Rubric 模块本身（后者见 test_rubric.py）：
确定性判分能判 → 不调 LLM；判不出 → 走 Rubric；Rubric 降级 → 退回 diff_process；
任何一级都不得因批改器故障把学生判成"错"。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.api.v1.exam as exam
from app.services.agent import rubric
from app.services.agent.process_diff import ProcessDiffResult
from app.services.rag.chunker import Chunk


def _chunk(text: str = "标准解法：x=2") -> Chunk:
    return Chunk(id="c1", audio_id="a", start="0", end="1", text=text)


class _Q:
    def __init__(self, answer="解：x=2", question="求 x"):
        self.index = 1
        self.question_text = question
        self.student_answer = answer
        self.exam_point = "极限"
        self.canonical_chunk_ids = ["c1"]

    def to_dict(self):
        return {
            "index": self.index,
            "question_text": self.question_text,
            "student_answer": self.student_answer,
            "exam_point": self.exam_point,
            "canonical_chunk_ids": self.canonical_chunk_ids,
        }


@pytest.fixture
def no_net(monkeypatch):
    """切断一切外部依赖：考点匹配、难度、定版检索、出题、落库。"""
    calls = {"rubric": 0, "diff": 0}

    async def _fake_match(*a, **k):
        class P:
            name = "极限"
        return P()

    async def _fake_retrieve(*a, **k):
        return [_chunk()]

    async def _fake_add_asset(*a, **k):
        return {"id": "asset1"}

    async def _fake_owner(sid):
        return None      # 无主会话 → 不触发越权拦截

    async def _fake_rubric(*a, **k):
        calls["rubric"] += 1
        return rubric.RubricResult(degraded=True, degraded_reason="stub")

    async def _fake_diff(*a, **k):
        calls["diff"] += 1
        return ProcessDiffResult(
            first_deviation_step=2, deviation_desc="整体判分：第2步偏离",
            is_final_answer_correct=False,
        )

    class _Gen:
        async def generate_variant(self, **k):
            class V:
                def model_dump(self):
                    return {"stem": "变式题", "options": [], "answer": "A"}
            return V()

    monkeypatch.setattr(exam, "match_from_text", _fake_match)
    monkeypatch.setattr(exam, "score", lambda t: 3)
    monkeypatch.setattr(exam, "OCREngine", lambda: None)
    monkeypatch.setattr(exam.repo, "add_asset", _fake_add_asset)
    monkeypatch.setattr(exam.repo, "get_session_owner", _fake_owner)
    monkeypatch.setattr(exam, "rubric_grade", _fake_rubric)
    monkeypatch.setattr(exam, "diff_process", _fake_diff)
    monkeypatch.setattr(exam, "QuizGenerator", _Gen)
    monkeypatch.setattr(exam, "resolve_tenant", lambda u: "public")
    return calls


def _user(anonymous=True):
    class U:
        id = "u1"
    u = U()
    u.anonymous = anonymous
    return u


class TestTieredGrading:
    @pytest.mark.asyncio
    async def test_deterministic_grade_skips_llm(self, no_net, monkeypatch):
        """结论词能确定性判定 → 完全不打 LLM（省费用、零延迟、结果可复现）。"""
        monkeypatch.setattr(
            exam, "grade_objective_by_canonical", lambda q, c: (True, "结论「收敛」一致")
        )
        out = await exam.grade_exam(
            user=_user(), session_id="s1", file=None, text_content="1. 判断敛散性\n解：收敛"
        )
        assert no_net["rubric"] == 0 and no_net["diff"] == 0
        assert out["report"][0]["correct"] is True
        assert out["report"][0]["rubric"] is None

    @pytest.mark.asyncio
    async def test_falls_back_to_diff_when_rubric_degraded(self, no_net, monkeypatch):
        monkeypatch.setattr(exam, "grade_objective_by_canonical", lambda q, c: (None, ""))
        out = await exam.grade_exam(
            user=_user(), session_id="s1", file=None, text_content="1. 求极限\n解：x=2"
        )
        assert no_net["rubric"] == 1 and no_net["diff"] == 1
        item = out["report"][0]
        assert item["rubric"] is None
        assert item["process_diff"] is not None
        assert item["correct"] is False        # 来自 diff_process 的整体判定

    @pytest.mark.asyncio
    async def test_rubric_used_when_available(self, no_net, monkeypatch):
        monkeypatch.setattr(exam, "grade_objective_by_canonical", lambda q, c: (None, ""))

        async def _ok(*a, **k):
            return rubric.parse_rubric({
                "full_score": 10, "final_answer_correct": True,
                "steps": [
                    {"no": 1, "name": "设元", "points": 5, "hit": "hit"},
                    {"no": 2, "name": "求解", "points": 5, "hit": "partial",
                     "error_type": "computation"},
                ],
                "summary": "方法正确",
            })

        monkeypatch.setattr(exam, "rubric_grade", _ok)
        out = await exam.grade_exam(
            user=_user(), session_id="s1", file=None, text_content="1. 求极限\n解：x=2"
        )
        assert no_net["diff"] == 0, "Rubric 成功时不该再打整体判分"
        item = out["report"][0]
        assert item["rubric"]["score"] == pytest.approx(7.5)
        assert item["rubric"]["error_type"] == "computation"
        assert "第 2 步" in item["attribution"]
        assert item["socratic_followup"]

    @pytest.mark.asyncio
    async def test_unanswered_is_not_graded(self, no_net, monkeypatch):
        """未作答 = 没有可判的内容，走 neither 判分器，也不算 needs_review。"""
        monkeypatch.setattr(exam, "grade_objective_by_canonical", lambda q, c: (None, ""))
        out = await exam.grade_exam(
            user=_user(), session_id="s1", file=None, text_content="1. 求极限"
        )
        item = out["report"][0]
        assert item["attribution"] == "卷面未作答"
        assert no_net["rubric"] == 0 and no_net["diff"] == 0
        assert out["summary"]["needs_review"] == 0

    # -------------------------------------------------- 统计口径 ----
    @pytest.mark.asyncio
    async def test_score_rate_excludes_ungraded_questions(self, no_net, monkeypatch):
        """未走通 Rubric 的题按 0 分计入，会把批改器故障算成学生失分——必须排除。"""
        monkeypatch.setattr(exam, "grade_objective_by_canonical", lambda q, c: (None, ""))
        out = await exam.grade_exam(
            user=_user(), session_id="s1", file=None, text_content="1. 求极限\n解：x=2"
        )
        s = out["summary"]
        assert s["graded_by_rubric"] == 0
        assert s["score_possible"] == 0
        assert s["score_rate"] is None, "分母为 0 时应为 None，不得除零或造假 0 分"

    @pytest.mark.asyncio
    async def test_error_distribution_aggregated(self, no_net, monkeypatch):
        monkeypatch.setattr(exam, "grade_objective_by_canonical", lambda q, c: (None, ""))

        async def _ok(*a, **k):
            return rubric.parse_rubric({
                "full_score": 10,
                "steps": [
                    {"no": 1, "name": "a", "points": 5, "hit": "miss", "error_type": "computation"},
                    {"no": 2, "name": "b", "points": 5, "hit": "miss", "error_type": "computation"},
                ],
            })

        monkeypatch.setattr(exam, "rubric_grade", _ok)
        out = await exam.grade_exam(
            user=_user(), session_id="s1", file=None, text_content="1. 求极限\n解：x=2"
        )
        assert out["summary"]["error_distribution"]["computation"] == 1
        assert out["summary"]["score_rate"] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_pitfalls_prefer_structured_error_label(self, no_net, monkeypatch):
        """错因回填优先用可聚合的枚举标签，而不是自由文本评语。"""
        monkeypatch.setattr(exam, "grade_objective_by_canonical", lambda q, c: (None, ""))

        async def _ok(*a, **k):
            return rubric.parse_rubric({
                "full_score": 10, "final_answer_correct": False,
                "steps": [{"no": 1, "name": "a", "points": 10, "hit": "miss",
                           "error_type": "method"}],
            })

        monkeypatch.setattr(exam, "rubric_grade", _ok)
        captured = {}

        async def _capture(sid, meta):
            captured.update(meta)
            return {"id": "a1"}

        monkeypatch.setattr(exam.repo, "add_asset", _capture)
        await exam.grade_exam(
            user=_user(), session_id="s1", file=None, text_content="1. 求极限\n解：x=2"
        )
        assert captured["pitfalls"] == ["方法选择不当"]

    # -------------------------------------------------- 输入校验 ----
    @pytest.mark.asyncio
    async def test_empty_paper_rejected(self, no_net):
        with pytest.raises(HTTPException) as ei:
            await exam.grade_exam(user=_user(), session_id="s1", file=None, text_content="   ")
        assert ei.value.status_code == 400

    @pytest.mark.asyncio
    async def test_cross_user_submission_rejected(self, no_net, monkeypatch):
        async def _other_owner(sid):
            return "someone-else"

        monkeypatch.setattr(exam.repo, "get_session_owner", _other_owner)
        with pytest.raises(HTTPException) as ei:
            await exam.grade_exam(user=_user(False), session_id="s1", file=None, text_content="1. 求极限")
        assert ei.value.status_code == 403
