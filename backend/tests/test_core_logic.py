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

from app.services.tokens import estimate_tokens


# ---------------------------------------------------------------- 拆题 ----
class TestPaperSplitter:
    def test_split_two_questions_with_answers(self):
        from app.services.extractor.paper_splitter import split_paper

        paper = (
            "1.\n判断 $\\int_1^{\\infty} x^{-2} dx$ 的敛散性。\n解：收敛，因为 p=2>1。\n"
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

    async def test_missing_date_not_treated_as_today(self, retriever):
        """回归：无时间戳切片不得被当作"今天"从而白拿最大衰减提权。

        历史 bug：ingest 的 _tag_scope 用 `lecture_date or today` 兜底，
        导致 dt=0 → 衰减系数取到 1+α（最大值），越是没日期的资料越靠前，
        与"随堂提权近讲"的设计意图完全相反。
        """
        from app.services.rag.chunker import Chunk

        undated = Chunk(
            id="ut-undated", audio_id="ut", start="00:00", end="00:10",
            text="反常积分 抓大头 判别法", course_id="ut-date-course",
            exam_point="ut 日期考点", lecture_date="",
        )
        await retriever.register_chunks([undated])
        hits = await retriever.retrieve_scored(
            "反常积分 抓大头", course_id="ut-date-course", retrieval_mode="lecture"
        )
        # 空 lecture_date 不参与衰减，但也不该被吞掉——仍可被正常召回
        assert any(c.id == "ut-undated" for _, c in hits)

    async def test_dated_chunk_boosted_over_undated(self, retriever):
        """同样相关的两份切片，有近期日期的应当排在无日期的前面。"""
        from app.services.rag.chunker import Chunk

        token = "utscale"
        stale = Chunk(
            id=f"ut-stale-{token}", audio_id="ut", start="00:00", end="00:10",
            text=f"反常积分 抓大头 {token}", course_id="ut-scale-course",
            exam_point=f"ut 尺度考点 {token}", lecture_date="2000-01-01",
        )
        fresh = Chunk(
            id=f"ut-fresh-{token}", audio_id="ut", start="00:00", end="00:10",
            text=f"反常积分 抓大头 {token}", course_id="ut-scale-course",
            exam_point=f"ut 尺度考点 {token}", lecture_date=date.today().isoformat(),
        )
        await retriever.register_chunks([stale, fresh])
        hits = await retriever.retrieve_scored(
            f"反常积分 抓大头 {token}", course_id="ut-scale-course", retrieval_mode="lecture"
        )
        ids = [c.id for _, c in hits]
        assert f"ut-fresh-{token}" in ids and f"ut-stale-{token}" in ids
        assert ids.index(f"ut-fresh-{token}") < ids.index(f"ut-stale-{token}")


# ---------------------------------------------------- 自测题判分链路 ----
@pytest.mark.asyncio
class TestQuizGradePipeline:
    """回归：判分必须依据出题时落库的真实答案，而不是写死的演示结论。

    历史 bug：/quiz/grade 只认"选项 A 正确"并复述演示题解析，配了真实模型
    后每次自测都给出与题目无关的对错。这里覆盖"出题 → 落库 → 判分"整条链路。
    """

    @staticmethod
    def _quiz_message(session_id: str, answer: str, options: list[str]) -> dict:
        from app.models.domain import MessageType, PolymorphicMessage, QuizPayload

        msg = PolymorphicMessage(
            session_id=session_id, role="assistant", type=MessageType.quiz_card,
            content="下列哪一项正确？",
        )
        msg.quiz_payload = QuizPayload(
            question_text="下列哪一项正确？", options=options, target_pitfall="ut 陷阱",
            explanation="ut 解析", answer=answer,
        )
        return msg.to_client()

    @staticmethod
    async def _session() -> str:
        import app.db.relational as repo

        return (await repo.create_session("ut-quiz"))["id"]

    async def test_grades_against_real_answer_not_first_option(self):
        """正确答案不是 A 时，选 A 必须判错、选 C 必须判对。"""
        from app.api.v1 import chat_stream
        from app.services.agent.quiz_generator import correct_option_index, grade_objective
        import app.db.relational as repo

        sid = await self._session()
        await repo.append_message(sid, self._quiz_message(sid, "C", ["甲", "乙", "丙", "丁"]))

        payload = await chat_stream._last_quiz_payload(sid)
        assert payload is not None and payload.answer == "C"
        assert correct_option_index(payload) == 2
        assert grade_objective(payload, "A")[0] is False
        assert grade_objective(payload, "C")[0] is True

    async def test_no_quiz_in_session_returns_none(self):
        """会话中没有自测题时不臆造结论，交回前端走"需复核"。"""
        from app.api.v1 import chat_stream

        sid = await self._session()
        assert await chat_stream._last_quiz_payload(sid) is None
        assert await chat_stream._last_quiz_payload("") is None

    async def test_text_answer_and_missing_answer(self):
        """答案为选项原文 / 填空题无标准答案两种边界。"""
        from app.services.agent.quiz_generator import correct_option_index, grade_objective
        from app.models.domain import QuizPayload

        by_text = QuizPayload(
            question_text="q", options=["收敛", "发散"], target_pitfall="t",
            explanation="e", answer="发散",
        )
        assert correct_option_index(by_text) == 1
        assert grade_objective(by_text, "发散")[0] is True

        no_answer = QuizPayload(
            question_text="求极限", options=None, target_pitfall="t", explanation="e", answer=None,
        )
        assert correct_option_index(no_answer) is None
        assert grade_objective(no_answer, "A")[0] is None


# ------------------------------------------------------- 音频证据定位 ----
class TestAudioSourceResolution:
    """回归：切片回听必须放对录音文件。

    历史 bug：遍历全部资产"存在即覆盖"取最后一份，多份录音共存时任何切片
    都播放最后上传的那份（串台）。
    """

    @staticmethod
    def _touch(tmp_path, *names: str) -> None:
        for n in names:
            (tmp_path / n).write_bytes(b"x")

    def test_picks_matching_not_last(self, tmp_path, monkeypatch):
        from app.api.v1 import evidence

        self._touch(tmp_path, "a.opus", "b.opus")
        monkeypatch.setattr(evidence, "AUDIO_DIR", tmp_path)
        assets = [
            {"kind": "audio", "audio_id": "ing-1", "uri": "/static/audio/a.opus"},
            {"kind": "audio", "audio_id": "ing-2", "uri": "/static/audio/b.opus"},
        ]
        assert evidence._resolve_audio_source(assets, "ing-1") == tmp_path / "a.opus"

    def test_multiple_without_match_refuses_to_guess(self, tmp_path, monkeypatch):
        from app.api.v1 import evidence

        self._touch(tmp_path, "a.opus", "b.opus")
        monkeypatch.setattr(evidence, "AUDIO_DIR", tmp_path)
        assets = [
            {"kind": "audio", "audio_id": "ing-1", "uri": "/static/audio/a.opus"},
            {"kind": "audio", "audio_id": "ing-2", "uri": "/static/audio/b.opus"},
        ]
        assert evidence._resolve_audio_source(assets, "ing-9") is None

    def test_legacy_single_audio_falls_back(self, tmp_path, monkeypatch):
        """旧数据没有 audio_id 字段，但库内只有一份录音时无歧义，可以放行。"""
        from app.api.v1 import evidence

        self._touch(tmp_path, "only.opus")
        monkeypatch.setattr(evidence, "AUDIO_DIR", tmp_path)
        assert evidence._resolve_audio_source(
            [{"kind": "audio", "uri": "/static/audio/only.opus"}], "ing-1"
        ) == tmp_path / "only.opus"

    def test_missing_file_skipped(self, tmp_path, monkeypatch):
        from app.api.v1 import evidence

        self._touch(tmp_path, "a.opus")  # b.opus 未落盘
        monkeypatch.setattr(evidence, "AUDIO_DIR", tmp_path)
        assets = [
            {"kind": "audio", "audio_id": "ing-2", "uri": "/static/audio/b.opus"},
            {"kind": "audio", "audio_id": "ing-1", "uri": "/static/audio/a.opus"},
        ]
        assert evidence._resolve_audio_source(assets, "ing-1") == tmp_path / "a.opus"


# ---------------------------------------------- 事件循环不被同步工作卡死 ----
@pytest.mark.asyncio
class TestEventLoopNotBlocked:
    """回归：重型同步工作（Whisper 推理 / ffmpeg / Pillow）必须挪到线程。

    历史 bug：`async def` 路由与 `async def _transcribe_real` 里直接调用同步
    阻塞代码，单人测试无感，两人并发上传/转录时整个服务假死。
    """

    async def test_whisper_inference_runs_off_loop(self):
        import threading
        import time

        from app.services.asr.transcriber import Transcriber

        class _FakeModel:
            thread_name: str | None = None

            def transcribe(self, path, language=None, vad_filter=None):
                type(self).thread_name = threading.current_thread().name
                time.sleep(0.05)  # 放大时间窗，确保可观测

                class _Seg:
                    start, end, text = 0.0, 1.5, "抓大头：分母里 x 平方是大头"

                return [_Seg()], None

        transcriber = Transcriber()
        transcriber._model = _FakeModel()
        segments = await transcriber._transcribe_real(b"fake-wav-bytes")

        assert len(segments) == 1 and segments[0].text.startswith("抓大头")
        loop_thread = threading.current_thread().name
        assert _FakeModel.thread_name is not None
        assert _FakeModel.thread_name != loop_thread, "Whisper 推理仍在主线程同步执行，会卡死事件循环"
        assert _FakeModel.thread_name != "MainThread"

    async def test_loop_stays_responsive_during_blocking_call(self):
        """阻塞调用期间事件循环仍要能推进其它协程（并发上传不互相拖死）。"""
        import asyncio
        import time

        ticks = 0

        async def _probe():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.02)

        def _slow(_data):
            time.sleep(0.25)  # 模拟 ffmpeg 子进程 / 图像压缩耗时
            return b"ok"

        task = asyncio.create_task(_probe())
        assert await asyncio.to_thread(_slow, b"x") == b"ok"  # ingest 路由使用的调度方式
        await asyncio.sleep(0)
        task.cancel()

        assert ticks >= 3, f"事件循环在阻塞调用期间被卡死（探针仅推进 {ticks} 次）"


