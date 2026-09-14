# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""多模态资产入库流水线接口：录音/板书/文字上传 → 异步 ASR/VLM/切片/向量化。

上传安全：魔数嗅探（拒绝伪装扩展名的文件）、单文件大小上限、UUID 重命名落盘。
"""
from __future__ import annotations

import asyncio
import base64
import pathlib
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form

import app.db.relational as repo
from app.api.v1.auth import AuthUser, current_user_optional
from app.core.gate import DEFAULT_ACQUIRE_TIMEOUT_S, DEFAULT_LEASE_TTL_S, DistributedGate, GateTimeout
from app.core.membership import plan_for, storage_limit_bytes
from app.core.tenancy import resolve_tenant
from app.db.minio_client import put_object
from app.services.asr.hotwords import correct
from app.services.asr.transcriber import Transcriber
from app.services.extractor.difficulty import score
from app.services.extractor.pitfall import extract_from_chunks
from app.services.rag.aligner import align_boards
from app.services.rag.chunker import Chunk, chunk_transcript
from app.services.rag.retriever import get_retriever
from app.services.vlm.ocr_engine import OCREngine

router = APIRouter()

# ---- 上传限制：音频 200MB / 图片 20MB / 文字 1MB ----
LIMITS = {"audio": 200 * 1024 * 1024, "board": 20 * 1024 * 1024, "image": 20 * 1024 * 1024, "text": 1 * 1024 * 1024}


def _sniff_magic(raw: bytes, media_type: str) -> bool:
    """真实文件魔数校验（纯 stdlib）：保证内容与声明的媒体类型一致。"""
    head = raw[:16]
    if media_type == "audio":
        # WAV(RIFF) / MP3(ID3 或帧同步) / MP4-M4A(ftyp) / FLAC / OGG
        if head[:4] == b"RIFF" and raw[8:12] == b"WAVE":
            return True
        if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
            return True
        if head[4:8] == b"ftyp":
            return True
        return head[:4] == b"fLaC" or head[:4] == b"OggS"
    if media_type in ("board", "image"):
        # PNG / JPEG / GIF / WebP(RIFF....WEBP) / BMP / HEIC(ftypheic/heix/mif1)
        sig = {
            b"\x89PNG": True,
            b"\xff\xd8\xff": True,
            b"GIF8": True,
            b"BM": True,
        }
        for prefix, ok in sig.items():
            if head.startswith(prefix):
                return ok
        if head[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return True
        if head[4:8] == b"ftyp" and raw[8:12] in (b"heic", b"heix", b"mif1", b"hevc"):
            return True
        return False
    return True  # text 类型由解码校验兜底


def _validate_upload(raw: bytes, media_type: str, filename: str) -> None:
    limit_key = "image" if media_type == "board" else media_type
    limit = LIMITS.get(limit_key, 20 * 1024 * 1024)
    if len(raw) > limit:
        mb = limit // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"文件超过上限（{media_type} 最大 {mb}MB）")
    if len(raw) < 8:
        raise HTTPException(status_code=400, detail="文件内容为空或过小")
    if media_type in ("audio", "board") and not _sniff_magic(raw, media_type):
        raise HTTPException(status_code=415, detail=f"文件内容与类型不符（伪扩展名已拦截）：{filename[:60]}")
    # 文件名只用于展示，入库统一重命名，路径穿越无从谈起；仍过滤控制字符
    if re.search(r"[\x00-\x1f]", filename or ""):
        raise HTTPException(status_code=400, detail="文件名包含非法字符")


TEXT_CHUNK_TARGET = 220  # 文字素材单切片目标字数


def _split_text_chunks(text: str) -> list[str]:
    """把粘贴的长文按段落聚合为教学切片：优先空行分段，长段落再按句切窗。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    units: list[str] = []
    for para in paragraphs:
        if len(para) <= TEXT_CHUNK_TARGET:
            units.append(para)
            continue
        sentences = re.split(r"(?<=[。！？!?；;])", para)
        buf = ""
        for sent in sentences:
            # 硬切：单个无句读单元本身超限时按长度切片，防止单切片膨胀到百 KB 级
            while len(sent) > TEXT_CHUNK_TARGET * 2:
                units.append(sent[:TEXT_CHUNK_TARGET])
                sent = sent[TEXT_CHUNK_TARGET:]
            if buf and len(buf) + len(sent) > TEXT_CHUNK_TARGET:
                units.append(buf)
                buf = sent.lstrip()
            else:
                buf += sent
        if buf.strip():
            units.append(buf)
    # 相邻过短片段合并，避免碎片切片
    merged: list[str] = []
    for u in units:
        if merged and len(merged[-1]) + len(u) < 60:
            merged[-1] = f"{merged[-1]}\n{u}"
        else:
            merged.append(u)
    return merged[:60]


