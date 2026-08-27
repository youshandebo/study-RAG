"""视觉与手写公式识别：OpenAI 兼容视觉模型封装（管理员面板可配置端点/模型）+ 离线演示 OCR 兜底。

输出统一为 {latex, problem_text, figure_hints} 结构，供解题流水线消费。
"""
from __future__ import annotations

import base64

from app.core import runtime_config


class OCREngine:
    async def recognize(self, image_b64: str) -> dict:
        cfg = runtime_config.effective("vlm")
        if cfg["api_key"] and cfg["base_url"] and cfg["model"]:
            try:
                return await self._recognize_vision(image_b64, cfg)
            except Exception:
                pass
        return self._demo_recognize()

    PROMPT = "识别图片中的数学题，将公式转为 LaTeX，原样输出题面。"

    async def _recognize_vision(self, image_b64: str, cfg: dict) -> dict:  # pragma: no cover - 外部 API
        """通吃 Qwen-VL / GPT-4o / GLM-4V 等 OpenAI 兼容多模态端点；Anthropic 协议单独分支。"""
        import httpx

        content = [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
            {"type": "text", "text": self.PROMPT},
        ]
        if cfg.get("provider") == "anthropic":
            resp = await httpx.AsyncClient(timeout=120).post(
                f"{cfg['base_url'].rstrip('/')}/v1/messages",
                headers={"x-api-key": cfg["api_key"], "anthropic-version": "2023-06-01"},
                json={
                    "model": cfg["model"],
                    "max_tokens": 2048,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": image_b64,
                                    },
                                },
                                {"type": "text", "text": self.PROMPT},
                            ],
                        }
                    ],
                },
            )
        else:
            resp = await httpx.AsyncClient(timeout=120).post(
                f"{cfg['base_url'].rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                json={
                    "model": cfg["model"],
                    "messages": [{"role": "user", "content": content}],
                },
            )
        resp.raise_for_status()
        data = resp.json()
        if "choices" in data:
            text = data["choices"][0]["message"]["content"]
        else:
            blocks = data.get("content") or []
            text = "".join(b.get("text", "") for b in blocks)
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
