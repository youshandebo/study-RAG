# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""P2-B 测试：Leitner 调度 / 确定性判分 / 自测闭环 / 错题本隔离 / 租户卡点。

运行：cd backend && python -m pytest tests/test_notebook.py -q

最该被钉住的三条不变量
----------------------
1. **判分不确定 ≠ 判错**：`correct=None` 时盒子不动、掌握度不扣——
   技术性"判不出来"绝不能变成对学生的惩罚。
2. **租户 + 用户双重隔离**：租户 A 的学生拿不到租户 B 的错题；
   租户 A 的教研拿不到租户 B 的卡点分布。
3. **自测失败自动被动归档**：CONVERGING 判错必须留下一条
   `source_type=passive_converge` 的错题，否则学生的盲区永远收不上来。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import app.db.relational as rel
from app.services.agent import socratic_store
from app.services.notebook import grading, leitner
from app.services.notebook import store as nb_store

DAY = 86_400_000


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """独立 SQLite + 干净内存兜底，互不污染。"""
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "notebook.db"))
    monkeypatch.setenv("MULTI_TENANT_MODE", "1")
    saved = (rel._engine, rel._sessionmaker, rel._tables_ready, rel._pg_broken)
    rel._engine, rel._sessionmaker, rel._tables_ready, rel._pg_broken = None, None, False, False
    nb_store.reset_memory()
    socratic_store.reset_memory()
    yield
    try:
        if rel._engine is not None:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(rel._engine.dispose())
            finally:
                loop.close()
    except Exception:
        pass
    rel._engine, rel._sessionmaker, rel._tables_ready, rel._pg_broken = saved
    nb_store.reset_memory()
    socratic_store.reset_memory()


@pytest.fixture
def mock_quiz(monkeypatch):
    """强制出题器走演示兜底（确定性题目/答案），避免测试真的打模型。"""
    async def _none(*_a, **_k):
        return None

    monkeypatch.setattr("app.services.agent.quiz_generator.ask_llm_json", _none)


def _user(tenant: str, role: str = "member", uid: str = "u-1", anonymous: bool = False):
    from app.api.v1.auth import AuthUser

    return AuthUser(uid, "a@b.com", "free", anonymous=anonymous, tenant_id=tenant, role=role)


# ============================================================ Leitner ====
class TestLeitnerScheduler:
    def test_interval_ladder(self):
        assert [leitner.interval_days(b) for b in range(1, 6)] == [1, 3, 7, 14, 30]

    def test_interval_clamps_out_of_range(self):
        assert leitner.interval_days(0) == 1 and leitner.interval_days(99) == 30

    def test_on_correct_promotes_and_schedules(self):
        now = 1_000_000
        res = leitner.on_correct(1, 40, now)
        assert res["leitner_box"] == 2
        assert res["mastery_score"] == 55
        assert res["next_review_at"] == now + 3 * DAY
        assert res["status"] == leitner.STATUS_ACTIVE

    def test_reaching_top_box_marks_mastered(self):
        res = leitner.on_correct(4, 60, 0)
        assert res["leitner_box"] == 5
        assert res["status"] == leitner.STATUS_MASTERED
        assert res["next_review_at"] == 30 * DAY

    def test_on_wrong_resets_to_box1(self):
        res = leitner.on_wrong(4, 80, 0)
        assert res["leitner_box"] == 1
        assert res["mastery_score"] == 55
        assert res["status"] == leitner.STATUS_ACTIVE
        assert res["next_review_at"] == DAY  # 次日重考

    def test_mastery_clamped(self):
        assert leitner.on_correct(1, 95, 0)["mastery_score"] == 100
        assert leitner.on_wrong(1, 10, 0)["mastery_score"] == 0

    def test_initial_mastery_falls_with_failures(self):
        assert leitner.initial_mastery(1) == 40
        assert leitner.initial_mastery(3) == 20
        assert leitner.initial_mastery(99) == 0

    def test_is_due(self):
        assert leitner.is_due(0, 100) is True
        assert leitner.is_due(100, 100) is True
        assert leitner.is_due(101, 100) is False

    def test_new_entry_defaults(self):
        entry = leitner.new_entry(fail_count=2, now_ms=0)
        assert entry["leitner_box"] == 1 and entry["review_count"] == 0
        assert entry["status"] == leitner.STATUS_ACTIVE and entry["next_review_at"] == DAY