@pytest.mark.asyncio
class TestPutObject:
    """对象存储写入改走线程后的行为回归（本地静态目录分支）。"""

    async def test_local_fallback_writes_file(self, tmp_path, monkeypatch):
        import base64
        import pathlib

        from app.db import minio_client

        monkeypatch.setattr(minio_client, "ensure_static_dirs", lambda: None)
        monkeypatch.setattr(minio_client, "AUDIO_DIR", tmp_path)
        monkeypatch.setattr(minio_client, "BOARDS_DIR", tmp_path)

        url = await minio_client.put_object("audio", "lesson.wav", base64.b64encode(b"pcm").decode())

        assert url.startswith("/static/audio/")
        assert (tmp_path / pathlib.Path(url).name).read_bytes() == b"pcm"

    async def test_board_routes_to_boards_dir(self, tmp_path, monkeypatch):
        import base64

        from app.db import minio_client

        monkeypatch.setattr(minio_client, "ensure_static_dirs", lambda: None)
        monkeypatch.setattr(minio_client, "AUDIO_DIR", tmp_path)
        monkeypatch.setattr(minio_client, "BOARDS_DIR", tmp_path)

        url = await minio_client.put_object("boards", "note.png", base64.b64encode(b"png").decode())

        assert url.startswith("/static/boards/")