@router.post("/ingest")
async def ingest(
    user: AuthUser = Depends(current_user_optional),
    session_id: str = Form(...),
    media_type: str = Form("audio"),  # audio | board | text
    file: UploadFile | None = File(None),
    lecture_date: str = Form(""),
    text_content: str = Form(""),
    subject: str = Form("未分类"),   # 学科：数学 / 物理 / …
    course_id: str = Form("default"),  # 课程唯一标识，检索强作用域
    chapter: str = Form(""),          # 章节
):
    """同步执行轻量流水线（演示规模），返回处理摘要。重型部署走 workers/tasks.py 的 Celery 版本。

    - audio  上传录音文件 → ASR 转录 → 切片对齐
    - board  上传板书图片 → VLM 识别 → 入库
    - text   直接提交文字素材（text_content 表单字段，或 .txt/.md 文件）→ 切片入库
    """
    raw = await file.read() if file is not None else b""
    # ---- 部署档位保命锁：入库并发闸门 ----
    # 解析 PDF/长录音极耗 CPU。eco 档容量 1：有任务在切片向量化时新上传排队，
    # 禁止并发解压与批量 Embedding，保证主聊天 API 始终拿得到 CPU 时间片。
    # 配额/鉴权等轻量校验在锁外完成，重活（压缩 → ASR/VLM → 切片 → 向量化）入锁。
    #
    # 闸门必须是**跨副本**的：进程内信号量在 N 个 K8s 副本下会放大成 N× 并发，
    # 档位上限等于失效。Redis 租约版见 core/gate.py（未配 Redis 时自动回落进程内）。
    gate = _ingest_gate()
    try:
        lease = await gate.acquire(timeout_s=DEFAULT_ACQUIRE_TIMEOUT_S)
    except GateTimeout:
        raise HTTPException(status_code=503, detail="入库任务排队超时，请稍后重试")
    try:
        return await _ingest_locked(
            user=user, session_id=session_id, media_type=media_type, file=file,
            lecture_date=lecture_date, text_content=text_content, subject=subject,
            course_id=course_id, chapter=chapter, raw=raw,
        )
    finally:
        await gate.release(lease)


_INGEST_GATE: DistributedGate | None = None


def _ingest_gate() -> DistributedGate:
    """按部署档位惰性创建入库闸门（容量 = max_concurrent_ingest）。

    容量在首次调用时定型：多副本共享的名额数必须稳定，运行中改会让已经在
    跑的任务和新任务用不同口径计数。改档位请重启（与 profiles 的其余保命锁一致）。
    """
    global _INGEST_GATE
    if _INGEST_GATE is None:
        from app.core import profiles

        _INGEST_GATE = DistributedGate(
            "ingest",
            int(profiles.effective()["max_concurrent_ingest"]),
            lease_ttl_s=DEFAULT_LEASE_TTL_S,
        )
    return _INGEST_GATE


