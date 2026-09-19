# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""多模态资产入库流水线接口：录音/板书/文字上传 → 异步 ASR/VLM/切片/向量化。

上传安全：魔数嗅探（拒绝伪装扩展名的文件）、单文件大小上限、UUID 重命名落盘。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import pathlib
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse

import app.db.relational as repo
from app.api.v1.auth import AuthUser, current_user_optional
from app.core.gate import DEFAULT_ACQUIRE_TIMEOUT_S, DEFAULT_LEASE_TTL_S, DistributedGate, GateTimeout
from app.core.membership import plan_for, storage_limit_bytes
from app.core.tenancy import resolve_tenant
from app.db.minio_client import delete_object, put_object
from app.services.asr.hotwords import correct
from app.services.asr.transcriber import Transcriber
from app.services.extractor.difficulty import score
from app.services.extractor.pitfall import extract_from_chunks
from app.services.ops import audit as audit_log
from app.services.rag.aligner import align_boards
from app.services.rag.chunker import Chunk, chunk_transcript
from app.services.rag.retriever import get_retriever
from app.services.vlm.ocr_engine import OCREngine

router = APIRouter()

_logger = logging.getLogger("app.api.ingest")

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


async def _register_with_rollback(
    retriever, chunks: list[Chunk], session_id: str, asset_meta: dict
) -> tuple[int, dict]:
    """写向量 → 写资产记录，**第二步失败则回滚第一步**。

    为什么顺序不能反：资产记录（关系库）是业务可见的"真相源"，向量只是检索索引。
    先写库后写向量的话，失败会留下"有记录、检索不到"的切片（用户看到素材却问不出内容）；
    当前顺序失败时留下的是"有向量、无记录"的孤儿切片——检索能命中但任何列表都查不到，
    更难排查。因此保持"先索引后落库"，并在落库失败时**补偿删除**已写入的向量与内存索引。

    `chunk_count` 由本函数按**实际写入量**填写（调用方拿不到这个值，容易写成
    "切片总数"从而把被跳过的空切片也算进去）。
    """
    added = await retriever.register_chunks(chunks)
    meta = dict(asset_meta)
    meta["chunk_count"] = added
    try:
        asset = await repo.add_asset(session_id, meta)
    except Exception:
        # 补偿：向量库 + 进程内索引一起清，否则本进程仍会继续返回幽灵结果
        await retriever.unregister_chunks(
            [c.id for c in chunks], tenants={c.tenant_id for c in chunks}
        )
        raise
    return added, asset


async def _compensate_object(url: str) -> None:
    """对象存储侧补偿：上传成功但后续入库失败时，把文件物理删掉。

    为什么必须有这一步：`_register_with_rollback` 只回滚**向量**，而文件在更早的
    `put_object` 就已经落盘/落桶了。assets 表是素材列表与存储配额的唯一真相源，
    落库失败后这个对象再没有任何记录指向它——不可见、不计费、用户删不掉，
    每次失败必然累积一个，直到磁盘或桶被撑爆。这是**确定性**泄漏，不是概率问题。

    为什么包一层而不是直接 await delete_object：补偿动作必须**无法抛出异常**。
    本函数运行在 except 分支里，若清理自身再抛错（MinIO 抖动、权限不足），
    原始异常就被顶掉了——排障时看到的是"错误的原因的原因"。
    残留（删不掉的对象）只能靠离线 GC 兜底；这里保证的是正常路径零泄漏。
    """
    if not url:
        return
    try:
        await delete_object(url)
    except Exception:
        pass


