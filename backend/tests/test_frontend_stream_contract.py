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
SIDEBAR = FRONTEND_SRC / "components" / "chat" / "SessionSidebar.tsx"
MSG_LIST = FRONTEND_SRC / "components" / "chat" / "UnifiedMessageList.tsx"
INGEST_PANEL = FRONTEND_SRC / "components" / "drawer" / "QuickIngestPanel.tsx"
API_TS = FRONTEND_SRC / "lib" / "api.ts"
SESSION_STORE = FRONTEND_SRC / "stores" / "useSessionStore.ts"
DB_TS = FRONTEND_SRC / "db" / "index.ts"
PAGE_TSX = FRONTEND_SRC / "app" / "(workspace)" / "page.tsx"


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


class TestStopMustAbortViaStore:
    """「停止」必须走 store 的统一中断入口（P1：ActionBar 流停不掉）。

    ActionBar 发起的流只把控制器注册在 useSessionStore（activeAbort），
    发起组件 OmniChatInput 的本地 ref 拿不到它——旧实现 stop() 先 abort
    本地空引用（空操作）、再把 store 里的活控制器置 null（丢弃而非中断）、
    清掉 busy：回答继续打字、输入框却被解锁到可以并发第二条流。
    """

    def test_stop_uses_store_abort(self) -> None:
        src = _read(OMNI)
        assert "abortRef" not in src, (
            "stop() 不得依赖组件本地 abortRef：跨组件发起的流（ActionBar）"
            "只把控制器注册在 store，本地 abort 是空操作——busy 被清、回答"
            "却继续打字。统一中断入口是 useSessionStore.abortActive()")
        assert re.search(r"abortActive\(\)", src), (
            "stop() 必须调用 store 的 abortActive()（abort + 清引用 + 清 busy 一次完成）")


class TestSceneModesSingleSource:
    """检索模式必须单一值集（P2-1：头部与 Chip 双入口两套值集）。

    头部 cycleMode 轮换 lecture/review_narrow/review_broad，而输入框
    SCENE_PRESETS 用的是裸 'review'——同一个概念两套值，Chip 的推导
    `startsWith('review')` 让 narrow/broad 都显示「期末复习」，用户从
    头部切档后 Chip 纹丝不动，无法得知当前实际档位。
    """

    def test_scene_presets_use_canonical_modes(self) -> None:
        src = _read(OMNI)
        assert "review_narrow" in src and "review_broad" in src, (
            "SCENE_PRESETS 必须使用后端 canonical 值集（review_narrow / "
            "review_broad），与头部 cycleMode 共用同一套值")
        bare = re.search(r"key:\s*'review'\s*as const", src)
        assert bare is None, (
            "SCENE_PRESETS 不得保留裸 'review' 预设——与 narrow/broad 并存"
            "会让双入口各自为政，Chip 无法区分周测/期末")

    def test_legacy_review_mode_normalized(self) -> None:
        src = _read(OMNI)
        assert re.search(r"'review'\s*\?\s*'review_broad'", src), (
            "历史会话可能存着裸 'review'：渲染前必须归一到 review_broad，"
            "否则该档位匹配不到任何预设，Chip 回退显示随堂模式造成"
            "「切了但没反应」的错觉")


class TestActionBarQuotaNoticeConsistency:
    """429/402 反馈必须同路径（P2-2：ActionBar 与输入框不一致）。

    输入框路径：移除占位气泡 + 输入框下方柔性提示（含 Retry-After 倒计时）。
    ActionBar 旧路径：一律写 ⚠️ 进气泡——限流时用户看不到何时能再问。
    前提是 softNotice 提升到 store，ActionBar 写、输入框渲染。
    """

    def test_action_bar_shares_soft_notice_path(self) -> None:
        src = _read(ACTION_BAR)
        assert "isRateLimit" in src and "isQuotaExceeded" in src, (
            "快捷操作的 429/402 必须识别并走统一反馈：请求未开始时占位"
            "气泡要移除，不得把限流错误写进气泡")
        assert "setSoftNotice" in src and "removeMessage" in src, (
            "快捷操作必须通过 store 的 setSoftNotice + removeMessage 走与"
            "输入框完全相同的反馈路径")

    def test_soft_notice_lives_in_store(self) -> None:
        store_src = _read(SESSION_STORE)
        assert "softNotice" in store_src, (
            "softNotice 必须提升到 useSessionStore：ActionBar 与输入框共用"
            "同一条反馈通道，ActionBar 写入的提示才能出现在输入框下方")
        omni_src = _read(OMNI)
        assert re.search(r"useSessionStore\(\(s\) => s\.softNotice\)", omni_src), (
            "输入框必须从 store 渲染 softNotice（组件局部 state 收不到"
            "ActionBar 写入的限流/额度提示）")


class TestSidebarRenameAffordance:
    """重命名必须有显式按钮（P2-3：触屏无入口、桌面无发现性）。

    旧实现仅 onDoubleClick——移动端双击不可靠，桌面端用户无从知道
    双击可改名。按钮须常驻（非 hover 依赖），与删除按钮同一触控标准。
    """

    def test_rename_has_visible_button(self) -> None:
        src = _read(SIDEBAR)
        assert "重命名会话" in src, (
            "重命名必须有带 aria-label 的显式按钮，不得只靠 onDoubleClick"
            "（触屏双击不可靠、桌面无发现性）")
        assert "Pencil" in src, "重命名按钮用铅笔图标（lucide-react Pencil）"
        assert "onDoubleClick" in src, "保留双击路径作为桌面端快捷方式"