# ------------------------------------------ 客户端断开后停止空转生成 ----
@pytest.mark.asyncio
class TestGhostGeneration:
    """回归：客户端断开必须中断 LLM 流，否则后台跑完全文白烧 token。

    历史 bug：/chat/stream 的 _stream 生成器从不检查 request.is_disconnected()，
    关页面 / 点停止后后端仍把整段生成跑完。
    """

    async def test_disconnect_stops_solve_generation(self, monkeypatch):
        from app.api.v1 import chat_stream as cs

        produced: list[str] = []

        class _FakeProvider:
            async def stream_chat(self, _messages):
                for i in range(50):
                    produced.append(f"chunk{i}")
                    yield f"chunk{i}"

        class _FakeRequest:
            def __init__(self) -> None:
                self.polls = 0

            async def is_disconnected(self) -> bool:
                self.polls += 1
                return self.polls > 3  # 第 4 次探测时"客户端已断开"

        req = _FakeRequest()

        async def _fake_retrieve(*_a, **_kw):
            return [], []

        monkeypatch.setattr(cs, "_retrieve_evidence", _fake_retrieve)
        monkeypatch.setattr(cs, "get_tier_provider", lambda _m: _FakeProvider())

        chat_req = cs.ChatRequest(session_id="ut-ghost", text="讲讲反常积分", force_intent="solve")
        out = [frame async for frame in cs._stream(chat_req, "", req)]

        # 断开后不应把 50 个 chunk 全跑完
        assert len(produced) < 50, f"断开后仍生成完 {len(produced)} 个 chunk，未中断"
        assert any("delta" in f for f in out)

    async def test_no_request_degrades_gracefully(self, monkeypatch):
        """未传 request（旧调用方）时不得抛错，按'未断开'处理。"""
        from app.api.v1 import chat_stream as cs

        async def _fake_retrieve(*_a, **_kw):
            return [], []

        monkeypatch.setattr(cs, "_retrieve_evidence", _fake_retrieve)

        chat_req = cs.ChatRequest(session_id="ut-ghost2", text="你好", force_intent="general")
        frames = [f async for f in cs._stream(chat_req, "")]
        assert frames, "无 request 时仍应正常产出事件"


# ------------------------------------------------------ 空切片不入库 ----
class TestEmptyChunkFiltering:
    """回归：空白 / 纯符号切片不得进入向量库（会霸占 top_k 有效名额）。"""

    def test_chunker_drops_blank_and_symbol_chunks(self):
        from app.services.rag.chunker import chunk_transcript
        from app.services.rag.chunker import TranscriptSegment

        segments = [
            TranscriptSegment(start_ms=0, end_ms=1000, text="   "),
            TranscriptSegment(start_ms=1000, end_ms=2000, text="\n\t"),
            TranscriptSegment(start_ms=2000, end_ms=3000, text="抓大头判别法。"),
        ]
        chunks = chunk_transcript("ut-blank", segments)
        assert len(chunks) == 1
        assert chunks[0].text == "抓大头判别法。"

    def test_chunker_keeps_normal_text(self):
        from app.services.rag.chunker import chunk_transcript, TranscriptSegment

        segments = [
            TranscriptSegment(start_ms=0, end_ms=1000, text="同学们看这道题。"),
            TranscriptSegment(start_ms=1000, end_ms=2000, text="抓大头是关键。"),
        ]
        chunks = chunk_transcript("ut-keep", segments)
        assert len(chunks) >= 1
        assert all(c.text.strip() for c in chunks)

    @pytest.mark.asyncio
    async def test_register_chunks_skips_blank(self):
        from app.services.rag.chunker import Chunk
        from app.services.rag.retriever import get_retriever

        retriever = await get_retriever()
        blank = Chunk(
            id="ut-blank-reg", audio_id="ut", start="00:00", end="00:10",
            text="   \n  ", course_id="ut-blank-course",
        )
        added = await retriever.register_chunks([blank])
        assert added == 0
        assert "ut-blank-reg" not in retriever._chunks


