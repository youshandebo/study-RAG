"""前端流式渲染契约守卫（结构性断言，红灯先行）。

为什么用 python 断言前端源码
----------------------------
本项目前端没有单测运行器（tsc 只查类型、next build 只查 ESLint），而这条
契约**坏掉时两者都不会报错**：在 `onMeta` 里把占位消息的 id 换成服务端
`message_id` 是完全合法的 TS，后果却是 `appendDelta`（按 id 精确匹配）再也
找不到目标消息——整条流式链路静默落空：打字机效果消失、多模型分屏轨道全
空白、`[N]` 证据角标点不开。由于最终卡片仍会一次性补上完整内容，这个断链
在 UI 上表现为"好像只是没有打字机效果"，极易被当成小瑕疵放过。

契约（流中途 id 恒定）
----------------------
1. 占位消息在流式期间必须**一直沿用 pendingId**，改名只允许发生在最终卡片
   到达时（由 `onCard` 用 `card.id` 落定，`patchMessage` 的 legacyId 顺带
   清掉 IndexedDB 里的占位旧行）；
2. 快捷操作（ActionBar）必须同样接上 delta 与 card，否则点了没反应。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# tests/ -> backend/ -> 仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
OMNI = FRONTEND_SRC / "components" / "chat" / "OmniChatInput.tsx"
ACTION_BAR = FRONTEND_SRC / "components" / "chat" / "ActionBar.tsx"


def _read(path: Path) -> str:
    assert path.is_file(), f"前端源码缺失，守卫无法判定：{path}"
    return path.read_text(encoding="utf-8")


class TestStreamIdCannotChangeMidFlight:
    """流式期间占位消息 id 必须恒定：改名会让 delta/证据/分屏轨道全部落空。"""

    def test_no_rename_in_meta(self) -> None:
        """onMeta 里把 id 换成服务端 message_id 是这条断链的经典复发点。"""
        src = _read(OMNI)
        # `m.id === pendingId ? { ...m, id: meta.messageId } : m` 之类的改名写法
        renamed = re.search(r"id:\s*meta\.messageId", src)
        assert renamed is None, (
            "onMeta 不得把占位消息改名成服务端 message_id：后续 onDelta / "
            "onEvidence / onTrackDelta 一律按 pendingId 写，appendDelta 只按 id "
            "精确匹配，改名会让整条流式链路静默落空（打字机效果消失、多模型"
            "轨道全空白、[N] 角标点不开）")

    def test_delta_targets_pending_id(self) -> None:
        src = _read(OMNI)
        assert re.search(r"appendDelta\(\s*activeSessionId,\s*pendingId", src), (
            "流式文本必须写入 pendingId 对应的占位消息")

    def test_card_lands_on_server_id_with_legacy(self) -> None:
        """最终卡片以 card.id 落定，并传 pendingId 作 legacyId 清理占位旧行。"""
        src = _read(OMNI)
        assert re.search(
            r"patchMessage\(\s*\n?\s*activeSessionId,\s*\n?\s*card\.id,", src
        ), "最终卡片必须以服务端 card.id 落定（唯一合法的改名时机）"
        # 同一次调用里 pendingId 作为第四个实参（legacyId）出现
        block = src[src.index("onCard: (card)"): src.index("onDone:")]
        assert "pendingId," in block, (
            "onCard 必须把 pendingId 作为 legacyId 传给 patchMessage，"
            "否则 IndexedDB 里的占位旧行不会被清理、刷新后空占位复活")


class TestActionBarConsumesResponse:
    """快捷操作不能只发请求不收回答（原实现漏了 onDelta/onCard）。"""

    def test_action_bar_wires_delta_and_card(self) -> None:
        src = _read(ACTION_BAR)
        assert "onDelta" in src, "快捷操作必须接收流式文本，否则点了永远等不到回复"
        assert "onCard" in src, "快捷操作必须接收最终卡片"

    def test_action_bar_registers_abort(self) -> None:
        src = _read(ACTION_BAR)
        assert "registerAbort(" in src and "controller.signal" in src, (
            "快捷操作必须注册 abort 控制器：否则无法停止生成、"
            "切会话时旧请求也中断不掉")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
