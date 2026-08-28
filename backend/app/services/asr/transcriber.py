"""语音识别服务：远程 OpenAI 兼容 /audio/transcriptions（管理员面板可配）→ 本地 Whisper → 离线演示兜底。

真实引擎通过懒加载引入；未配置任何引擎时使用内置课堂剧本生成带毫秒时间戳的
模拟转录切片，保证 ingest 流水线端到端可演示。
"""
from __future__ import annotations

import math

from app.core import runtime_config
from app.services.rag.chunker import TranscriptSegment

# 模拟课堂剧本：（起止毫秒，台词）——与 corpus 种子切片同源
_DEMO_TIMELINE: list[tuple[int, int, str]] = [
    (725_000, 870_000, "同学们看这道题，判断这个反常积分的收敛性。"),
    (870_000, 960_000, "我们课上说过抓大头：x 趋于无穷时分母里 x 平方是大头。"),
    (1_205_000, 1_320_000, "ln x 可以扔掉，于是被积函数就相当于 x 的负二分之三次方。"),
    (1_455_000, 1_600_000, "注意！审敛最大的误区：抓大头不等于乱丢项，等价代换必须整体成立。"),
    (1_600_000, 1_680_000, "另外下限 x=1 处函数值有限，不是瑕点，别写成瑕积分。"),
    (1_862_000, 1_980_000, "p-积分判别法口诀：p 大于一收敛，p 小等于一发散。"),
    (2_510_000, 2_650_000, "变式训练：对数增长慢于任意正幂，取四分之一放缩就出来了。"),
]


class Transcriber:
    """统一入口：面板配置的远程 ASR > 本地 faster-whisper > 演示转录，任一环节失败自动降级。"""

    def __init__(self) -> None:
        self._model = None

    async def transcribe(self, audio_bytes: bytes, filename: str = "") -> list[TranscriptSegment]:
        cfg = runtime_config.effective("asr")
        if audio_bytes and cfg["api_key"] and cfg["base_url"] and cfg["model"]:
            try:
                return await self._transcribe_remote(audio_bytes, filename, cfg)
            except Exception:
                pass
        model = self._load_whisper()
        if model and audio_bytes:
            try:
                return await self._transcribe_real(audio_bytes)
            except Exception:
                pass
        return self._transcribe_demo()

    async def _transcribe_remote(self, audio_bytes: bytes, filename: str, cfg: dict) -> list[TranscriptSegment]:  # pragma: no cover
        """OpenAI 兼容转录端点返回整段文本；按标点切句并按字数比例合成毫秒时间戳。"""
        import httpx

        from app.core.security import retry_async

        async def _call() -> str:
            async with httpx.AsyncClient(timeout=180) as client:
                resp = await client.post(
                    f"{cfg['base_url'].rstrip('/')}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {cfg['api_key']}"},
                    files={"file": (filename or "audio.wav", audio_bytes)},
                    data={"model": cfg["model"]},
                )
                resp.raise_for_status()
                return str(resp.json().get("text", "")).strip()

        text = await retry_async(_call, attempts=2, exceptions=(httpx.HTTPError,))
        return _segments_from_plain_text(text)

    def _load_whisper(self):  # pragma: no cover - 重型依赖懒加载
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel

            self._model = WhisperModel("small", compute_type="int8")
        except Exception:
            self._model = False
        return self._model

    async def _transcribe_real(self, audio_bytes: bytes) -> list[TranscriptSegment]:  # pragma: no cover
        import tempfile
        import pathlib

        suffix = pathlib.Path("a.wav").suffix
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio_bytes)
            path = f.name
        segments_iter, _info = self._model.transcribe(path, language="zh", vad_filter=True)
        out: list[TranscriptSegment] = []
        for seg in segments_iter:
            out.append(
                TranscriptSegment(
                    start_ms=int(seg.start * 1000), end_ms=int(seg.end * 1000), text=seg.text.strip()
                )
            )
        pathlib.Path(path).unlink(missing_ok=True)
        return out

    @staticmethod
    def _transcribe_demo() -> list[TranscriptSegment]:
        return [TranscriptSegment(start_ms=s, end_ms=e, text=t) for s, e, t in _DEMO_TIMELINE]


_SENTENCE_BREAKS = "。！？!?；;\n"


def _segments_from_plain_text(text: str, pace_ms_per_char: float = 220.0) -> list[TranscriptSegment]:
    """把无时间戳的整段转录切成句级切片，时长按字数线性推进（展示用途足够）。"""
    sentences: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _SENTENCE_BREAKS:
            if buf.strip():
                sentences.append(buf.strip())
            buf = ""
    if buf.strip():
        sentences.append(buf.strip())
    if not sentences:
        return []

    out: list[TranscriptSegment] = []
    cursor = 0
    avg_len = sum(len(s) for s in sentences) / len(sentences)
    base_pace = max(60.0, min(400.0, 9000.0 / max(avg_len, 10)))  # 均匀铺满约 9 秒/句附近的节奏感
    for s in sentences:
        dur = max(800.0, len(s) * base_pace)
        start = int(cursor)
        end = int(cursor + dur)
        cursor += dur + 120.0
        out.append(TranscriptSegment(start_ms=start, end_ms=end, text=s))
    return out


def estimate_duration_ms(text: str) -> int:
    """粗略估算文本朗读时长，用于填充无真实音频时间戳的场景。"""
    return int(len(text) * math.ceil(220))