# ============================================================= Grading ====
class TestDeterministicGrading:
    def test_normalize_strips_latex_and_width(self):
        assert grading.normalize(" $\\left( x^2 \\right)$ ") == "(x^2)"
        assert grading.normalize("１．５") == "1.5"

    def test_option_letter_equivalence(self):
        opts = ["A. 收敛", "B. 发散", "C. 无法判定"]
        assert grading.grade_answer(reference_answer="A. 收敛", student_answer="A", options=opts)[0] is True
        assert grading.grade_answer(reference_answer="A. 收敛", student_answer="a", options=opts)[0] is True
        assert grading.grade_answer(reference_answer="A. 收敛", student_answer="B", options=opts)[0] is False

    def test_text_equivalence(self):
        assert grading.grade_answer(
            reference_answer="$x^2$", student_answer="x^2")[0] is True

    def test_fraction_equals_decimal(self):
        assert grading.grade_answer(reference_answer="3/2", student_answer="1.5")[0] is True
        assert grading.grade_answer(reference_answer="0.5", student_answer=".5")[0] is True

    def test_wrong_number_is_false(self):
        assert grading.grade_answer(reference_answer="1", student_answer="2")[0] is False

    def test_unknown_when_no_reference(self):
        assert grading.grade_answer(reference_answer="", student_answer="随便")[0] is None

    def test_open_answer_mismatch_is_unknown_not_false(self):
        """无选项的开放题对不上时返回 None（交给结构化判分），绝不能武断判错。"""
        verdict, _ = grading.grade_answer(
            reference_answer="因为对数增长慢于任意正幂", student_answer="不知道")
        assert verdict is None

    def test_empty_answer_is_unknown(self):
        assert grading.grade_answer(reference_answer="x", student_answer="")[0] is None

    @pytest.mark.asyncio
    async def test_structured_grade_parses_json(self, monkeypatch):
        async def _fake(_system, _prompt, **_k):
            return {"correct": True, "reason": "数学等价"}

        monkeypatch.setattr("app.services.llm.json_call.ask_llm_json", _fake)
        verdict, reason = await grading.structured_grade(
            question="q", reference_answer="a", student_answer="b")
        assert verdict is True and "等价" in reason

    @pytest.mark.asyncio
    async def test_structured_failure_is_unknown_not_wrong(self, monkeypatch):
        async def _boom(*_a, **_k):
            raise RuntimeError("LLM down")

        monkeypatch.setattr("app.services.llm.json_call.ask_llm_json", _boom)
        verdict, _ = await grading.structured_grade(question="q", reference_answer="a", student_answer="b")
        assert verdict is None, "判分器故障不得倒向'判错'"

    @pytest.mark.asyncio
    async def test_fallback_skips_llm_when_deterministic(self, monkeypatch):
        """确定性规则一旦给结论，就不该再去打模型。"""
        called = {"n": 0}

        async def _spy(*_a, **_k):
            called["n"] += 1
            return {"correct": False}

        monkeypatch.setattr("app.services.llm.json_call.ask_llm_json", _spy)
        verdict, _ = await grading.grade_with_fallback(
            reference_answer="x^2", student_answer="x^2")
        assert verdict is True and called["n"] == 0


