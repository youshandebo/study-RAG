# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""对象存储孤儿文件：失败补偿回归测试（P0 资源泄漏，故障注入为证）。

为什么单独立一份文件
--------------------
`test_catalog_sync.py::TestIngestRollback` 钉住的是**向量侧**补偿；本文件钉的是
同一条写路径上的**对象存储侧**。两者是同一个缺陷的两个残留：

    写入顺序 = put_object(文件先落盘/落桶) → ASR/VLM → 向量化 → 资产落库

落库失败时，向量会被 `_register_with_rollback` 补删，但那个已经写进 MinIO /
本地静态目录的归档文件没有任何记录指向它——assets 表是配额与素材列表的唯一
真相源，孤儿文件不计费、不可见，会**每次失败必然**累积一个，直到磁盘/桶爆仓。
这不是概率性泄漏，是确定性泄漏，因此必须被abspath测试钉死。

测试为什么不能用 mock 假造 put_object
-------------------------------------
补偿的关键是"物理文件到底还在不在"。mock 掉写入后断言被调用过，只能证明
代码路径走通，证明不了文件消失。所以这里把 `minio_client` 的模块级目录常量
整体重定向到 pytest 临时目录，让 `put_object` 走**真实的本地落盘分支**
（`MINIO_ENDPOINT` 为空时的生产兜底路径），断言直接看 `iterdir()` 的结果。
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy.exc import SQLAlchemyError

import app.db.minio_client as storage
from app.api.v1 import ingest as ingest_api
from app.api.v1.auth import AuthUser
from app.core.config import get_settings
from app.services.rag.chunker import TranscriptSegment