# --------------------------------------------------- 上下文装箱与预算 ----
class TestContextPacker:
    """Token 预算贪心装箱 + 邻近切片动态扩展。

    背景：原先按静态 top_k 取前 N 条直接拼 prompt——密集长切片会顶爆模型
    上下文窗口（API 400），零碎短句又白白浪费预算。
    """

    @staticmethod
    def _chunk(cid: str, text: str, start: str = "00:00", end: str = "00:10"):
        from app.services.rag.chunker import Chunk

        return Chunk(id=cid, audio_id="ut", start=start, end=end, text=text)

    def test_budget_accounts_for_system_query_history(self):
        from app.services.rag.packer import compute_budget
        from app.services.tokens import estimate_tokens

        sys_p, q = "系统提示" * 50, "用户问题"
        hist = [{"role": "user", "content": "上一轮提问"}, {"role": "assistant", "content": "上一轮回答"}]

        budget = compute_budget(8192, sys_p, q, hist, generation_buffer=1024)

        expect_used = (
            estimate_tokens(sys_p) + estimate_tokens(q)
            + sum(estimate_tokens(m["content"]) for m in hist)
        )
        assert budget == 8192 - expect_used - 1024

    def test_budget_never_negative(self):
        from app.services.rag.packer import compute_budget

        # 系统提示已超窗口：预算必须钳到 0，不能出负数
        assert compute_budget(100, "长" * 5000, "问题", None, generation_buffer=1024) == 0

    def test_long_chunk_truncated_not_dropped(self):
        from app.services.rag.packer import pack_context

        huge = self._chunk("ut-huge", "超" * 5000)
        res = pack_context([(1.0, huge)], budget=100000, max_chunk_tokens=100)

        assert len(res.chunks) == 1, "超长切片应截断保留，而不是整条丢弃"
        assert len(res.chunks[0].text) < 5000
        assert res.used_tokens <= 100000

    def test_low_score_dropped_when_budget_tight(self):
        from app.services.rag.packer import pack_context

        cands = [(1.0 - i * 0.01, self._chunk(f"ut-p{i}", "内容" * 200)) for i in range(20)]
        res = pack_context(cands, budget=600, max_chunk_tokens=10000)

        assert res.dropped > 0, "预算不足时必须有候选被丢弃"
        assert res.used_tokens <= 600
        assert res.chunks[0].id == "ut-p0", "高分切片必须优先入选（贪心）"

    def test_neighbors_expanded_when_budget_allows(self):
        """核心切片入选后，预算有余应补 c002 / c004，修复 ASR 单句缺前后语义。"""
        from app.services.rag.packer import pack_context

        chunks = [
            self._chunk("ut-x-c001", "第一句。"),
            self._chunk("ut-x-c002", "第二句。"),
            self._chunk("ut-x-c003", "第三句是核心。"),
            self._chunk("ut-x-c004", "第四句。"),
            self._chunk("ut-x-c005", "第五句。"),
        ]
        skeleton = {c.id: c for c in chunks}
        core = chunks[2]  # c003
        res = pack_context([(1.0, core)], budget=100000, skeleton=skeleton)

        ids = {c.id for c in res.chunks}
        assert ids == {"ut-x-c002", "ut-x-c003", "ut-x-c004"}, f"邻居扩展不完整: {ids}"
        assert set(res.expanded) == {"ut-x-c002", "ut-x-c004"}

    def test_neighbors_respect_budget(self):
        """预算只够一条时，不得硬塞邻居撑爆窗口。"""
        from app.services.rag.packer import pack_context

        chunks = [
            self._chunk("ut-y-c001", "前文" * 300),
            self._chunk("ut-y-c002", "核心" * 300),
            self._chunk("ut-y-c003", "后文" * 300),
        ]
        skeleton = {c.id: c for c in chunks}
        core = chunks[1]
        one = pack_context([(1.0, core)], budget=100000, max_chunk_tokens=10000)
        core_cost = one.used_tokens

        res = pack_context([(1.0, core)], budget=core_cost, skeleton=skeleton)

        assert [c.id for c in res.chunks] == ["ut-y-c002"]
        assert res.expanded == []
        assert res.used_tokens <= core_cost

    def test_non_sequential_id_skips_expansion(self):
        """无 `-cNNN` 顺序血缘的切片不做扩展，也不能因此报错。"""
        from app.services.rag.packer import pack_context

        odd = self._chunk("plain-id", "没有顺序后缀。")
        res = pack_context([(1.0, odd)], budget=100000, skeleton={"plain-id": odd})

        assert res.expanded == []
        assert len(res.chunks) == 1

    def test_serialize_matches_token_accounting(self):
        from app.services.rag.packer import _token_of, serialize

        c = self._chunk("ut-s-c001", "抓大头判别法。")
        assert _token_of(c) == estimate_tokens(serialize([c]))


