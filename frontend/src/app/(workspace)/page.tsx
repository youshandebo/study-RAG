'use client';

/** 统一主工作台：左会话栏 + 中全能对话画布 + 右证据抽屉 */
import { useEffect, useRef, useState } from 'react';
import SessionSidebar from '@/components/chat/SessionSidebar';
import UnifiedMessageList from '@/components/chat/UnifiedMessageList';
import OmniChatInput from '@/components/chat/OmniChatInput';
import EvidenceDrawer from '@/components/drawer/EvidenceDrawer';
import { useSessionStore } from '@/stores/useSessionStore';
import { useEvidenceStore } from '@/stores/useEvidenceStore';
import { hydrateMessages } from '@/db';
import { API_BASE } from '@/lib/api';

export default function WorkspacePage() {
  const { sessions, activeSessionId, init, appendMessage, messagesBySession } = useSessionStore();
  const openDrawer = useEvidenceStore((s) => s.openDrawer);
  const drawerOpen = useEvidenceStore((s) => s.open);
  const [backendOk, setBackendOk] = useState<boolean | null>(null);
  const hydratedFor = useRef<Set<string>>(new Set());

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

  // 首个会话自动生成演示欢迎卡片（仅一次）
  const welcomed = useRef(false);
  useEffect(() => {
    if (welcomed.current || !activeSessionId || sessions.length === 0) return;
    welcomed.current = true;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSessionId, sessions.length]);

  return (
    <div className="flex h-screen overflow-hidden">
      <SessionSidebar />

      <main className="flex min-w-0 flex-1 flex-col">
        {/* 顶部工具栏 */}
        <header className="flex items-center justify-between border-b border-rule bg-paper/80 px-6 py-3 backdrop-blur">
          <div className="min-w-0">
            <h1 className="font-display truncate text-[15px] font-bold text-ink">
              {sessions.find((s) => s.id === activeSessionId)?.title ?? '统一工作台'}
            </h1>
            <p className="text-[11.5px] text-ink-faint">
              拍照解题 · 老师原法 RAG · 音画溯源 · 苏格拉底伴学 · 多模型比对
            </p>
          </div>
          <div className="flex items-center gap-3">
            <span
              className={`rounded-full px-2.5 py-1 text-[11.5px] ${
                backendOk === null
                  ? 'bg-paper-deep text-ink-faint'
                  : backendOk
                    ? 'bg-chalk-soft text-chalk'
                    : 'bg-cinnabar-soft text-cinnabar'
              }`}
              title={backendOk ? '后端已连接（无 API Key 时自动运行内置演示引擎）' : '后端未连接：npm run dev 之外请先启动 uvicorn'}
            >
              {backendOk === null ? '检查后端…' : backendOk ? '● 助教引擎在线' : '● 后端未连接'}
            </span>
            <button
              onClick={() => openDrawer()}
              className="rounded-md border border-rule bg-[#fdfaf2] px-3 py-1.5 text-[12.5px] text-ink-soft transition hover:border-chalk hover:text-chalk"
            >
              {drawerOpen ? '隐藏抽屉' : '协作抽屉'} ⟩
            </button>
          </div>
        </header>

        {/* 消息流 + 输入 */}
        <UnifiedMessageList />
        <OmniChatInput />
      </main>

      <EvidenceDrawer />
    </div>
  );
}