class TestIngestProgressIsReal:
    """入库进度必须反映真实任务状态（P2-4：假进度条）。

    旧实现用 setInterval 每 320ms 推一档直到 95%——与后端流水线完全
    解耦：长 ASR 卡在 95% 干等，快任务演完五档才结束。后端已有完整
    任务化接口（POST /ingest/tasks 立即返回句柄 + GET 状态轮询/SSE，
    `_STAGE_HINTS` 就是给前端的阶段文案），前端必须接上。
    """

    def test_panel_uses_task_api(self) -> None:
        src = _read(INGEST_PANEL)
        assert "submitIngestTask" in src, (
            "上传必须走任务化入库接口：提交立即返回句柄，再按真实状态"
            "刷新进度，不得再演出假进度")
        assert re.search(r"window\.setInterval", src) is None, (
            "入库进度不得用 setInterval 按时间轴模拟——那与真实流水线"
            "完全解耦（长任务卡 95%，快任务演完五档）")

    def test_api_provides_task_client(self) -> None:
        src = _read(API_TS)
        assert "submitIngestTask" in src and "pollIngestTask" in src, (
            "api.ts 必须提供任务化入库客户端（POST /ingest/tasks 提交 + "
            "GET /ingest/tasks/{id} 轮询到终态）")


class TestExpandedRenderCap:
    """「展开更早消息」必须递进封顶（P3：expanded 全量渲染无上限）。

    旧实现 expanded ? messages : slice(-60)：一次点开千条会话全量进
    DOM。改为 renderCount 窗口递进（每次 +300），展开入口仍在、
    但任何时刻的渲染量都有上界。
    """

    def test_expand_caps_dom(self) -> None:
        src = _read(MSG_LIST)
        assert re.search(r"slice\(-renderCount\)", src), (
            "可见消息必须按 renderCount 窗口截取（展开递进），"
            "保证任何时刻 DOM 渲染量有上界")
        assert re.search(r"expanded \? messages", src) is None, (
            "不得保留「expanded 即全量渲染」的旧模式")


class TestNoDemoResidue:
    """演示期残留清理（P3）：死分支兜底与预置文案不得留在生产 UI。"""

    def test_no_demo_fallback_in_action_bar(self) -> None:
        src = _read(ACTION_BAR)
        assert "反常积分敛散性" not in src, (
            "ActionBar 不得保留演示期兜底文案——content.slice 永不为 null，"
            "该分支是死代码，且把演示内容混进生产路径")

    def test_no_preset_demo_copy(self) -> None:
        src = _read(INGEST_PANEL)
        assert "已预置" not in src, (
            "生产 UI 不得保留「已预置 N 条切片」演示文案——"
            "真实部署没有预置数据，文案会误导用户以为素材已入库")


class TestLocalDataOwnerScoping:
    """本地 IndexedDB 必须按账号分区，登出必须重置本地视图（P0 隐私泄漏）。

    服务端 TenantScope/ScopedQdrantClient 那层多租户隔离做得再扎实，这个
    漏洞也完全绕开它——不需要碰后端，物理上坐到别人用过的电脑前就够：
    sessions 表无 owner 字段、init() 用 toArray() 全量加载、登出只清
    token——A 退出登录后 B 在同一浏览器登录，侧边栏照样列出 A 的全部
    会话，点进去聊天记录、拍的题目照片全可见。目标场景（学校机房共享
    设备）恰好是这个泄漏最容易被撞见的场景。
    """

    def test_sessions_table_has_owner_index(self) -> None:
        src = _read(DB_TS)
        assert re.search(r"sessions:\s*'id,\s*createdAt,\s*owner'", src), (
            "Dexie sessions 表必须有 owner 索引——这是按账号分区查询的"
            "物理前提；没有它 init() 只能全量加载，分区无从谈起")

    def test_legacy_rows_backfilled_on_upgrade(self) -> None:
        src = _read(DB_TS)
        assert "upgrade" in src, (
            "schema 升级必须带 upgrade 钩子处理存量行——预修复的无 owner "
            "数据需要一次性回填，否则分区过滤后老用户本地会话凭空消失")
        # 回填写法不限形态（对象字面量或赋值式），判定语义：无主行归 anonymous 桶
        assert re.search(r"owner\s*(?::|=)\s*'anonymous'", src), (
            "存量无 owner 行必须回填到 'anonymous' 桶（非破坏性迁移）："
            "无法归因的数据宁可归匿名桶，也不得默认归到登录用户名下")

    def test_init_filters_by_owner(self) -> None:
        src = _read(SESSION_STORE)
        assert re.search(r"where\(['\"]owner['\"]\)", src), (
            "init() 必须按当前 owner 过滤加载会话，不得把本地库整表读进侧栏")
        assert re.search(r"db\.sessions\.toArray\(\)", src) is None, (
            "init() 不得 db.sessions.toArray() 全量加载——这正是泄漏点："
            "本地库里所有账号的会话不加过滤地进 UI 状态")

    def test_create_session_stamps_owner(self) -> None:
        src = _read(SESSION_STORE)
        assert re.search(r"owner:\s*ownerKey\(\)", src), (
            "新建会话必须盖当前 owner 章——漏盖的行永远进不了任何账号的"
            "分区，用户自己刷新后也会丢会话")

    def test_logout_resets_local_view(self) -> None:
        src = _read(PAGE_TSX)
        assert "resetLocalView" in src, (
            "登出（含 token 失效分支）必须调用 store 的 resetLocalView 重置"
            "本地视图——只清 token 不清内存/本地视图，A 的会话在登出后的"
            "屏幕上原样留着，以匿名身份继续可读")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