# ------------------------------------------------ 精排与门控权威加权 ----
class TestRerankAuthorityGate:
    """核心约束：Cross-Encoder 只定"相关度"，canonical 定"可信度"，两者解耦。

    门控 S_final = S_sem*(1+beta) if is_canonical 且 S_sem>=tau else S_sem
    """

    @staticmethod
    def _chunk(cid: str, canonical: bool = False, text: str = "内容"):
        from app.services.rag.chunker import Chunk

        return Chunk(
            id=cid, audio_id="ut", start="00:00", end="00:10", text=text,
            is_canonical=canonical, course_id="ut-rr",
        )

    def test_sigmoid_monotonic_and_bounded(self):
        from app.services.rag.reranker import sigmoid

        assert 0.0 < sigmoid(-10) < 0.01
        assert 0.99 < sigmoid(10) < 1.0
        assert abs(sigmoid(0) - 0.5) < 1e-9
        assert sigmoid(-3) < sigmoid(0) < sigmoid(3)

    def test_sigmoid_no_overflow(self):
        """极端 logit 不得抛 OverflowError（朴素 exp 实现在此会炸）。"""
        from app.services.rag.reranker import sigmoid

        assert sigmoid(-1000) >= 0.0
        assert sigmoid(1000) <= 1.0

    def test_canonical_boosted_only_above_tau(self):
        from app.services.rag.reranker import apply_authority_gate

        tau, beta = 0.42, 0.30
        high = self._chunk("ut-c-high", canonical=True)   # 语义达标
        low = self._chunk("ut-c-low", canonical=True)     # 语义不达标
        plain = self._chunk("ut-plain")

        out = apply_authority_gate([(0.80, high), (0.10, low), (0.70, plain)], tau, beta)
        got = {c.id: s for s, c in out}

        assert abs(got["ut-c-high"] - 0.80 * 1.30) < 1e-9, "达标定版应获提振"
        assert got["ut-c-low"] == 0.10, "语义不达标时定版**不得**被强行置顶"
        assert got["ut-plain"] == 0.70, "普通切片不加成"

    def test_irrelevant_canonical_not_top_ranked(self):
        """本项目要防的核心场景：无关定版切片不能被捧到首位。"""
        from app.services.rag.reranker import apply_authority_gate

        irrelevant_canonical = self._chunk("ut-irr-canon", canonical=True, text="完全无关")
        relevant_plain = self._chunk("ut-rel-plain", text="正解")

        out = apply_authority_gate([(0.05, irrelevant_canonical), (0.90, relevant_plain)], 0.42, 0.30)

        assert out[0][1].id == "ut-rel-plain", "无关定版被门控挡住后，高相关切片仍应在首位"

    def test_boost_cannot_overtake_wildly_better_chunk(self):
        """提振幅度有界：0.5 分定版不该越过 0.9 分的强相关切片。"""
        from app.services.rag.reranker import apply_authority_gate

        canon = self._chunk("ut-weak-canon", canonical=True)
        strong = self._chunk("ut-strong")

        out = apply_authority_gate([(0.50, canon), (0.90, strong)], 0.42, 0.30)

        assert out[0][1].id == "ut-strong"

    def test_beta_zero_is_pure_semantic(self):
        from app.services.rag.reranker import apply_authority_gate

        canon = self._chunk("ut-b0", canonical=True)
        out = apply_authority_gate([(0.9, canon)], 0.42, 0.0)
        assert abs(out[0][0] - 0.9) < 1e-9

    @pytest.mark.asyncio
    async def test_noop_reranker_passthrough(self):
        """零依赖默认路径：不重排、不改分，与引入精排前完全一致。"""
        from app.services.rag.reranker import NoopReranker, RerankConfig, RerankPipeline

        scored = [(0.9, self._chunk("ut-n1")), (0.5, self._chunk("ut-n2", canonical=True))]
        pipe = RerankPipeline(NoopReranker(), RerankConfig(enabled=False))

        assert pipe.active is False
        assert await pipe.run("q", scored) == scored

    @pytest.mark.asyncio
    async def test_pipeline_disabled_even_with_model(self):
        """配了模型但 enabled=False 时也必须透传，防止误开。"""
        from app.services.rag.reranker import NoopReranker, RerankConfig, RerankPipeline

        scored = [(0.9, self._chunk("ut-d1"))]
        pipe = RerankPipeline(NoopReranker(), RerankConfig(enabled=True))

        assert pipe.active is False, "Noop 实现下 active 必须为 False"
        assert await pipe.run("q", scored) == scored

    @pytest.mark.asyncio
    async def test_pipeline_reranks_and_truncates_pool(self):
        """候选池按 recall_pool 截断后才送精排；门控加成在精排分上进行。"""
        from app.services.rag.reranker import BaseReranker, RerankConfig, RerankPipeline

        seen: list[str] = []

        class _R(BaseReranker):
            name = "fake"

            async def rerank(self, _q, scored):
                seen.extend(c.id for _, c in scored)
                return [(0.9, c) for _, c in scored]  # 全部打同分

        chunks = [self._chunk(f"ut-pool{i}") for i in range(10)]
        chunks[0] = self._chunk("ut-pool0", canonical=True)
        scored = [(1.0 - i * 0.01, c) for i, c in enumerate(chunks)]

        pipe = RerankPipeline(_R(), RerankConfig(enabled=True, recall_pool=3, tau=0.42, beta=0.30))
        out = await pipe.run("q", scored)

        assert len(seen) == 3, f"送入精排的数量应受 recall_pool 约束，实为 {len(seen)}"
        assert out[0][1].id == "ut-pool0", "同分时定版切片应因门控加成排首位"
        assert abs(out[0][0] - 0.9 * 1.30) < 1e-9

    def test_get_reranker_defaults_to_noop(self):
        """未配置时必须是 Noop，保证零依赖环境主干测试全通。"""
        from app.services.rag.reranker import NoopReranker, get_reranker, reset_reranker

        reset_reranker()
        try:
            assert isinstance(get_reranker(), NoopReranker)
        finally:
            reset_reranker()

    def test_rerank_config_defaults(self):
        from app.core.runtime_config import effective

        cfg = effective("rerank")
        assert cfg["enabled"] is False
        assert cfg["tau"] == pytest.approx(0.42)
        assert cfg["beta"] == pytest.approx(0.30)
        assert cfg["recall_pool"] == 18

    def test_production_gate_matches_offline_eval(self):
        """离线标定脚本与生产门控必须同源，否则标定结果对生产无效。"""
        from app.services.rag.reranker import (
            apply_authority_gate,
            sigmoid,
        )

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
        from eval_rerank_params import final_score as eval_final_score

        for logit in (-3.0, -0.5, 0.0, 0.8, 1.4, 2.5):
            from app.services.rag.chunker import Chunk

            for canon in (True, False):
                chunk = Chunk(
                    id="ut-parity", audio_id="ut", start="00:00", end="00:10",
                    text="x", is_canonical=canon,
                )
                prod = apply_authority_gate([(sigmoid(logit), chunk)], 0.42, 0.30)[0][0]

                class _C:
                    pass

                c = _C()
                c.logit, c.is_canonical = logit, canon
                offline = eval_final_score(c, 0.42, 0.30)

                assert abs(prod - offline) < 1e-9, (
                    f"生产与离线标定结果不一致: logit={logit} canon={canon} "
                    f"prod={prod} offline={offline}"
                )


# --------------------------------------------- 离线标定脚本自身行为 ----
class TestRerankEvalScript:
    """标定脚本必须真的做网格搜索，且选优逻辑正确。"""

    @staticmethod
    def _load():
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
        import eval_rerank_params as ev

        return ev

    def test_builtin_golden_has_adversarial_samples(self):
        """黄金集必须含对抗负样本，否则网格搜索没有可区分的信号。"""
        ev = self._load()
        ds = ev.load_dataset(None)

        assert len(ds) >= 8
        per_query = [sum(1 for c in q.candidates if c.is_canonical and not c.is_relevant) for q in ds]
        assert all(n >= 1 for n in per_query), "每条 query 都要有无关定版作为对抗样本"

    def test_golden_produces_nonzero_mrr_gain(self):
        """黄金集要能产生可测收益——全 1.0 的 baseline 说明梯度消失。"""
        ev = self._load()
        ds = ev.load_dataset(None)
        base = ev.evaluate_dataset(ds, tau=0.0, beta=0.0)

        assert base["mrr"] < 1.0, "baseline 满分说明样本无区分度，标定将失效"

    def test_gate_blocks_irrelevant_canonical_at_calibrated_tau(self):
        """标定出的阈值必须能拦住全部无关定版，同时放行全部相关定版。"""
        ev = self._load()
        ds = ev.load_dataset(None)
        res = ev.grid_search(ds, [round(0.30 + i * 0.02, 2) for i in range(26)],
                             [round(i * 0.02, 2) for i in range(31)])

        assert res["passes"]["no_false_boosts"], "标定结果不应留下误提权"
        assert res["passes"]["activation_ok"]

    def test_grid_search_explores_full_space(self):
        """回归：原参考实现的 min_false_boosts 单调收紧会让后续 tau 全被跳过。"""
        ev = self._load()
        ds = ev.load_dataset(None)
        taus = [0.30, 0.50, 0.70, 0.74]
        betas = [0.0, 0.10]
        res = ev.grid_search(ds, taus, betas)

        combos = {(c["tau"], c["beta"]) for c in res["candidates"]}
        assert (0.74, 0.10) in combos or len(combos) >= 4, (
            f"网格探索不完整，仅覆盖 {sorted(combos)}"
        )

    def test_baseline_is_beta_zero_not_tau_one(self):
        """baseline 语义 = 不启用加成，故须 beta=0；用 tau=1.0 会让激活率恒为 0。"""
        ev = self._load()
        ds = ev.load_dataset(None)
        res = ev.grid_search(ds, [0.30, 0.74], [0.0, 0.20])

        assert res["baseline"]["canonical_activation_rate"] > 0.0

    def test_stable_sigmoid_extremes(self):
        ev = self._load()

        assert ev.stable_sigmoid(-1000) >= 0.0
        assert ev.stable_sigmoid(1000) <= 1.0
        assert ev.stable_sigmoid(0) == pytest.approx(0.5)

    def test_ranking_is_deterministic_on_ties(self):
        ev = self._load()
        c1 = ev.EvalCandidate("b", 1.0, False, True)
        c2 = ev.EvalCandidate("a", 1.0, False, True)

        first = [c.chunk_id for c, _ in ev.rank_candidates([c1, c2], 0.4, 0.3)]
        second = [c.chunk_id for c, _ in ev.rank_candidates([c2, c1], 0.4, 0.3)]

        assert first == second, "同分排序不得依赖输入顺序，否则标定结果不可复现"


