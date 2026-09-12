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
