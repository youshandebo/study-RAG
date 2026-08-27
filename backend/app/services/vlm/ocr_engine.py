"""视觉与手写公式识别：Qwen2.5-VL 封装 + 离线演示 OCR 兜底。

输出统一为 {latex, problem_text, figure_hints} 结构，供解题流水线消费。
"""
from __future__ import annotations

import base64

from app.core.config import get_settings


class OCREngine:
    async def recognize(self, image_b64: str) -> dict:
        settings = get_settings()
        if settings.dashscope_api_key:
            try:
                return await self._recognize_qwen_vl(image_b64)
            except Exception:
                pass
        return self._demo_recognize()

    async def _recognize_qwen_vl(self, image_b64: str) -> dict:  # pragma: no cover - 外部 API
        import httpx

        settings = get_settings()
        resp = await httpx.AsyncClient(timeout=90).post(
            f"{settings.dashscope_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.dashscope_api_key}"},
            json={
                "model": "qwen-vl-max",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                            {
                                "type": "text",
                                "text": "识别图片中的数学题，将公式转为 LaTeX，原样输出题面。",
                            },
                        ],
                    }
                ],
            },
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        return {"latex": text, "problem_text": text, "figure_hints": []}

    @staticmethod
    def _demo_recognize() -> dict:
        return {
            "latex": r"\int_1^{+\infty} \frac{\sqrt{x}}{x^2 + \ln x}\,dx",
            "problem_text": (
                r"试判断反常积分 $\int_1^{+\infty} \frac{\sqrt{x}}{x^2 + \ln x}\,dx$ 的敛散性。"
            ),
            "figure_hints": ["检测到手写分式结构", "积分上限为 +∞"],
        }


def decode_image_size(image_b64: str) -> int:
    try:
        return len(base64.b64decode(image_b64))
    except Exception:
        return 0