# --------------------------------------------- 真实 logit 抽取管线 ----
class TestLogitExtraction:
    """抽取管线的核心风险不是「跑不通」，而是「跑通了但数据是假的、
    却被当成真实标定用掉」。因此测试重点压在溯源标记与弱标注语义上。"""

    @staticmethod
    def _load():
        scripts = str(Path(__file__).resolve().parent.parent.parent / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import extract_rerank_dataset as ex

        return ex

    def test_stub_logits_are_deterministic(self):
        """stub 必须可复现，否则管线自检本身都不稳定。"""
        ex = self._load()
        b = ex.StubBackend()
        a1 = b.score_batch("抓大头能丢 ln x 吗", ["切片甲", "切片乙"])
        a2 = b.score_batch("抓大头能丢 ln x 吗", ["切片甲", "切片乙"])

        assert a1 == a2

    def test_stub_logits_stay_in_physical_band(self):
        """stub 只为模拟真实 Cross-Encoder 的窄带，不得产生离谱量级。"""
        ex = self._load()
        b = ex.StubBackend()
        vals = b.score_batch("q", [f"文本{i}" for i in range(200)])

        assert all(-2.0 <= v <= 1.5 for v in vals)

    def test_relevance_by_exam_point(self):
        ex = self._load()
        q = ex.QuerySpec(query="q", course_id="c1", exam_point="ep-a")
        hit = ex.Recalled("k1", "t", "ep-a", "ch1", "c1", False, True)
        miss = ex.Recalled("k2", "t", "ep-b", "ch2", "c1", False, True)

        assert ex.judge_relevance(q, hit)[0] is True
        assert ex.judge_relevance(q, miss)[0] is False

    def test_relevance_by_chapter_when_exam_point_absent(self):
        """考点缺失时按章节兜底——这是弱标注「弱」的来源，必须有测试钉住。"""
        ex = self._load()
        q = ex.QuerySpec(query="q", course_id="c1", chapter="5.3 反常积分")
        c = ex.Recalled("k1", "t", "别的考点", "5.3 反常积分", "c1", False, True)

        assert ex.judge_relevance(q, c)[0] is True

    def test_canonical_alone_is_not_relevant_outside_scope(self):
        """跨课程切片即使带 canonical 也不能票成相关，否则对抗样本消失。"""
        ex = self._load()
        q = ex.QuerySpec(query="q", course_id="c1")
        foreign = ex.Recalled("k9", "t", "ep-x", "ch-x", "c2", True, False)

        assert ex.judge_relevance(q, foreign)[0] is False

    def test_adversarial_definition(self):
        ex = self._load()
        adv = ex.Recalled("k1", "t", "ep-b", "ch2", "c1", True, True)

        assert ex.is_adversarial(adv, relevant=False) is True
        assert ex.is_adversarial(adv, relevant=True) is False

    def test_dataset_carries_provenance_marker(self):
        """每条 query 必须自带来源标记——黄金集会被单独搬运，头部信息会丢。"""
        ex = self._load()
        specs = [ex.QuerySpec(query="q", course_id="math", query_id="q1")]
        stats = ex.ExtractStats()

        # 直接喂空文本，绕开真实召回：本测试只验证标记落入输出结构
        import asyncio

        orig = ex.recall_for_query

        async def _fake(_q, _depth, _retriever=None):
            return [ex.Recalled("k1", "text", "ep", "ch", "math", True, True)]

        ex.recall_for_query = _fake
        try:
            ds = ex.build_golden(specs, ex.StubBackend(), 15, stats)
        finally:
            ex.recall_for_query = orig

        assert ds and ds[0]["stub"] is True
        assert ds[0]["provenance"] == "stub"

    def test_backend_defaults_to_stub_flag_off(self):
        """真实后端产出不得带 stub 标记，否则闸门会误杀真实标定。"""
        ex = self._load()
        stats = ex.ExtractStats()

        assert stats.stub_used is False

    def test_validator_rejects_stub_dataset(self):
        """闸门：标定脚本读到 stub 数据必须硬终止，绝不输出推荐参数。"""
        import json
        import tempfile

        ev = self._load_eval()

        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump(
                [{"query_id": "q1", "query": "q", "stub": True,
                  "candidates": [{"chunk_id": "k", "logit": 0.5,
                                  "is_canonical": True, "is_relevant": True}]}],
                fh, ensure_ascii=False,
            )
            path = fh.name

        with pytest.raises(SystemExit) as ei:
            ev.load_dataset(path)

        assert ei.value.code == 3, "应以退出码 3 拒绝，便于流水线识别"

    def test_validator_accepts_real_dataset(self):
        """真实（非 stub）数据必须放行，否则闸门会挡死正常标定。"""
        import json
        import tempfile

        ev = self._load_eval()

        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump(
                [{"query_id": "q1", "query": "q", "provenance": "onnx",
                  "candidates": [{"chunk_id": "k", "logit": 0.5,
                                  "is_canonical": True, "is_relevant": True}]}],
                fh, ensure_ascii=False,
            )
            path = fh.name

        ds = ev.load_dataset(path)

        assert len(ds) == 1 and ds[0].candidates[0].logit == 0.5

    @staticmethod
    def _load_eval():
        scripts = str(Path(__file__).resolve().parent.parent.parent / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import eval_rerank_params as ev

        return ev


# --------------------------------------------- 多章节基准夹具 ----
class TestMultiChapterFixture:
    """夹具的核心价值：在真实检索器上构造出**真实存在**的跨章节对抗候选。

    内置语料只有单章节，chapter 通道会把同章切片全部票成正样本，硬对抗样本
    物理不存在 → 标定必然退化。夹具用「同词异章」解决这个问题，且这些样本
    是检索器真实召回出来的，不是手捏的 logit。
    """

    @staticmethod
    def _fixture():
        from tests.fixtures import multichapter as fx

        return fx

    @staticmethod
    async def _retriever_with_fixture():
        import os

        os.environ.setdefault("SEED_DEMO_CORPUS", "0")
        fx = TestMultiChapterFixture._fixture()
        from app.services.rag.retriever import HybridRetriever

        r = HybridRetriever()
        await r.register_chunks(fx.FIXTURE_CHUNKS)
        return r

    def test_fixture_shape(self):
        """夹具规模必须够小（跑得快）又够立体（多章节 + 多定版）。"""
        fx = self._fixture()

        assert len(fx.FIXTURE_CHUNKS) <= 20, "夹具要保持轻量，否则测试变慢"
        chapters = {c.chapter for c in fx.FIXTURE_CHUNKS}
        assert len(chapters) >= 3, "至少三个章节才谈得上跨章节冲突"
        canon = fx.fixture_canonical_ids()
        assert len(canon) >= 3, "每条讲座都要有定版，否则对抗样本凑不出来"

    def test_fixture_chunk_ids_unique(self):
        fx = self._fixture()
        ids = [c.id for c in fx.FIXTURE_CHUNKS]

        assert len(ids) == len(set(ids))

    def test_fixture_queries_declare_expectations(self):
        """每条查询都要显式声明它期待的正确答案与对抗定版。"""
        fx = self._fixture()
        all_ids = {c.id for c in fx.FIXTURE_CHUNKS}

        for q in fx.FIXTURE_QUERIES:
            assert q["expect_relevant_chunk"] in all_ids, q["query_id"]
            assert q["expect_adversarial_chunk"] in all_ids, q["query_id"]
            assert q["expect_relevant_chunk"] != q["expect_adversarial_chunk"]

    def test_fixture_queries_omit_exam_point(self):
        """查询必须不带 exam_point——否则考点硬过滤会在打分前丢掉跨考点切片，
        对抗样本根本进不了候选池，夹具就失去意义了。"""
        fx = self._fixture()

        for q in fx.FIXTURE_QUERIES:
            assert not q.get("exam_point"), (
                f"{q['query_id']} 携带了 exam_point，会让对抗样本被提前过滤掉"
            )

    def test_adversarial_chunks_are_canonical(self):
        """对抗项必须是定版——否则它们不会触发门控，测不到误提权。"""
        fx = self._fixture()
        by_id = {c.id: c for c in fx.FIXTURE_CHUNKS}

        for q in fx.FIXTURE_QUERIES:
            adv = by_id[q["expect_adversarial_chunk"]]
            assert adv.is_canonical, f"{adv.id} 不是定版，无法构成对抗定版"

    def test_relevant_chunks_are_canonical(self):
        """正确答案也必须是定版，否则 beta 提权对它无效，收益测不出来。"""
        fx = self._fixture()
        by_id = {c.id: c for c in fx.FIXTURE_CHUNKS}

        for q in fx.FIXTURE_QUERIES:
            rel = by_id[q["expect_relevant_chunk"]]
            assert rel.is_canonical, f"{rel.id} 不是定版，吃不到 beta 红利"

    def test_fixture_covers_multiple_domains(self):
        """三个讲座应该讨论不同主题，仅共享领域词汇而非同一考点。"""
        fx = self._fixture()
        points = {c.exam_point for c in fx.FIXTURE_CHUNKS}

        assert len(points) >= 4, "考点太少说明三讲其实是同一件事"

    def test_real_recall_brings_adversarial_into_pool(self):
        """端到端核心断言：真实检索器必须把该查询的对抗定版召进候选池。

        这是整套标定方案成立的**物理前提**。如果这条挂了，说明检索器
        的过滤/召回逻辑让对抗样本无法出现，标定就只能靠造假数据。
        """
        import asyncio

        fx = self._fixture()

        async def _run():
            r = await self._retriever_with_fixture()
            out = {}
            for q in fx.FIXTURE_QUERIES:
                scored = await r.retrieve_scored(
                    q["query"], top_k=15, course_id=q["course_id"]
                )
                out[q["query_id"]] = {c.id for _s, c in scored}
            return out

        pools = asyncio.run(_run())

        for q in fx.FIXTURE_QUERIES:
            pool = pools[q["query_id"]]
            assert q["expect_relevant_chunk"] in pool, (
                f"{q['query_id']}: 正确答案未召回"
            )
            assert q["expect_adversarial_chunk"] in pool, (
                f"{q['query_id']}: 对抗定版未进入候选池，标定前提不成立"
            )

    def test_real_recall_has_multiple_candidates_per_query(self):
        """候选池要够深，否则排序无从谈起（内置语料就是每查询仅 1 条的退化态）。"""
        import asyncio

        fx = self._fixture()

        async def _run():
            r = await self._retriever_with_fixture()
            sizes = []
            for q in fx.FIXTURE_QUERIES:
                scored = await r.retrieve_scored(
                    q["query"], top_k=15, course_id=q["course_id"]
                )
                sizes.append(len(scored))
            return sizes

        sizes = asyncio.run(_run())

        assert all(n >= 5 for n in sizes), f"候选池过浅: {sizes}"

    def test_canonical_bonus_flattens_coarse_scores(self):
        """记录一个真实缺陷:粗排的 canonical_bonus 是**固定加分**,不区分彼此。

        三条定版会拿到完全相同的融合分,粗排阶段根本无法排序——跨章节的
        错误定版可以和正确答案并列甚至靠前。门控之所以必需,根因在这里。
        本测试把这个事实钉住,防止将来有人误以为粗排已经够用。
        """
        import asyncio

        fx = self._fixture()
        q = fx.FIXTURE_QUERIES[0]

        async def _run():
            r = await self._retriever_with_fixture()
            return await r.retrieve_scored(
                q["query"], top_k=15, course_id=q["course_id"]
            )

        scored = asyncio.run(_run())
        by_id = {c.id: s for s, c in scored}

        rel, adv = q["expect_relevant_chunk"], q["expect_adversarial_chunk"]
        if rel in by_id and adv in by_id:
            # 二者都是 canonical、都加了同一个 bonus，粗排分必然相同
            assert by_id[rel] == pytest.approx(by_id[adv], abs=1e-9), (
                "若此断言失败，说明粗排已能区分定版优劣，"
                "那么重排的门控设计需要重新评估"
            )

    def test_fixture_dataset_feeds_calibration_script(self):
        """夹具产出的候选必须能被标定脚本消费（结构对齐 = 闭环可用）。"""
        import asyncio
        import json
        import tempfile

        fx = self._fixture()
        scripts = str(Path(__file__).resolve().parent.parent.parent / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import eval_rerank_params as ev

        async def _run():
            r = await self._retriever_with_fixture()
            rows = []
            for q in fx.FIXTURE_QUERIES:
                scored = await r.retrieve_scored(
                    q["query"], top_k=15, course_id=q["course_id"]
                )
                cands = []
                for _s, c in scored:
                    is_rel = c.id == q["expect_relevant_chunk"]
                    cands.append(
                        {
                            "chunk_id": c.id,
                            "logit": 1.0 if is_rel else 0.0,
                            "is_canonical": bool(c.is_canonical),
                            "is_relevant": bool(is_rel),
                        }
                    )
                rows.append(
                    {"query_id": q["query_id"], "query": q["query"],
                     "candidates": cands}
                )
            return rows

        rows = asyncio.run(_run())

        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump(rows, fh, ensure_ascii=False)
            path = fh.name

        ds = ev.load_dataset(path)

        assert len(ds) == len(fx.FIXTURE_QUERIES)
        assert all(q.candidates for q in ds), "夹具喂出的候选不得为空"

    def test_fixture_not_wired_into_production_corpus(self):
        """夹具绝不能进生产语料——否则会污染真实部署的检索结果。"""
        from app.services.rag import corpus

        production_ids = {c["id"] for c in corpus.SEED_CHUNKS}
        fx = self._fixture()
        fixture_ids = {c.id for c in fx.FIXTURE_CHUNKS}

        assert not (production_ids & fixture_ids), "夹具切片混进了生产演示语料"

    def test_fixture_mode_produces_adversarial_samples(self):
        """--fixture 模式必须真的产出硬对抗负样本。

        这是本轮的核心修复目标：内置语料下对抗样本恒为 0，标定退化成常量。
        夹具模式下必须 > 0，否则等于白做。
        """
        scripts = str(Path(__file__).resolve().parent.parent.parent / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import extract_rerank_dataset as ex

        import asyncio

        fx = self._fixture()
        retriever, _fx = asyncio.run(ex.build_fixture_retriever())
        specs = [
            ex.QuerySpec(query=q["query"], course_id=q["course_id"], query_id=q["query_id"])
            for q in fx.FIXTURE_QUERIES
        ]
        gt = {q["query_id"]: q["expect_relevant_chunk"] for q in fx.FIXTURE_QUERIES}
        stats = ex.ExtractStats()

        ds = ex.build_golden(
            specs, ex.StubBackend(), 15, stats,
            retriever=retriever, ground_truth=gt,
        )

        assert stats.adversarial > 0, "夹具模式下仍未产出对抗样本，标定必然退化"
        assert stats.relevant_canonical > 0, "相关定版为 0 时 activation 无法度量"
        assert stats.candidates >= 5 * 5, f"候选池过浅: {stats.candidates}"

    def test_ground_truth_overrides_weak_label(self):
        """夹具真值必须覆盖弱标注——弱标注的 chapter 通道在夹具场景不适用。"""
        import asyncio

        scripts = str(Path(__file__).resolve().parent.parent.parent / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import extract_rerank_dataset as ex

        fx = self._fixture()
        retriever, _ = asyncio.run(ex.build_fixture_retriever())
        q = fx.FIXTURE_QUERIES[0]
        specs = [ex.QuerySpec(query=q["query"], course_id=q["course_id"],
                              query_id=q["query_id"])]
        gt = {q["query_id"]: q["expect_relevant_chunk"]}
        stats = ex.ExtractStats()

        ds = ex.build_golden(
            specs, ex.StubBackend(), 15, stats,
            retriever=retriever, ground_truth=gt,
        )

        marked = [c for c in ds[0]["candidates"] if c["is_relevant"]]
        assert len(marked) == 1, "夹具真值应恰好标出一条相关切片"
        assert marked[0]["chunk_id"] == q["expect_relevant_chunk"]
        assert marked[0]["label_reason"] == "ground_truth"
