# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""Rubric 分步采分测试：钉住"分数算术不交给模型"这条核心不变量。

分两部分：
- `TestParseRubric` 打桩在**解析层**，不需要 LLM，覆盖全部异常分支；
- `TestRubricGrade` 打桩 `ask_llm_json`，覆盖调用层的降级路径。
"""
from __future__ import annotations

import pytest

from app.services.agent import rubric


def _step(no=1, name="步骤", points=2, hit="hit", error_type=None, follow=False):
    return {
        "no": no, "name": name, "points": points, "hit": hit,
        "evidence": "原文", "error_type": error_type, "follow_through": follow,
    }


class TestParseRubric:
    def test_points_are_normalized_to_full_score(self):
        """模型拆出的分值之和常常不等于满分——必须归一化，否则会给出 11/10 分。"""
        data = {"full_score": 10, "steps": [
            _step(1, "设元", 4), _step(2, "列式", 4), _step(3, "求解", 4),  # 合计 12
        ]}
        r = rubric.parse_rubric(data)
        assert r.full_score == 10
        assert sum(s.points for s in r.steps) == pytest.approx(10.0)
        assert r.score == pytest.approx(10.0)      # 全 hit → 满分，且不超满分

    def test_under_weighted_steps_also_normalize(self):
        data = {"full_score": 10, "steps": [_step(1, "a", 1), _step(2, "b", 1)]}  # 合计 2
        r = rubric.parse_rubric(data)
        assert sum(s.points for s in r.steps) == pytest.approx(10.0)
        assert r.score == pytest.approx(10.0)

    def test_partial_awards_half(self):
        data = {"full_score": 10, "steps": [
            _step(1, "a", 5, hit="hit"), _step(2, "b", 5, hit="partial"),
        ]}
        r = rubric.parse_rubric(data)
        assert r.score == pytest.approx(7.5)   # 5 + 5*0.5

    def test_miss_awards_zero(self):
        data = {"full_score": 10, "steps": [
            _step(1, "a", 5, hit="hit"), _step(2, "b", 5, hit="miss"),
        ]}
        assert rubric.parse_rubric(data).score == pytest.approx(5.0)

    def test_score_never_exceeds_full_score(self):
        """浮点累加 + 全 hit，四舍五入后不得出现 10.01 这类越界。"""
        data = {"full_score": 10, "steps": [_step(i, f"s{i}", 3, hit="hit") for i in range(1, 8)]}
        r = rubric.parse_rubric(data)
        assert r.score <= r.full_score

    # -------------------------------------------------- 核心不变量 ----
    def test_score_is_computed_not_trusted_from_model(self):
        """模型说"最终答案对"，但所有采分点都 miss → 分数必须是 0。

        这条是整个模块的立身之本：分数由 Python 按采分点算出，
        模型的 `final_answer_correct` 只是**附加信息**，不参与算分。
        否则"答案对但过程全错"会被判满分，分步采分就失去了意义。
        """
        data = {"full_score": 10, "final_answer_correct": True, "steps": [
            _step(1, "a", 5, hit="miss"), _step(2, "b", 5, hit="miss"),
        ]}
        r = rubric.parse_rubric(data)
        assert r.score == 0.0
        assert r.final_answer_correct is True   # 结论照实保留，只是不算分

    def test_final_answer_flag_independent_of_score(self):
        data = {"full_score": 10, "final_answer_correct": False, "steps": [
            _step(1, "a", 5, hit="hit"), _step(2, "b", 5, hit="hit"),
        ]}
        r = rubric.parse_rubric(data)
        assert r.score == pytest.approx(10.0)
        assert r.final_answer_correct is False  # 过程全对但结论错，如实反映

    # -------------------------------------------------- 异常输入 ----
    def test_invalid_hit_falls_back_to_partial_with_warning(self):
        """非法 hit 取中间档：判 miss 会冤枉学生，判 hit 会掩盖批改器异常。"""
        data = {"full_score": 10, "steps": [_step(1, "a", 10, hit="maybe")]}
        r = rubric.parse_rubric(data)
        assert r.steps[0].hit == rubric.PARTIAL
        assert r.score == pytest.approx(5.0)
        assert any("hit 值非法" in w for w in r.warnings)

    def test_invalid_error_type_cleared_with_warning(self):
        """自由文本归因无法聚合统计——运营端要看"计算错误占比"这种分布。"""
        data = {"full_score": 10, "steps": [_step(1, "a", 10, hit="miss", error_type="手滑了")]}
        r = rubric.parse_rubric(data)
        assert r.steps[0].error_type is None
        assert any("error_type 非法" in w for w in r.warnings)

    def test_error_type_cleared_when_hit(self):
        """命中就不该有错因——以命中为准，否则报告里会出现"给分了又说错"。"""
        data = {"full_score": 10, "steps": [_step(1, "a", 10, hit="hit", error_type="computation")]}
        assert rubric.parse_rubric(data).steps[0].error_type is None

    def test_invalid_points_default_to_one_not_zero(self):
        """给 0 会让该采分点归一化后彻底消失，等于模型少拆一点就少算一分。"""
        data = {"full_score": 10, "steps": [
            {"no": 1, "name": "a", "points": "abc", "hit": "hit"},
            {"no": 2, "name": "b", "points": -3, "hit": "hit"},
        ]}
        r = rubric.parse_rubric(data)
        assert all(s.points > 0 for s in r.steps)
        assert r.score == pytest.approx(10.0)

    def test_out_of_range_full_score_is_ignored(self):
        data = {"full_score": 9999, "steps": [_step(1, "a", 10)]}
        r = rubric.parse_rubric(data)
        assert r.full_score == rubric.DEFAULT_FULL_SCORE
        assert any("越界" in w for w in r.warnings)

    def test_caller_full_score_wins(self):
        """满分以卷面/配置为准，模型给的只在合理时兜底。"""
        data = {"full_score": 8, "steps": [_step(1, "a", 4), _step(2, "b", 4)]}
        assert rubric.parse_rubric(data, full_score=20).full_score == 20

    def test_missing_steps_are_degraded_not_zero(self):
        """采分点缺失是"批改器没干成活"，不是"学生得 0 分"——二者必须区分。"""
        r = rubric.parse_rubric({"full_score": 10, "steps": []})
        assert r.degraded is True
        assert r.score == 0.0
        assert "采分点" in r.degraded_reason

    def test_steps_capped_to_prevent_cost_runaway(self):
        data = {"full_score": 10, "steps": [_step(i, f"s{i}", 1) for i in range(1, 30)]}
        assert len(rubric.parse_rubric(data).steps) <= 12

    # -------------------------------------------------- 归因 ----
    def test_first_missed_step_drives_error_type(self):
        data = {"full_score": 10, "steps": [
            _step(1, "设元", 3, hit="hit"),
            _step(2, "列式", 3, hit="miss", error_type="concept"),
            _step(3, "求解", 4, hit="miss", error_type="computation"),
        ]}
        r = rubric.parse_rubric(data)
        assert r.first_missed == 2
        assert r.error_type == "concept"   # 取首个，不是最后一个

    def test_attribution_points_at_specific_step(self):
        data = {"full_score": 10, "steps": [
            _step(1, "设元", 5, hit="hit"),
            _step(2, "换元限", 5, hit="miss", error_type="computation"),
        ]}
        text = rubric.rubric_attribution(rubric.parse_rubric(data))
        assert "第 2 步" in text and "换元限" in text and "计算失误" in text

    def test_follow_through_not_blamed_as_root_cause(self):
        """连锁给分的步不该被当成错因——真正的根因在前一步，
        把它当错因会让学生改错地方、越改越乱。"""
        data = {"full_score": 10, "steps": [
            _step(1, "求导", 4, hit="miss", error_type="computation"),
            _step(2, "代入", 3, hit="partial", follow=True),   # 被第 1 步连带
            _step(3, "化简", 3, hit="hit"),
        ]}
        r = rubric.parse_rubric(data)
        assert r.first_missed == 1          # 首个丢分位置
        assert r.root_cause_step == 1       # 根因仍是它
        assert r.affected_steps == [2]      # 第 2 步被连带
        assert r.error_type == "computation"

    def test_attribution_surfaces_chained_loss(self):
        """连带失分不能隐去：学生要知道这几步是同一处错误导致的，
        否则会误以为自己到处都是问题。"""
        data = {"full_score": 10, "steps": [
            _step(1, "设元", 5, hit="miss", error_type="method"),
            _step(2, "求解", 5, hit="partial", follow=True),
        ]}
        text = rubric.rubric_attribution(rubric.parse_rubric(data))
        assert "第 1 步" in text and "方法选择不当" in text
        assert "第 2 步" in text and "连带恢复" in text

    def test_root_cause_falls_back_when_all_marked_follow_through(self):
        """模型把所有丢分步都标成 follow_through（标注有误）时，
        仍要给出一个可定位的根因，而不是空归因。"""
        data = {"full_score": 10, "steps": [_step(1, "a", 10, hit="miss", follow=True)]}
        r = rubric.parse_rubric(data)
        assert r.root_cause_step == 1
        assert "第 1 步" in rubric.rubric_attribution(r)

    def test_followup_never_leaks_when_degraded(self):
        r = rubric.RubricResult(degraded=True)
        assert rubric.rubric_followup(r) is None
        assert rubric.rubric_attribution(r) == ""

    def test_no_missed_step_yields_no_followup(self):
        data = {"full_score": 10, "steps": [_step(1, "a", 10, hit="hit")]}
        assert rubric.rubric_followup(rubric.parse_rubric(data)) is None

    def test_ratio_safe_on_zero_full_score(self):
        r = rubric.RubricResult(full_score=0, score=0)
        assert r.ratio == 0.0      # 不得抛 ZeroDivisionError


class TestRubricGrade:
    @pytest.mark.asyncio
    async def test_degrades_when_no_student_process(self, monkeypatch):
        r = await rubric.rubric_grade("  ", "题", [])
        assert r.degraded and "未写作答" in r.degraded_reason

    @pytest.mark.asyncio
    async def test_degrades_without_canonical_solution(self, monkeypatch):
        """没有定版解法就无法拆解采分点——此时给分是凭空打分，宁可不给。"""
        r = await rubric.rubric_grade("解：x=1", "题", [])
        assert r.degraded and "定版" in r.degraded_reason

    @pytest.mark.asyncio
    async def test_degrades_when_llm_unavailable(self, monkeypatch):
        from app.services.rag.chunker import Chunk

        monkeypatch.setattr(rubric, "ask_llm_json", lambda *a, **k: _ret(None))
        chunk = Chunk(id="c1", audio_id="a", start="0", end="1", text="标准解法")
        r = await rubric.rubric_grade("解：x=1", "题", [chunk])
        assert r.degraded and "不可用" in r.degraded_reason
        # 关键：降级时**不得**把学生判成错——判分器故障 ≠ 学生答错
        assert r.final_answer_correct is None
        assert r.score == 0.0 and r.steps == []

    @pytest.mark.asyncio
    async def test_happy_path_grades_by_steps(self, monkeypatch):
        from app.services.rag.chunker import Chunk

        payload = {"full_score": 10, "final_answer_correct": True, "steps": [
            _step(1, "设元", 5, hit="hit"),
            _step(2, "求解", 5, hit="partial", error_type="computation"),
        ], "summary": "方法正确，注意计算"}
        monkeypatch.setattr(rubric, "ask_llm_json", lambda *a, **k: _ret(payload))
        chunk = Chunk(id="c1", audio_id="a", start="0", end="1", text="标准解法")

        r = await rubric.rubric_grade("解：设 x=…", "题", [chunk])

        assert r.degraded is False
        assert r.score == pytest.approx(7.5)
        assert r.ratio == pytest.approx(0.75)
        assert r.first_missed == 2
        assert r.error_type == "computation"
        assert r.to_dict()["error_label"] == "计算失误（方法正确）"


async def _ret(value):
    return value