# ======================================================== Store =====
class TestNotebookStore:
    @pytest.mark.asyncio
    async def test_add_and_get(self, db_env):
        item = await nb_store.add_mistake(
            tenant_id="org-a", user_id="u-1", concept_tag="反常积分",
            question_context="题目", reference_answer="A", options=["A", "B"],
            source_type=nb_store.SOURCE_PASSIVE,
        )
        got = await nb_store.get("org-a", item["id"])
        assert got is not None and got["concept_tag"] == "反常积分"
        assert got["leitner_box"] == 1 and got["options"] == ["A", "B"]

    @pytest.mark.asyncio
    async def test_due_only_after_interval(self, db_env):
        item = await nb_store.add_mistake(tenant_id="org-a", user_id="u-1")
        nra = item["next_review_at"]
        assert nra == item["created_at"] + DAY, "新错题应排在次日复习"

        # 以条目自身的调度时间为锚：早一秒不到期，到点即到期
        assert await nb_store.list_due("org-a", "u-1", now_ms=nra - 1) == []
        due = await nb_store.list_due("org-a", "u-1", now_ms=nra)
        assert [d["id"] for d in due] == [item["id"]]

    @pytest.mark.asyncio
    async def test_review_correct_promotes(self, db_env):
        item = await nb_store.add_mistake(tenant_id="org-a", user_id="u-1")
        now = 1_700_000_000_000
        updated = await nb_store.apply_review("org-a", item["id"], correct=True, now_ms=now)

        assert updated["leitner_box"] == 2
        assert updated["mastery_score"] == leitner.initial_mastery(1) + leitner.MASTERY_GAIN
        assert updated["review_count"] == 1 and updated["last_reviewed_at"] == now
        assert updated["next_review_at"] == now + 3 * DAY

    @pytest.mark.asyncio
    async def test_review_wrong_resets_box(self, db_env):
        item = await nb_store.add_mistake(tenant_id="org-a", user_id="u-1")
        # 先升到 Box 3
        await nb_store.apply_review("org-a", item["id"], correct=True)
        await nb_store.apply_review("org-a", item["id"], correct=True)
        assert (await nb_store.get("org-a", item["id"]))["leitner_box"] == 3

        updated = await nb_store.apply_review("org-a", item["id"], correct=False, now_ms=0)
        assert updated["leitner_box"] == 1 and updated["next_review_at"] == DAY

    @pytest.mark.asyncio
    async def test_review_unknown_does_not_change_box(self, db_env):
        """判分不确定：只记复习次数，盒子与掌握度不动。"""
        item = await nb_store.add_mistake(tenant_id="org-a", user_id="u-1")
        before = await nb_store.get("org-a", item["id"])
        updated = await nb_store.apply_review("org-a", item["id"], correct=None)

        assert updated["leitner_box"] == before["leitner_box"]
        assert updated["mastery_score"] == before["mastery_score"]
        assert updated["review_count"] == 1

    @pytest.mark.asyncio
    async def test_cross_tenant_is_invisible(self, db_env):
        item = await nb_store.add_mistake(tenant_id="org-a", user_id="u-1")
        assert await nb_store.get("org-b", item["id"]) is None
        assert await nb_store.list_for_user("org-b", "u-1") == []
        assert await nb_store.apply_review("org-b", item["id"], correct=True) is None

    @pytest.mark.asyncio
    async def test_cross_user_is_invisible(self, db_env):
        await nb_store.add_mistake(tenant_id="org-a", user_id="u-1")
        assert await nb_store.list_for_user("org-a", "u-2") == []

    @pytest.mark.asyncio
    async def test_tenant_struggles_aggregation(self, db_env):
        await nb_store.add_mistake(tenant_id="org-a", user_id="u-1", concept_tag="反常积分",
                                   source_type=nb_store.SOURCE_PASSIVE)
        await nb_store.add_mistake(tenant_id="org-a", user_id="u-2", concept_tag="反常积分",
                                   source_type=nb_store.SOURCE_MANUAL)
        mastered = await nb_store.add_mistake(tenant_id="org-a", user_id="u-2", concept_tag="矩阵",
                                              source_type=nb_store.SOURCE_PASSIVE)
        for _ in range(4):
            await nb_store.apply_review("org-a", mastered["id"], correct=True)
        await nb_store.add_mistake(tenant_id="org-b", user_id="u-9", concept_tag="别的机构知识点")

        rows = await nb_store.tenant_struggles("org-a")
        tags = [r["concept_tag"] for r in rows]
        assert "别的机构知识点" not in tags, "卡点聚合串租户了"

        by_tag = {r["concept_tag"]: r for r in rows}
        assert by_tag["反常积分"]["mistakes"] == 2
        assert by_tag["反常积分"]["passive_converge"] == 1
        assert by_tag["反常积分"]["converge_fail_rate"] == 0.5
        assert by_tag["矩阵"]["mastered"] == 1

    @pytest.mark.asyncio
    async def test_memory_fallback(self, monkeypatch):
        async def _no_db():
            return None

        monkeypatch.setattr(nb_store.repo, "db_session", _no_db)
        nb_store.reset_memory()

        item = await nb_store.add_mistake(tenant_id="org-a", user_id="u-1", concept_tag="X")
        assert (await nb_store.get("org-a", item["id"]))["concept_tag"] == "X"
        updated = await nb_store.apply_review("org-a", item["id"], correct=True)
        assert updated["leitner_box"] == 2