async def _ingest_locked(
    *,
    user: AuthUser,
    session_id: str,
    media_type: str,
    file: UploadFile | None,
    lecture_date: str,
    text_content: str,
    subject: str,
    course_id: str,
    chapter: str,
    raw: bytes,
):
    if not user.anonymous:
        owner = await repo.get_session_owner(session_id)
        if owner and owner != user.id:
            raise HTTPException(status_code=403, detail="无权向该会话入库素材")
    if file is not None:
        plan = plan_for(user.tier)
        # 会员配额：单文件上限取「档位上限 ∨ 全局硬限」较小者；总存储超配额拒绝
        single_cap = min(int(plan["max_upload_mb"]), LIMITS.get("image" if media_type == "board" else media_type, 20 * 1024 * 1024)) * 1024 * 1024
        if len(raw) > single_cap:
            raise HTTPException(status_code=413, detail=f"{user.tier} 档单文件上限 {single_cap // (1024*1024)}MB，升级会员可提升")
        if not user.anonymous:
            used = await repo.storage_used_bytes(user.id)
            limit = storage_limit_bytes(user.tier)
            if used + len(raw) > limit:
                raise HTTPException(
                    status_code=403,
                    detail=f"存储空间已满（{used // (1024*1024)}MB/{limit // (1024*1024)}MB），升级会员可获得更大空间",
                )
        _validate_upload(raw, media_type, file.filename or "")

    # ---- 双轨压缩时序 ----
    # 上传期(前置标准化)：图片→高质量 WebP（识别输入）；音频→16kHz 单声道 PCM（无损送 ASR）
    # 归档期(识别后深压)：图片→Q75~80 WebP；音频→24~32kbps Opus 后再入对象存储
    from app.services.media import compressor

    archive_raw = raw
    recognize_b64 = base64.b64encode(raw).decode() if raw else ""
    compress_meta: dict | None = None
    if file is not None and raw:
        # 压缩/转码是同步阻塞的（ffmpeg 子进程 + Pillow 图像运算），必须挪到线程里，
        # 否则在 async 路由中直接调用会卡死整个事件循环——两人同时上传就集体假死。
        try:
            if media_type == "board":
                normalized, up_meta = await asyncio.to_thread(compressor.compress_image, raw)  # 上传期标准化（高质量）
                archive_raw, ar_meta = await asyncio.to_thread(compressor.archive_image, normalized)  # 归档期深压 Q75~80
                recognize_b64 = base64.b64encode(normalized).decode()  # VLM 用标准化版识别
                compress_meta = {"upload": up_meta, "archive": ar_meta}
            elif media_type == "audio":
                suffix = pathlib.Path(file.filename or "a.wav").suffix or ".wav"
                pcm, up_meta = await asyncio.to_thread(compressor.normalize_audio_for_asr, raw, suffix)  # 16k 单声道 PCM 送 ASR
                archive_raw, ar_meta = await asyncio.to_thread(compressor.compress_audio, raw, suffix)  # 归档期 Opus 深压
                raw = pcm  # ASR 输入 = 无损 PCM
                compress_meta = {"upload": up_meta, "archive": ar_meta}
        except Exception:
            compress_meta = {"skipped": "压缩异常，保留原始文件"}  # 压缩失败不影响入库主链路
            archive_raw = raw

    def _tag_scope(chunk_list: list[Chunk]) -> None:
        """为切片补齐作用域元数据（租户隔离 + 课程隔离 + 时间衰减的数据基础）。

        **租户由服务端权威注入**（`resolve_tenant(user)`，来源为签名 JWT + 用户
        记录），绝不接受任何客户端参数——否则租户 A 只要在表单里填 B 的
        tenant/course 就能把资料写进 B 的知识库。

        未提供授课日期时**不补今天**：衰减项要求 lecture_date 非空才生效，
        若填今天则 dt=0、衰减系数取最大值，无时间戳的资料反而被当成"最新"
        获得提权，信号完全反向。留空即不参与衰减（中性）。
        """
        tenant = resolve_tenant(user)
        for c in chunk_list:
            c.tenant_id = tenant
            c.subject = subject
            c.course_id = course_id or "default"
            c.chapter = chapter
            c.lecture_date = lecture_date or ""

    if media_type == "text":
        note_text = (text_content or "").strip()
        filename = file.filename or "" if file else ""
        if not note_text and raw:
            try:
                note_text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(status_code=400, detail="文字文件需为 UTF-8 编码的 txt/md") from None
        note_text = note_text.strip()
        if not note_text:
            raise HTTPException(status_code=400, detail="文字内容为空")

        url = await put_object("boards", filename or f"note-{uuid.uuid4().hex[:6]}.txt",
                               base64.b64encode(note_text.encode()).decode())
        chunks = _build_text_chunks(note_text, filename or "文字笔记")
        _tag_scope(chunks)
        pitfalls = extract_from_chunks(chunks)

        retriever = await get_retriever()
        added = await retriever.register_chunks(chunks)
        asset = await repo.add_asset(
            session_id,
            {
                "owner": None if user.anonymous else user.id,
                "size_bytes": len(note_text.encode()),
                "kind": "text",
                "uri": url,
                "filename": filename or f"{note_text[:12]}…",
                "lecture_date": lecture_date,
                "chunk_count": added,
                "pitfalls": pitfalls[:3],
            },
        )
        return {
            "asset": asset,
            "chunks_added": added,
            "pitfalls_extracted": pitfalls[:5],
            "sample_chunks": [
                {"start": c.start, "end": c.end, "text": c.text[:60], "exam_point": c.exam_point}
                for c in chunks[:4]
            ],
        }

    # 归档：识别后深压版本入对象存储
    url = (
        await put_object(media_type, file.filename or "upload", base64.b64encode(archive_raw).decode())
        if file is not None
        else ""
    )


    if media_type == "audio":
        segments = await Transcriber().transcribe(raw, file.filename or "")  # raw=16k PCM
        for seg in segments:
            seg.text = correct(seg.text)
        audio_id = f"ing-{abs(hash(file.filename + course_id)) % 10_000}"
        chunks = chunk_transcript(audio_id, segments)
        chunks = align_boards(chunks, board_count=12)
        for c in chunks:
            c.difficulty = score(c.text)
            c.pitfalls = []
        _tag_scope(chunks)
        pitfalls = extract_from_chunks(chunks)
    else:
        ocr = await OCREngine().recognize(recognize_b64)
        text = ocr.get("problem_text") or ocr.get("latex", "")
        audio_id = "board-only"
        chunks = [
            Chunk(
                id=f"bd-{uuid.uuid4().hex[:10]}",
                audio_id=audio_id,
                start="00:00",
                end="00:00",
                text=text,
                board_index=1,
                board_caption=f"板书上传 · {file.filename or ''}",
                exam_point="板书推导要点",
                difficulty=score(text),
            )
        ]
        _tag_scope(chunks)
        pitfalls = []

    retriever = await get_retriever()
    added = await retriever.register_chunks(chunks)

    asset = await repo.add_asset(
        session_id,
        {
            "owner": None if user.anonymous else user.id,
            "size_bytes": len(raw),
            "kind": media_type,
            "uri": url,
            "filename": file.filename or "",
            # 音频与切片组的关联键：切片回听时据此精确定位录音文件，
            # 否则多份录音共存时无法判断该放哪一份（历史 bug）
            "audio_id": audio_id,
            "lecture_date": lecture_date,
            "chunk_count": added,
            "pitfalls": pitfalls[:3],
        },
    )
    return {
        "asset": asset,
        "chunks_added": added,
        "pitfalls_extracted": pitfalls[:5],
        "sample_chunks": [
            {"start": c.start, "end": c.end, "text": c.text[:60], "exam_point": c.exam_point}
            for c in chunks[:4]
        ],
    }


def _build_text_chunks(note_text: str, title: str) -> list[Chunk]:
    pieces = _split_text_chunks(note_text)
    note_id = f"txt-note-{uuid.uuid4().hex[:6]}"  # 同一次上传共享虚拟音频组，便于证据面板归组
    chunks: list[Chunk] = []
    for i, piece in enumerate(pieces, start=1):
        chunks.append(
            Chunk(
                id=f"txt-{uuid.uuid4().hex[:10]}",
                audio_id=note_id,
                start="00:00",
                end="00:00",
                text=piece.replace("\n", " "),
                board_index=None,
                board_caption=f"文字笔记 · {title} 第{i}段",
                exam_point=f"笔记要点 {i}",
                difficulty=score(piece),
            )
        )
    return chunks


@router.get("/assets")
async def assets(session_id: str) -> list[dict]:
    return await repo.list_assets(session_id)
