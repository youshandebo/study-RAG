"""语音识别服务：Whisper / SenseVoice 封装 + 离线演示转录兜底。

真实引擎通过懒加载引入；未安装推理栈时使用内置课堂剧本生成带毫秒时间戳的
模拟转录切片，保证 ingest 流水线端到端可演示。
"""
from __future__ import annotations

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
    """统一入口：优先本地 Whisper；失败或未安装时回落演示转录。"""

    def __init__(self) -> None:
        self._model = None

    def _load_whisper(self):  # pragma: no cover - 重型依赖懒加载
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel

            self._model = WhisperModel("small", compute_type="int8")
        except Exception:
            self._model = False
        return self._model

    async def transcribe(self, audio_bytes: bytes, filename: str = "") -> list[TranscriptSegment]:
        model = self._load_whisper()
        if model and audio_bytes:
            return await self._transcribe_real(audio_bytes)
        return self._transcribe_demo()

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