# ==================================================== 收敛自测闭环 ====
class TestConvergingLoop:
    @staticmethod
    async def _to_converging(t, sid, tenant="org-a", uid="u-1"):
        """把会话推到 CONVERGING（并在进入时出题挂起）。"""
        await t.start_or_advance(sid, "教我反常积分", tenant_id=tenant, user_id=uid, topic="反常积分")
        await t.start_or_advance(sid, "我觉得是 x^2", tenant_id=tenant, user_id=uid, topic="反常积分")
        await t.start_or_advance(sid, "对，应该是 x^2", tenant_id=tenant, user_id=uid, topic="反常积分")
        return await t.start_or_advance(sid, "对，就是 x^2", tenant_id=tenant, user_id=uid, topic="反常积分")

    @pytest.mark.asyncio
    async def test_entering_converging_creates_pending_quiz(self, db_env, mock_quiz):
        from app.services.agent.socratic_tutor import SocraticTutor

        payload = await self._to_converging(SocraticTutor(), "s-1")
        assert payload.phase == "converging"
        assert "检验" in payload.guiding_question

        pending = await socratic_store.get_pending_quiz("s-1")
        assert pending is not None and pending.get("question_text")

    @pytest.mark.asyncio
    async def test_wrong_answer_fails_converge_and_archives_mistake(self, db_env, mock_quiz):
        from app.services.agent.socratic_tutor import SocraticTutor

        t = SocraticTutor()
        await self._to_converging(t, "s-1")
        # 演示题正确答案是选项 A，这里故意选 B
        payload = await t.start_or_advance("s-1", "B", tenant_id="org-a", user_id="u-1",
                                           topic="反常积分", exam_point="p-反常积分")

        assert payload.phase == "guiding", "自测失败应回退到引导重新搭脚手架"
        assert payload.converge_failed is True
        assert await socratic_store.get_pending_quiz("s-1") is None, "已作答的题不该继续挂起"

        book = await nb_store.list_for_user("org-a", "u-1")
        assert len(book) == 1, "自测失败必须被动归档一条错题"
        item = book[0]
        assert item["source_type"] == nb_store.SOURCE_PASSIVE
        assert item["concept_tag"] == "p-反常积分"
        assert item["reference_answer"], "错题要带参考答案，复习才有判分依据"

    @pytest.mark.asyncio
    async def test_correct_answer_resolves_and_archives_nothing(self, db_env, mock_quiz):
        from app.services.agent.socratic_tutor import SocraticTutor

        t = SocraticTutor()
        await self._to_converging(t, "s-2")
        payload = await t.start_or_advance("s-2", "A", tenant_id="org-a", user_id="u-1",
                                           topic="反常积分", exam_point="p-反常积分")

        assert payload.phase == "resolved"
        assert await nb_store.list_for_user("org-a", "u-1") == []

    @pytest.mark.asyncio
    async def test_first_turn_does_not_grade(self, db_env, mock_quiz):
        from app.services.agent.socratic_tutor import SocraticTutor

        payload = await SocraticTutor().start_or_advance("s-3", "我不会", tenant_id="org-a", user_id="u-1")
        assert payload.phase == "diagnosing", "首轮是主题/开场白，不该被当作作答"