@pytest.fixture(autouse=True)
def no_minio(monkeypatch):
    """关掉 MinIO：走本地静态目录分支——那是未配对象存储时的真实生产路径。"""
    monkeypatch.setenv("MINIO_ENDPOINT", "")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "")
    monkeypatch.setenv("MINIO_SECRET_KEY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def local_storage(monkeypatch, tmp_path):
    """把静态目录常量重定向到临时目录，返回 (boards, audio) 供断言文件增删。"""
    boards = tmp_path / "boards"
    audio = tmp_path / "audio"
    monkeypatch.setattr(storage, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(storage, "BOARDS_DIR", boards)
    monkeypatch.setattr(storage, "AUDIO_DIR", audio)
    return boards, audio


class _StubRetriever:
    """检索器替身：只记录写入/回滚调用，避免测试连真向量库。"""

    def __init__(self) -> None:
        self.registered: list[str] = []
        self.unregistered: list[str] = []

    async def register_chunks(self, chunks):
        self.registered.extend(c.id for c in chunks)
        return len(chunks)

    async def unregister_chunks(self, chunk_ids, tenants=None):
        self.unregistered.extend(chunk_ids)
        return len(chunk_ids)


def _anon_user() -> AuthUser:
    return AuthUser("u-1", "a@b.com", "free", anonymous=True)


def _patch_retriever(monkeypatch, stub: _StubRetriever) -> None:
    async def _factory():
        return stub

    monkeypatch.setattr(ingest_api, "get_retriever", _factory)


def _make_add_asset_boom(monkeypatch):
    """模拟主库写入失败：关系库宕机/连接被打断是幂等重试之外最常见的真实故障。"""

    async def _boom(session_id, meta):
        raise SQLAlchemyError("connection reset by peer")

    monkeypatch.setattr(ingest_api.repo, "add_asset", _boom)


_TEXT_CONTENT = "泰勒展开把光滑函数局部多项式化，是近似计算与极限求解的核心工具。"


class TestIngestObjectCompensation:
    """入库主链路（ingest.py）：落库失败 → 已上传对象必须物理删除。"""

    @pytest.mark.asyncio
    async def test_text_upload_failure_removes_uploaded_object(self, monkeypatch, local_storage):
        """文本素材：文件已落盘，资产记录写不进去 → 对象不得残留。"""
        boards, _audio = local_storage
        stub = _StubRetriever()
        _patch_retriever(monkeypatch, stub)
        _make_add_asset_boom(monkeypatch)

        with pytest.raises(SQLAlchemyError):
            await ingest_api._ingest_locked(
                user=_anon_user(), session_id="s-orphan-text", media_type="text",
                file=None, lecture_date="", text_content=_TEXT_CONTENT,
                subject="数学", course_id="default", chapter="", raw=b"",
            )

        # 向量侧补偿必须照旧生效（不要把对象补偿改成了向量补偿的替代品）
        assert stub.unregistered, "向量补偿不得被对象补偿覆盖"
        remaining = [p.name for p in boards.iterdir()]
        assert remaining == [], f"落库失败后对象目录必须为空，实际残留：{remaining}"

    @pytest.mark.asyncio
    async def test_board_upload_failure_removes_uploaded_object(self, monkeypatch, local_storage):
        """板书图片：同一缺陷的媒体分支（图片经压缩、OCR 后才到落库）。"""
        boards, _audio = local_storage
        stub = _StubRetriever()
        _patch_retriever(monkeypatch, stub)
        _make_add_asset_boom(monkeypatch)

        class _FakeFile:
            filename = "board.png"

        class _FakeOCR:
            async def recognize(self, image_b64):
                return {"problem_text": "求 lim(x→0) sin x / x = 1", "latex": ""}

        monkeypatch.setattr(ingest_api, "OCREngine", _FakeOCR)

        # 合法 PNG 魔数 + 填充：既要通过 _validate_upload 的魔数嗅探，
        # 又要让压缩环节失败时能被保命降级（archive_raw 退回原始字节）。
        raw = b"\x89PNG\r\n\x1a\n" + b"0" * 512

        with pytest.raises(SQLAlchemyError):
            await ingest_api._ingest_locked(
                user=_anon_user(), session_id="s-orphan-board", media_type="board",
                file=_FakeFile(), lecture_date="", text_content="",
                subject="数学", course_id="default", chapter="", raw=raw,
            )

        remaining = [p.name for p in boards.iterdir()]
        assert remaining == [], f"板书入库失败后对象目录必须为空，实际残留：{remaining}"

    @pytest.mark.asyncio
    async def test_success_path_keeps_object(self, monkeypatch, local_storage):
        """成功路径绝对不能被误删：补偿只在异常分支触发，不能变成无条件清理。"""
        boards, _audio = local_storage
        stub = _StubRetriever()
        _patch_retriever(monkeypatch, stub)

        async def _ok(session_id, meta):
            return {"id": "asset-ok"}

        monkeypatch.setattr(ingest_api.repo, "add_asset", _ok)

        out = await ingest_api._ingest_locked(
            user=_anon_user(), session_id="s-ok-text", media_type="text",
            file=None, lecture_date="", text_content=_TEXT_CONTENT,
            subject="数学", course_id="default", chapter="", raw=b"",
        )

        assert out["asset"]["id"] == "asset-ok"
        assert len(list(boards.iterdir())) == 1, "成功入库的文件必须完整保留"
        assert not stub.unregistered


class TestWorkerObjectCompensation:
    """Celery 路径（workers/tasks.py）必须与 API 路径同一口径补补偿。"""

    def test_audio_pipeline_failure_removes_uploaded_object(self, monkeypatch, local_storage):
        _boards, audio = local_storage
        # workers/ 位于 backend 根目录而非 app 包下（sys.path 已在文件头部补过 backend 根）
        from workers import tasks

        class _FakeTranscriber:
            async def transcribe(self, raw, filename):
                return [TranscriptSegment(start_ms=0, end_ms=1500,
                                          text="今天我们讲泰勒展开的余项估计")]

        stub = _StubRetriever()

        async def _fake_get_retriever():
            return stub

        async def _boom(session_id, meta):
            raise SQLAlchemyError("connection reset by peer")

        monkeypatch.setattr("app.services.asr.transcriber.Transcriber", _FakeTranscriber)
        monkeypatch.setattr("app.services.rag.retriever.get_retriever", _fake_get_retriever)
        monkeypatch.setattr("app.db.relational.add_asset", _boom)

        # 合法 WAV 魔数（RIFF....WAVE）， workers 路径不做魔数校验，仅为真实文件语义
        raw = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 256

        with pytest.raises(SQLAlchemyError):
            tasks.run_ingest_pipeline("s-orphan-audio", "audio", raw, "lesson.wav")

        remaining = [p.name for p in audio.iterdir()]
        assert remaining == [], f"Worker 入库失败后对象目录必须为空，实际残留：{remaining}"


class TestDeleteObjectContract:
    """delete_object 自身的契约：幂等、不抛异常、绝不越界删白名单之外的路径。"""

    @pytest.mark.asyncio
    async def test_deletes_local_static_file(self, local_storage):
        boards, _audio = local_storage
        boards.mkdir(parents=True, exist_ok=True)
        target = boards / "note.txt"
        target.write_bytes(b"orphan")

        await storage.delete_object("/static/boards/note.txt")

        assert not target.exists(), "白名单内的静态文件必须被物理删除"

    @pytest.mark.asyncio
    async def test_second_delete_is_idempotent(self, local_storage):
        boards, _audio = local_storage
        boards.mkdir(parents=True, exist_ok=True)
        (boards / "gone.txt").write_bytes(b"x")

        await storage.delete_object("/static/boards/gone.txt")
        await storage.delete_object("/static/boards/gone.txt")  # 重复补偿不得炸

    @pytest.mark.asyncio
    async def test_refuses_paths_outside_whitelist(self, local_storage, tmp_path):
        """补偿入口绝不能变成任意文件删除：非 /static 前缀的 URL 一律不碰。"""
        boards, _audio = local_storage
        boards.mkdir(parents=True, exist_ok=True)
        victim = tmp_path / "secret.txt"
        victim.write_bytes(b"keep me")

        await storage.delete_object("../secret.txt")
        await storage.delete_object("/static/boards/../../secret.txt")
        await storage.delete_object(str(victim))

        assert victim.exists(), "越界路径必须被拒绝删除"
        assert await storage.delete_object("/static/boards/../../secret.txt") is False

    @pytest.mark.asyncio
    async def test_empty_url_is_noop(self, local_storage):
        """未上传对象（如纯文本无文件）时 url 为空，不得抛异常。"""
        assert await storage.delete_object("") is False