@router.post("/ingest")
async def ingest(
    request: Request,
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
    """同步执行轻量流水线（**遗留契约**），返回处理摘要。

    ⚠️ 生产入口请走 `POST /ingest/tasks`：本端点会在整个流水线期间独占 HTTP
    连接，链路动辄数十秒到数分钟，必然撞网关超时（Nginx/CDN 默认 60s 断连），
    且断连后结果无人接收也无法找回。保留它是为了兼容既有调用方与测试，
    不是推荐路径。
    """
    raw = await file.read() if file is not None else b""
    # ---- 部署档位保命锁：入库并发闸门 ----
    # 解析 PDF/长录音极耗 CPU。eco 档容量 1：有任务在切片向量化时新上传排队，
    # 禁止并发解压与批量 Embedding，保证主聊天 API 始终拿得到 CPU 时间片。
    # 配额/鉴权等轻量校验在锁外完成，重活（压缩 → ASR/VLM → 切片 → 向量化）入锁。
    #
    # 闸门必须是**跨副本**的：进程内信号量在 N 个 K8s 副本下会放大成 N× 并发，
    # 档位上限等于失效。Redis 租约版见 core/gate.py（未配 Redis 时自动回落进程内）。
    try:
        lease = await _acquire_slot(DEFAULT_ACQUIRE_TIMEOUT_S)
    except GateTimeout:
        raise HTTPException(status_code=503, detail="入库任务排队超时，请稍后重试")
    try:
        result = await _ingest_locked(
            user=user, session_id=session_id, media_type=media_type, file=file,
            lecture_date=lecture_date, text_content=text_content, subject=subject,
            course_id=course_id, chapter=chapter, raw=raw,
        )
    finally:
        await _release_slot(lease)

    # 审计埋点放在成功后：课件上传是"谁往机构知识库里放了什么"的核心问题，
    # 机构尽调必问。只记元信息（类型/大小/归属），不记内容——
    # 内容已经在切片里，审计表是责任记录不是数据仓库。
    await audit_log.record(
        "ingest.upload",
        actor="anonymous" if user.anonymous else user.id,
        actor_role="anonymous" if user.anonymous else (user.role or "member"),
        tenant_id=resolve_tenant(user),
        target=f"{media_type}:{(file.filename if file is not None else 'text')[:120]}",
        ip=audit_log.ip_of(request),
        detail={
            "session_id": session_id,
            "bytes": len(raw),
            "subject": subject,
            "course_id": course_id,
            "chapter": chapter,
        },
    )
    return result


# ------------------------------------------------------------ 任务化入库 ----
# 后台任务的强引用池：`asyncio.create_task` 返回的 Task 若不被引用，可能在
# GC 时被提前回收（官方文档明确点名的坑）。任务结束即自动摘除，池子不会膨胀。
_BACKGROUND_TASKS: set[asyncio.Task] = set()

# 本进程正在跑的入库任务数。为什么要自己数：闸门容量可以在运行中热调变小，
# 而已经拿到的信号量许可没法撤回——只有自己计数才能即时生效"降到 1"这类
# 降级（尤其是内存保护层触发时，必须立刻停止放行新任务，而不是等旧任务跑完）。
_INGEST_INFLIGHT = 0

# 任务状态阶段文案：给前端进度条和 SSE 用，避免各端各写一套。
_STAGE_HINTS = {
    "queued": "已排队，等待计算资源",
    "running": "正在处理（转录/识别/切片/向量化）",
    "succeeded": "入库完成",
    "failed": "入库失败",
}

# SSE 进度流的存活上限（毫秒）。为什么必须是上限：僵尸任务（持有进程已死）
# 永远不会有人把它推向终态，不封顶的话 SSE 连接会一直挂到网关超时。
# 取 RANGES 上界 3600s 而不是随意写个数：它是"客户端愿意等"，而
# task_zombie_timeout_s（默认 600s，可热调到上限 3600s）是"系统何时判死"。
# 前者必须 ≥ 后者，否则任务尚未被判死、实时通道就先断了。
# （07e8cb7 重构误删了这个常量的定义，留下 SSE 里的引用——上线即 NameError，
#   由 tests/test_ingest_task_metrics.py::TestSSENameError 钉死。）
_TASK_HEARTBEAT_TIMEOUT_MS = 60 * 60 * 1000


def _task_store():
    """任务读写模块（repo）。抽出来是为了让测试能整体替换 storage 实现。"""
    return repo


@router.post("/ingest/tasks")
async def submit_ingest_task(
    request: Request,
    user: AuthUser = Depends(current_user_optional),
    session_id: str = Form(...),
    media_type: str = Form("audio"),  # audio | board | text
    file: UploadFile | None = File(None),
    lecture_date: str = Form(""),
    text_content: str = Form(""),
    subject: str = Form("未分类"),
    course_id: str = Form("default"),
    chapter: str = Form(""),
    idempotency_key: str = Form(""),
) -> dict:
    """提交入库任务，**立即返回句柄**，流水线转入后台执行。

    为什么端点要做成任务化：压缩 → ASR/VLM → 切片 → 向量化这条链路耗时
    数十秒到数分钟，挂在 HTTP 请求上有三个无解的后果——网关必然超时、
    断连后结果丢失、重传没有去重依据。任务化后：

    - 响应在登记完任务后立刻返回（毫秒级），不再陪流水线耗到超时
    - 进度与结果落 `ingest_tasks` 表，客户端可轮询或收 SSE
    - `idempotency_key` 相同即复用同一任务，重传不会重复入库

    `idempotency_key` 由客户端为"同一次上传"生成并重试保持不变（典型做法：
    上传前生成 UUID 存本地）。未提供时按 tenant+文件名+大小派生一个，
    至少挡住最机械的双击重复提交。
    """
    raw = await file.read() if file is not None else b""
    tenant = resolve_tenant(user)
    now = int(time.time() * 1000)
    key = idempotency_key or f"auto:{tenant}:{file.filename if file is not None else 'text'}:{len(raw)}"

    task_id = uuid.uuid4().hex[:16]
    record = {
        "id": task_id,
        "session_id": session_id,
        "owner": None if user.anonymous else user.id,
        "tenant_id": tenant,
        "idempotency_key": key,
        "media_type": media_type,
        "filename": (file.filename if file is not None else "") or "",
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "finished_at": 0,
        "error": None,
        "result_json": "",
    }
    stored, created = await _task_store().create_ingest_task(record)

    if created:
        _spawn_pipeline(
            task_id=stored["id"], user=user, session_id=session_id, media_type=media_type,
            file=file, lecture_date=lecture_date, text_content=text_content,
            subject=subject, course_id=course_id, chapter=chapter, raw=raw,
        )

    return {"task_id": stored["id"], "status": stored["status"],
            "stage": _STAGE_HINTS.get(stored["status"], "")}


def _spawn_pipeline(**kwargs) -> None:
    """在后台执行流水线，并把 Task 放进模块级池持有强引用。

    用进程内 Task 而不是 Celery：单机/单容器部署无需 Redis 即可获得完整的
    任务语义（状态、幂等、失败可见）。代价是**进程重启会丢进行中的任务**——
    这类任务由 `reap_zombie_tasks()` 标记失败，客户端不会无限等待。
    """
    coro = _run_pipeline(**kwargs)
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


# 直方图允许的 media_type 标签取值。为什么必须白名单：media_type 是客户端
# 表单字段，直接拿它当标签值，等于把"生成几条时间序列"的控制权交给调用方——
# 一次打错的请求就会永久新增一条序列，直到 _MAX_SERIES 封顶把别的指标挤出去。
_KNOWN_MEDIA_TYPES = ("audio", "board", "text")


def _observe_task_duration(media_type: str, outcome: str, seconds: float) -> None:
    """记录一次任务的全栈耗时；遥测失败绝不能打断业务路径（同 circuit 的口径）。"""
    try:
        from app.core import metrics

        label = media_type if media_type in _KNOWN_MEDIA_TYPES else "other"
        metrics.observe("ingest_task_duration_seconds", seconds,
                        {"media_type": label, "outcome": outcome})
    except Exception:
        _logger.debug("observe task duration failed", exc_info=True)


async def _run_pipeline(*, task_id: str, user: AuthUser, session_id: str, media_type: str,
                        file: UploadFile | None, lecture_date: str, text_content: str,
                        subject: str, course_id: str, chapter: str, raw: bytes) -> None:
    """后台流水线包装：无论成败都要把终态写回任务表。

    为什么失败也要写：这里没有调用栈可见性——后台任务抛出的异常只会打到日志，
    客户端看不到任何东西。不落库的话，用户只会看到任务永远停在 running，
    而不知道是 ASR 挂了还是参数错了。
    """
    store = _task_store()
    started = time.perf_counter()
    await store.update_ingest_task(task_id, {"status": "running"})
    try:
        lease = await _acquire_slot(DEFAULT_ACQUIRE_TIMEOUT_S)
    except GateTimeout:
        await store.update_ingest_task(task_id, {
            "status": "failed", "error": "入库任务排队超时，请稍后重试",
            "finished_at": int(time.time() * 1000),
        })
        # 排队等待也要计时：这段时间内不 touch updated_at，在收尸眼里与执行
        # 时间同权——漏记它会低估 P99，校出来的阈值会误杀还在排队的健康任务。
        _observe_task_duration(media_type, "queue_timeout", time.perf_counter() - started)
        return
    try:
        result = await _ingest_locked(
            user=user, session_id=session_id, media_type=media_type, file=file,
            lecture_date=lecture_date, text_content=text_content, subject=subject,
            course_id=course_id, chapter=chapter, raw=raw,
            heartbeat=_phase_hook(task_id),
        )
    except Exception as exc:
        await _release_slot(lease)
        await store.update_ingest_task(task_id, {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "finished_at": int(time.time() * 1000),
        })
        # 失败样本必须计时：它们恰恰集中在逼近收尸阈值的长尾，排除掉等于按
        # 最乐观的情况算 P99，阈值一定会偏紧。
        _observe_task_duration(media_type, "failed", time.perf_counter() - started)
        return
    await _release_slot(lease)
    await store.update_ingest_task(task_id, {
        "status": "succeeded",
        "result_json": json.dumps(result, ensure_ascii=False),
        "finished_at": int(time.time() * 1000),
    })
    _observe_task_duration(media_type, "succeeded", time.perf_counter() - started)


async def active_task_count() -> int:
    """当前未终态的入库任务数（饱和度指标）。

    进程内在跑的 + 库里仍处于 running 的都要算：前者是本副本的实际负载，
    后者包含"别的副本正在跑"和"待收尸的僵尸"两类。只看进程内会把多副本
    部署下的真实积压低估成 1/N。
    """
    running = await repo.list_zombie_ingest_tasks(0)  # cutoff=0 → 所有 running 任务
    return max(_INGEST_INFLIGHT, len(running))


def _phase_hook(task_id: str):
    """生成阶段心跳回调：每推进一个阶段就 touch 一次 `updated_at`。

    为什么必须分阶段 touch：收尸判据是"无心跳时长"，而心跳只有显式更新才会动。
    一个合法跑了 40 分钟的长录音，若中途从不刷新，就与"卡死 40 分钟"在数据上
    完全同形——收尸只能二选一地误杀或漏杀。阶段推进时 touch 之后，"慢"和"死"
    才第一次可区分。
    """

    async def _touch(phase: str) -> None:
        await repo.update_ingest_task(task_id, {"updated_at": int(time.time() * 1000)})
        _logger.debug("ingest task %s -> phase %s", task_id, phase)

    return _touch


async def _acquire_slot(timeout_s: float) -> str:
    """准入：跨副本租约（Redis/进程内）+ 本进程当前并发计数，两者都过才放行。

    两道闸的分工：DistributedGate 管"多副本共享名额"，本地计数管"热调后的即时
    生效"。只用前者时，把并发从 4 降到 1 后，旧信号量仍有 4 个许可在路上，
    降级要到旧任务全部结束才真正起作用——内存保护等不起。
    """
    from app.core import task_policy

    capacity = int(task_policy.effective()["ingest_max_concurrency"])
    gate = _ingest_gate()
    lease = await gate.acquire(timeout_s=timeout_s)
    global _INGEST_INFLIGHT
    deadline = time.monotonic() + timeout_s
    while _INGEST_INFLIGHT >= capacity and time.monotonic() < deadline:
        await asyncio.sleep(0.25)
    _INGEST_INFLIGHT += 1
    return lease


async def _release_slot(lease: str) -> None:
    global _INGEST_INFLIGHT
    _INGEST_INFLIGHT = max(0, _INGEST_INFLIGHT - 1)
    await _ingest_gate().release(lease)


@router.get("/ingest/tasks/{task_id}")
async def get_ingest_task(task_id: str, user: AuthUser = Depends(current_user_optional)) -> dict:
    """查询单个入库任务状态（含失败原因与成功摘要）。

    越权保护：任务记录带 owner，匿名任务（演示模式）才允许跨会话读取；
    登录用户只能查自己的任务——否则任务 id 就成了遍历他人素材的入口。
    """
    record = await _task_store().get_ingest_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if not user.anonymous and record.get("owner") and record["owner"] != user.id:
        raise HTTPException(status_code=403, detail="无权查看该任务")
    return {**record, "stage": _STAGE_HINTS.get(record["status"], "")}


@router.get("/ingest/tasks/{task_id}/events")
async def stream_ingest_task(task_id: str, user: AuthUser = Depends(current_user_optional)):
    """任务进度的 SSE 流：状态变化时推送一次，终态后关闭连接。

    轮询表而非订阅 Pub/Sub：任务以分钟计，1 秒轮询的延迟完全可以接受，
    换来的是不用管订阅连接的重连与消息丢失——丢了就不会自愈，而
    "状态停在 running"是持续的错误结果。
    """
    async def _gen():
        last = ""
        deadline = time.time() + _TASK_HEARTBEAT_TIMEOUT_MS / 1000
        while time.time() < deadline:
            record = await _task_store().get_ingest_task(task_id)
            if record is None:
                yield _sse("task", {"task_id": task_id, "status": "not_found"})
                return
            if not user.anonymous and record.get("owner") and record["owner"] != user.id:
                yield _sse("task", {"task_id": task_id, "status": "forbidden"})
                return
            payload = json.dumps({
                "task_id": task_id, "status": record["status"],
                "stage": _STAGE_HINTS.get(record["status"], ""),
                "error": record.get("error"), "result": record.get("result"),
            }, ensure_ascii=False)
            if payload != last:
                yield _sse("task", json.loads(payload))
                last = payload
            if record["status"] in ("succeeded", "failed"):
                return
            await asyncio.sleep(1.0)

    return StreamingResponse(
        _gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def reap_zombie_tasks(older_than_ms: int | None = None) -> int:
    """收尸：把 running 但心跳停摆的任务标记失败（进程重启/被杀后的残留）。

    阈值**取自运行时配置**（`task_zombie_timeout_s`，随部署档位给默认值），
    不再是代码里的常量。1C2G 上跑得慢、8C16G 上跑得快、弱网 ASR 更慢——
    同一个固定阈值不可能同时适配这三种环境。

    这些任务永远等不到回调——持有它们的进程已经不在了。不处理的话客户端会
    一直轮询到超时，且状态表里永远留着 running 僵尸，运维无法判断真实负载。
    返回被收尸的任务数。
    """
    from app.core import task_policy

    if older_than_ms is None:
        older_than_ms = int(task_policy.effective()["task_zombie_timeout_s"]) * 1000
    store = _task_store()
    zombies = await store.list_zombie_ingest_tasks(older_than_ms)
    now = int(time.time() * 1000)
    for task in zombies:
        await store.update_ingest_task(task["id"], {
            "status": "failed",
            "error": "任务执行进程中断（重启或被杀），请重新提交",
            "finished_at": now,
        })
    if zombies:
        # 幸存者偏差补偿：僵尸没有正常回调，永远进不了时长直方图。若不单独
        # 计数，"这次收尸到底误杀了多少健康的慢任务"在线是完全失明的——而
        # 这恰恰是调 `task_zombie_timeout_s` 时最需要回答的问题。
        try:
            from app.core import metrics

            metrics.inc("ingest_tasks_zombie_total", value=float(len(zombies)))
        except Exception:
            _logger.debug("count zombie reap failed", exc_info=True)
    return len(zombies)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


_INGEST_GATE: DistributedGate | None = None
_INGEST_GATE_CAPACITY: int = -1


def _ingest_gate() -> DistributedGate:
    """按**当前生效**并发数取闸门；容量变化即重建（热调无需重启）。

    为什么允许重建：容量来自三层弹性配置（档位 → 面板热调 → 内存保护层），
    管理员改完必须立刻生效，否则"弹性"名不副实。旧闸门对象被丢弃后，已发放
    的租约仍会走 `_release_slot()` 归还到旧对象——不泄漏名额，只是那个旧对象
    不再参与新准入。

    ⚠️ 重建只约束**新任务**：已在跑的任务不会因为容量从 4 降到 1 而被打断
    （强行打断等于丢半个素材）。真正的即时性由 `_INGEST_INFLIGHT` 计数保证。
    """
    global _INGEST_GATE, _INGEST_GATE_CAPACITY
    from app.core import task_policy

    capacity = int(task_policy.effective()["ingest_max_concurrency"])
    if _INGEST_GATE is None or _INGEST_GATE_CAPACITY != capacity:
        _INGEST_GATE = DistributedGate(
            "ingest",
            capacity,
            lease_ttl_s=DEFAULT_LEASE_TTL_S,
        )
        _INGEST_GATE_CAPACITY = capacity
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
    heartbeat: Callable[[str], Awaitable[None]] | None = None,
):
    """入库主流程（在闸门内执行）。

    `heartbeat` 是阶段心跳回调：每个阶段推进时调用一次（uploading →
    transcribing → chunking → indexing），用于刷新任务的 `updated_at`。
    同步调用方（遗留 `POST /ingest`）不传即为 None，行为不变。
    """

    async def _beat(phase: str) -> None:
        if heartbeat is not None:
            await heartbeat(phase)

    await _beat("uploading")
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
        # 从这里开始，任何一步失败都已留下一个"没有资产记录指向它"的对象文件，
        # 必须补偿删除；成功路径则原样保留（补偿只在 except 分支触发）。
        try:
            await _beat("chunking")
            chunks = _build_text_chunks(note_text, filename or "文字笔记")
            _tag_scope(chunks)
            pitfalls = extract_from_chunks(chunks)

            retriever = await get_retriever()
            await _beat("indexing")
            added, asset = await _register_with_rollback(
                retriever,
                chunks,
                session_id,
                {
                    "owner": None if user.anonymous else user.id,
                    "size_bytes": len(note_text.encode()),
                    "kind": "text",
                    "uri": url,
                    "filename": filename or f"{note_text[:12]}…",
                    "lecture_date": lecture_date,
                    "pitfalls": pitfalls[:3],
                },
            )
        except Exception:
            await _compensate_object(url)
            raise

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


    # 与文本分支同一口径：对象已落存储之后（含 ASR/VLM 识别本身失败）
    # 的任何异常都必须把文件删掉，否则留下无记录指向的孤儿对象。
    try:
        await _beat("transcribing")
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

        await _beat("chunking")
        retriever = await get_retriever()
        await _beat("indexing")
        added, asset = await _register_with_rollback(
            retriever,
            chunks,
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
                "pitfalls": pitfalls[:3],
            },
        )
    except Exception:
        await _compensate_object(url)
        raise

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