# ======================================================== API =====
class TestNotebookAPI:
    @pytest.mark.asyncio
    async def test_anonymous_rejected(self, db_env):
        from fastapi import HTTPException

        from app.api.v1.notebook import list_due

        with pytest.raises(HTTPException) as ei:
            await list_due(limit=10, user=_user("org-a", anonymous=True))
        assert ei.value.status_code == 401

    @pytest.mark.asyncio
    async def test_add_and_list(self, db_env):
        from app.api.v1.notebook import NotebookAdd, add_to_notebook, list_notebook

        await add_to_notebook(
            NotebookAdd(question="求极限", answer="1", concept_tag="极限"), _user("org-a"))
        res = await list_notebook(status="", limit=50, user=_user("org-a"))

        assert res["count"] == 1
        assert res["items"][0]["source_type"] == nb_store.SOURCE_MANUAL
        assert res["items"][0]["concept_tag"] == "极限"

    @pytest.mark.asyncio
    async def test_review_promotes_box(self, db_env):
        from app.api.v1.notebook import ReviewBody, review

        item = await nb_store.add_mistake(
            tenant_id="org-a", user_id="u-1", question_context="题",
            reference_answer="A. 收敛", options=["A. 收敛", "B. 发散"])
        res = await review(item["id"], ReviewBody(answer="A"), _user("org-a"))

        assert res["correct"] is True
        assert res["item"]["leitner_box"] == 2

    @pytest.mark.asyncio
    async def test_cross_user_review_is_404(self, db_env):
        from fastapi import HTTPException

        from app.api.v1.notebook import ReviewBody, review

        item = await nb_store.add_mistake(tenant_id="org-a", user_id="u-1")
        with pytest.raises(HTTPException) as ei:
            await review(item["id"], ReviewBody(answer="A"), _user("org-a", uid="u-2"))
        assert ei.value.status_code == 404, "跨用户应 404（不泄露存在性）"

    @pytest.mark.asyncio
    async def test_cross_tenant_review_is_404(self, db_env):
        from fastapi import HTTPException

        from app.api.v1.notebook import ReviewBody, review

        item = await nb_store.add_mistake(tenant_id="org-a", user_id="u-1")
        with pytest.raises(HTTPException) as ei:
            await review(item["id"], ReviewBody(answer="A"), _user("org-b", uid="u-1"))
        assert ei.value.status_code == 404

    @pytest.mark.asyncio
    async def test_generate_variant(self, db_env, mock_quiz):
        from app.api.v1.notebook import generate_variant

        item = await nb_store.add_mistake(
            tenant_id="org-a", user_id="u-1", concept_tag="反常积分", misconception="误判对数项")
        res = await generate_variant(item["id"], _user("org-a"))

        assert res["ok"] is True and res["source_id"] == item["id"]
        assert res["question"]["question_text"]

    @pytest.mark.asyncio
    async def test_empty_question_rejected(self, db_env):
        from fastapi import HTTPException

        from app.api.v1.notebook import NotebookAdd, add_to_notebook

        with pytest.raises(HTTPException) as ei:
            await add_to_notebook(NotebookAdd(question="   "), _user("org-a"))
        assert ei.value.status_code == 400


# ==================================================== 租户卡点 ====
class TestTenantInsights:
    @staticmethod
    async def _seed():
        await nb_store.add_mistake(tenant_id="org-a", user_id="u-1", concept_tag="反常积分",
                                   source_type=nb_store.SOURCE_PASSIVE)
        await nb_store.add_mistake(tenant_id="org-b", user_id="u-9", concept_tag="B 机构知识点",
                                   source_type=nb_store.SOURCE_PASSIVE)

    @pytest.mark.asyncio
    async def test_tenant_admin_sees_only_own_tenant(self, db_env):
        from app.api.v1.feedback import concept_struggles

        await self._seed()
        res = await concept_struggles(limit=50, user=_user("org-a", "tenant_admin"))

        assert res["tenant_id"] == "org-a"
        tags = [r["concept_tag"] for r in res["items"]]
        assert tags == ["反常积分"], "租户教研看到了别家机构的卡点"
        assert res["items"][0]["converge_fail_rate"] == 1.0

    @pytest.mark.asyncio
    async def test_member_forbidden(self, db_env):
        from fastapi import HTTPException

        from app.api.v1.feedback import concept_struggles

        with pytest.raises(HTTPException) as ei:
            await concept_struggles(limit=50, user=_user("org-a", "member"))
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    async def test_anonymous_unauthorized(self, db_env):
        from fastapi import HTTPException

        from app.api.v1.feedback import concept_struggles

        with pytest.raises(HTTPException) as ei:
            await concept_struggles(limit=50, user=_user("org-a", anonymous=True))
        assert ei.value.status_code == 401


