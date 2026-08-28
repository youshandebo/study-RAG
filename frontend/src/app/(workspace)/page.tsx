'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com


/** 统一主工作台：左会话栏 + 中全能对话画布 + 右证据抽屉 */
import { useEffect, useRef, useState } from 'react';
import { BookOpen, ChevronRight, Settings } from 'lucide-react';
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

  return (
    <div className="flex h-screen overflow-hidden">
      <SessionSidebar />

      <main className="flex min-w-0 flex-1 flex-col">
        {/* 顶部工具栏 */}
        <header className="flex items-center justify-between border-b border-rule bg-white/85 px-6 py-3 backdrop-blur-sm">
          <div className="min-w-0">
            <h1 className="flex items-center gap-2 truncate text-[15px] font-semibold text-ink">
              <BookOpen size={16} strokeWidth={1.5} className="shrink-0 text-chalk" aria-hidden />
              {sessions.find((s) => s.id === activeSessionId)?.title ?? '统一工作台'}
            </h1>
            <p className="mt-0.5 text-[11.5px] text-ink-faint">
              拍照解题 · 老师原法 RAG · 音画溯源 · 苏格拉底伴学 · 多模型比对
            </p>
          </div>
          <div className="flex items-center gap-2.5">
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
            <a
              href="/admin"
              className="flex items-center gap-1.5 rounded-lg border border-rule/60 bg-white/70 px-3 py-1.5 text-[12.5px] text-ink-soft transition hover:border-rule hover:text-ink"
              title="模型配置 · 知识库统计 · 管理员密码"
            >
              <Settings size={14} strokeWidth={1.5} />
              管理后台
            </a>
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
    </div>
  );
}
