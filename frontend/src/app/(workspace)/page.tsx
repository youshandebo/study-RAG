'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com


/** 统一主工作台：左会话栏 + 中全能对话画布 + 右证据抽屉 */
import { useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { BookMarked, BookOpen, ChevronRight, LogIn, LogOut, Menu, NotebookPen, Settings, User } from 'lucide-react';
import { fetchMe, getSassToken, setSassToken, setSassUser, type MeInfo } from '@/lib/api';
import SessionSidebar from '@/components/chat/SessionSidebar';
import UnifiedMessageList from '@/components/chat/UnifiedMessageList';
import OmniChatInput from '@/components/chat/OmniChatInput';
import EvidenceDrawer from '@/components/drawer/EvidenceDrawer';
import NotebookDrawer from '@/components/notebook/NotebookDrawer';
import MobileDrawer from '@/components/MobileDrawer';
import { useSessionStore } from '@/stores/useSessionStore';
import { useEvidenceStore } from '@/stores/useEvidenceStore';
import { useNotebookStore } from '@/stores/useNotebookStore';
import { hydrateMessages } from '@/db';
import { API_BASE } from '@/lib/api';

export default function WorkspacePage() {
  const router = useRouter();
  const { sessions, activeSessionId, updateSessionMeta, init, appendMessage, messagesBySession } = useSessionStore();
  const activeSession = sessions.find((s) => s.id === activeSessionId);
  const mode = activeSession?.retrievalMode ?? 'lecture';
  const MODE_UI: Record<string, { label: string; hint: string; cls: string }> = {
    lecture: { label: '⚡ 随堂模式', hint: '随堂做题：优先最近 1~2 周新授内容', cls: 'bg-amber-500/10 text-amber-600' },
    review_narrow: { label: '📖 周测复习', hint: '窄范围复习：近期内容仍强相关（α 默认 0.20）', cls: 'bg-sky-500/10 text-sky-600' },
    review_broad: { label: '📚 期末复习', hint: '大跨度复习：几乎时间平权，防漏早期重点（α 默认 0.05）', cls: 'bg-violet-500/10 text-violet-600' },
    explore: { label: '🧭 拓展解法', hint: '输入框下方切换的探索模式', cls: 'bg-violet-500/10 text-violet-600' },
  };
  const modeUi = MODE_UI[mode] ?? MODE_UI.lecture;
  const cycleMode = () => {
    if (!activeSession) return;
    const order: Array<string> = ['lecture', 'review_narrow', 'review_broad'];
    const next = order[(order.indexOf(mode === 'explore' ? 'lecture' : mode) + 1) % order.length];
    updateSessionMeta(activeSession.id, { retrievalMode: next as never });
  };
  const openDrawer = useEvidenceStore((s) => s.openDrawer);
  const drawerOpen = useEvidenceStore((s) => s.open);
  const [backendOk, setBackendOk] = useState<boolean | null>(null);
  const [me, setMe] = useState<MeInfo | null>(null);
  const hydratedFor = useRef<Set<string>>(new Set());
  const { openDrawer: openNotebook, dueCount, refreshDue } = useNotebookStore();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  // 当前会员身份：有 token 时拉 /auth/me（失效自动清本地态）
  useEffect(() => {
    if (!getSassToken()) {
      setMe(null);
      return;
    }
    void fetchMe()
      .then((m) => {
        if (m && !m.anonymous) {
          setMe(m);
          void refreshDue(); // 登录后拉一次待复习角标
        } else {
          // token 失效（服务端判定匿名）：与手动登出同路径处理——凭据清掉
          // 的瞬间必须重置本地视图，否则 A 的会话在掉线后的屏幕上原样留着。
          setSassToken(null);
          setSassUser(null);
          setMe(null);
          useSessionStore.getState().resetLocalView();
        }
      })
      .catch(() => {
        // 网络层失败（后端未启动 / 502 抖动）**不等于**登录态失效：绝不能
        // 顺手清掉 token，否则一次抖动就把已登录用户登出——把可观测性问题
        // 变成账号问题。原实现没有 catch：后端没起时这里是一个未捕获的
        // Promise rejection，会员区静默停在"登录/注册"，用户无从判断是
        // 自己没登录还是服务没起来。改为保留会员态、交给健康探测条提示。
        setBackendOk(false);
      });
  }, [refreshDue]);

  // 启动初始化：加载/创建会话
  useEffect(() => {
    void init();
  }, [init]);

  // 后端健康探测
  useEffect(() => {
    fetch(`${API_BASE}/health`)
      .then((r) => (r.ok ? setBackendOk(true) : setBackendOk(false)))
      .catch(() => setBackendOk(false));
  }, []);

  // 安装版首次部署检测：无用户且未设管理密码时，自动进入网页初始化向导。
  // 注意不能因本地残留 token 而跳过——502 时期浏览器可能存过早已失效的
  // token，而 needs_setup=true 意味着系统里根本没有用户，跳向导永远正确。
  useEffect(() => {
    fetch(`${API_BASE}/auth/setup/status`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d?.needs_setup) router.replace('/setup');
      })
      .catch(() => undefined); // 后端未起/探测失败：留在工作台，不打断
  }, [router]);

  // 会话切换时从 Dexie 恢复历史（严格按 sessionId 隔离）
  useEffect(() => {
    if (!activeSessionId || hydratedFor.current.has(activeSessionId)) return;
    hydratedFor.current.add(activeSessionId);
    if ((messagesBySession[activeSessionId] ?? []).length === 0) {
      void hydrateMessages(activeSessionId).then((rows) => {
        rows.forEach((r) => appendMessage(r));
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSessionId]);

  return (
    <div className="flex h-screen overflow-hidden">
      {/* 大屏常驻三列之一；xl 以下隐藏，改由头部菜单按钮唤出左侧抽屉 */}
      <div className="hidden h-full xl:block">
        <SessionSidebar />
      </div>

      <main className="flex min-w-0 flex-1 flex-col">
        {/* 顶部工具栏 */}
        <header className="flex flex-wrap items-center justify-between gap-2 border-b border-rule bg-white/85 px-4 py-3 backdrop-blur-sm sm:px-6">
          <button
            type="button"
            onClick={() => setMobileNavOpen(true)}
            aria-label="打开会话列表"
            className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-rule/60 bg-white/70 text-ink-soft transition hover:border-rule hover:text-ink xl:hidden"
          >
            <Menu size={18} strokeWidth={1.5} aria-hidden />
          </button>
          <div className="min-w-0 flex-1">
            <h1 className="flex items-center gap-2 truncate text-[15px] font-semibold text-ink">
              <BookOpen size={16} strokeWidth={1.5} className="shrink-0 text-chalk" aria-hidden />
              {activeSession?.title ?? '统一工作台'}
              {activeSession?.courseId && (
                <span
                  className="inline-flex shrink-0 items-center gap-1 rounded-full border border-chalk/30 bg-chalk-soft px-2 py-0.5 text-[10.5px] font-medium text-chalk"
                  title={`当前会话挂载课程：${activeSession.courseId}${activeSession.chapter ? ' · ' + activeSession.chapter : ''}（检索限定在该课程作用域内）`}
                >
                  <BookMarked size={11} strokeWidth={1.5} aria-hidden />
                  {activeSession.subject || '课程'} · {activeSession.chapter || activeSession.courseId}
                </span>
              )}
            </h1>
            <p className="mt-0.5 text-[11.5px] text-ink-faint">
              拍照解题 · 老师原法 RAG · 音画溯源 · 苏格拉底伴学 · 多模型比对
            </p>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2.5 xl:flex-nowrap">
            <span
              className={`flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11.5px] font-medium ${
                backendOk === null
                  ? 'bg-paper-deep text-ink-faint'
                  : backendOk
                    ? 'bg-chalk-soft text-chalk'
                    : 'bg-cinnabar-soft text-cinnabar'
              }`}
              title={backendOk ? '后端已连接（无 API Key 时自动运行内置演示引擎）' : '后端未连接：请先启动 uvicorn'}
            >
              {backendOk ? (
                <span className="relative flex h-1.5 w-1.5">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-chalk opacity-75" />
                  <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-chalk" />
                </span>
              ) : (
                <span className={`h-1.5 w-1.5 rounded-full ${backendOk === null ? 'bg-ink-faint' : 'bg-cinnabar'}`} />
              )}
              {backendOk === null ? '检查后端…' : backendOk ? '引擎在线' : '后端未连接'}
            </span>
            {/* 会员身份：匿名=登录入口；已登录=档位徽标+退出 */}
            {me && !me.anonymous ? (
              <span className="inline-flex items-center gap-1.5 rounded-full bg-chalk-soft px-2.5 py-1 text-[11.5px] text-chalk">
                <User size={11} strokeWidth={1.5} aria-hidden />
                <span className="max-w-[120px] truncate">{me.email}</span>
                <span className="rounded bg-chalk px-1.5 text-[10px] font-bold uppercase text-white">
                  {me.tier === 'max' ? 'MAX' : me.tier === 'pro' ? 'PRO' : 'FREE'}
                </span>
                <button
                  onClick={() => {
                    // 顺序有讲究：先清凭据（ownerKey 变 anonymous），再重置
                    // 本地视图并按匿名桶重新加载——只清 token 的话，A 的全部
                    // 会话/聊天记录/题目照片仍留在屏幕与本地库的当前视图里，
                    // 共享设备上下一个使用者直接可见（P0 隐私修复出口端）。
                    setSassToken(null);
                    setSassUser(null);
                    setMe(null);
                    useSessionStore.getState().resetLocalView();
                  }}
                  aria-label="退出登录"
                  title="退出登录"
                  className="rounded p-0.5 transition hover:bg-white/40"
                >
                  <LogOut size={11} strokeWidth={1.5} />
                </button>
              </span>
            ) : (
              <a
                href="/login"
                className="inline-flex items-center gap-1.5 rounded-full bg-blue-600 px-3 py-1 text-[11.5px] font-semibold text-white transition hover:bg-blue-500"
              >
                <LogIn size={11} strokeWidth={1.5} aria-hidden />
                登录 / 注册
              </a>
            )}
            {/* 检索场景三档轮换：α 默认值随场景走（0.30/0.20/0.05），均可用输入卡的时间偏好滑杆覆盖 */}
            <button
              onClick={cycleMode}
              disabled={!activeSession}
              title={modeUi.hint + '（点击切换）'}
              className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11.5px] font-medium transition ${modeUi.cls} disabled:opacity-40`}
            >
              {modeUi.label}
            </button>
            {me?.is_admin && (
              <a
                href="/admin"
                className="flex items-center gap-1.5 rounded-lg border border-rule/60 bg-white/70 px-3 py-1.5 text-[12.5px] text-ink-soft transition hover:border-rule hover:text-ink"
                title="模型配置 · 知识库统计 · 管理员密码"
              >
                <Settings size={14} strokeWidth={1.5} />
                管理后台
              </a>
            )}
            {/* 错题本入口：待复习数量红点角标（仅登录用户） */}
            {me && !me.anonymous && (
              <button
                onClick={() => openNotebook('review')}
                title="错题本 · Leitner 复习"
                className="relative flex items-center gap-1.5 rounded-lg border border-rule/60 bg-white/70 px-3 py-1.5 text-[12.5px] text-ink-soft transition hover:border-rule hover:text-ink"
              >
                <NotebookPen size={14} strokeWidth={1.5} />
                错题本
                {dueCount > 0 && (
                  <span className="absolute -right-1.5 -top-1.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-cinnabar px-1 text-[10px] font-bold text-white">
                    {dueCount > 99 ? '99+' : dueCount}
                  </span>
                )}
              </button>
            )}
            <button
              onClick={() => openDrawer()}
              className="flex items-center gap-1.5 rounded-lg border border-rule/60 bg-white/70 px-3 py-1.5 text-[12.5px] text-ink-soft transition hover:border-rule hover:text-ink"
            >
              {drawerOpen ? '隐藏抽屉' : '协作抽屉'}
              <ChevronRight size={14} strokeWidth={1.5} />
            </button>
          </div>
        </header>

        {/* 消息流 + 输入 */}
        <UnifiedMessageList />
        <OmniChatInput />
      </main>

      <EvidenceDrawer />
      {/* xl 以下：头部菜单唤出的左侧会话抽屉（选会话后关闭） */}
      <MobileDrawer open={mobileNavOpen} onClose={() => setMobileNavOpen(false)} ariaLabel="会话列表">
        <SessionSidebar onSelectSession={() => setMobileNavOpen(false)} />
      </MobileDrawer>
      {/* 错题本抽屉：租户管理员 / 平台超管额外可见"机构卡点"页签 */}
      <NotebookDrawer canManage={!!(me?.is_admin || me?.role === 'tenant_admin')} />
    </div>
  );
}