# ==================================================== 模型与迁移 ====
class TestSchemaAndMigration:
    def test_row_columns_and_indexes(self):
        from app.db.pg_models import MistakeNotebookRow

        cols = set(MistakeNotebookRow.__table__.c.keys())
        assert {
            "id", "tenant_id", "user_id", "concept_tag", "source_type", "session_id",
            "question_context", "reference_answer", "misconception",
            "leitner_box", "mastery_score", "review_count", "next_review_at", "status",
        } <= cols

        idx_names = {i.name for i in MistakeNotebookRow.__table__.indexes}
        assert {
            "ix_mistake_tenant_user_status", "ix_mistake_next_review", "ix_mistake_tenant_concept",
        } <= idx_names

    def test_pending_quiz_column_on_session(self):
        from app.db.pg_models import SocraticSessionRow

        assert "pending_quiz_json" in set(SocraticSessionRow.__table__.c.keys())

    def test_migration_revision_chain(self):
        import importlib.util

        path = Path(__file__).resolve().parent.parent / "migrations" / "versions" / "0005_mistake_notebook.py"
        spec = importlib.util.spec_from_file_location("mig_0005", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.revision == "0005" and mod.down_revision == "0004"


# ==================================================== P2-C 前端契约 ====
class TestP2CContract:
    def test_me_payload_exposes_role(self, monkeypatch):
        """前端据此显隐"机构卡点"页签；服务端另有强制校验，role 只用于显隐。"""
        monkeypatch.setenv("MULTI_TENANT_MODE", "1")
        from app.api.v1.auth import AuthUser

        pub = AuthUser("u-1", "a@b.com", "free", tenant_id="org-a", role="tenant_admin").to_public()
        assert pub["role"] == "tenant_admin"
        assert pub["tenant_id"] == "org-a", "多租户模式下 /auth/me 应回传所属机构"

    @pytest.mark.asyncio
    async def test_converging_payload_carries_structured_selftest(self, db_env, mock_quiz):
        """收敛阶段必须结构化下发自测题（前端据此渲染可点选项）。"""
        from app.services.agent.socratic_tutor import SocraticTutor

        t = SocraticTutor()
        payload = None
        for text in ["教我反常积分", "我觉得是 x^2", "对，应该是 x^2", "对，就是 x^2"]:
            payload = await t.start_or_advance("s-c", text, tenant_id="org-a", user_id="u-1", topic="反常积分")

        assert payload is not None and payload.phase == "converging"
        assert payload.selftest is not None, "收敛阶段应下发结构化自测题"
        assert payload.selftest.question_text
        assert payload.selftest.options, "自测题必须带选项，前端才能渲染成点选"

    @pytest.mark.asyncio
    async def test_non_converging_payload_has_no_selftest(self, db_env, mock_quiz):
        from app.services.agent.socratic_tutor import SocraticTutor

        payload = await SocraticTutor().start_or_advance("s-d", "教我", tenant_id="org-a", user_id="u-1")
        assert payload.selftest is None

    @pytest.mark.asyncio
    async def test_struggles_csv_export_scoped_to_tenant(self, db_env):
        from fastapi.responses import PlainTextResponse

        from app.api.v1.feedback import concept_struggles

        await nb_store.add_mistake(tenant_id="org-a", user_id="u-1", concept_tag="反常积分",
                                   source_type=nb_store.SOURCE_PASSIVE)
        await nb_store.add_mistake(tenant_id="org-b", user_id="u-9", concept_tag="B机构知识点")

        resp = await concept_struggles(limit=50, format="csv", user=_user("org-a", "tenant_admin"))

        assert isinstance(resp, PlainTextResponse)
        body = resp.body.decode("utf-8")
        assert body.startswith("﻿"), "缺 UTF-8 BOM，Excel 打开中文乱码"
        assert "concept_tag" in body.splitlines()[0]
        assert "反常积分" in body
        assert "B机构知识点" not in body, "CSV 里出现了其他租户的卡点"
