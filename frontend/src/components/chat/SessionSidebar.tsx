'use client';

/** 会话隔离列表：新建 / 切换 / 重命名 / 删除（Cherry Studio 式严格 sessionId 隔离） */
import { useState } from 'react';
import { useSessionStore } from '@/stores/useSessionStore';

export default function SessionSidebar() {
  const { sessions, activeSessionId, createSession, switchSession, removeSession, renameSession } =
    useSessionStore();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState('');

  const commitRename = async (id: string) => {
    if (draftTitle.trim()) await renameSession(id, draftTitle.trim());
    setEditingId(null);
  };

  return (
    <aside className="flex h-full w-64 shrink-0 flex-col bg-board text-paper">
      {/* 品牌区 */}
      <div className="border-b border-white/10 px-5 py-5">
        <div className="font-display text-lg font-bold tracking-wide">课堂原法助教</div>
        <div className="mt-1 text-[11px] tracking-widest text-paper/50">UNIFIED OMNI-CHAT</div>
      </div>

      {/* 新建会话 */}
      <button
        onClick={() => createSession()}
        className="mx-4 mt-4 rounded-lg border border-dashed border-paper/30 px-3 py-2.5 text-sm text-paper/85 transition hover:border-paper/60 hover:bg-white/5"
      >
        ＋ 新建对话
      </button>

      {/* 会话列表 */}
      <nav className="mt-3 flex-1 space-y-1 overflow-y-auto px-3 pb-4" aria-label="会话列表">
        {sessions.map((s) => {
          const active = s.id === activeSessionId;
          return (
            <div
              key={s.id}
              className={`group relative flex cursor-pointer items-center gap-2 rounded-lg px-3 py-2.5 text-sm transition ${
                active ? 'bg-white/12 text-paper' : 'text-paper/70 hover:bg-white/6 hover:text-paper'
              }`}
              onClick={() => editingId !== s.id && switchSession(s.id)}
            >
              {active && <span className="absolute left-0 top-2 bottom-2 w-[3px] rounded-full bg-[#e8c9a0]" />}
              {editingId === s.id ? (
                <input
                  autoFocus
                  value={draftTitle}
                  onChange={(e) => setDraftTitle(e.target.value)}
                  onBlur={() => commitRename(s.id)}
                  onKeyDown={(e) => e.key === 'Enter' && commitRename(s.id)}
                  className="w-full rounded border border-paper/30 bg-transparent px-1.5 py-0.5 text-sm outline-none"
                />
              ) : (
                <span className="flex-1 truncate" onDoubleClick={() => { setEditingId(s.id); setDraftTitle(s.title); }}>
                  {s.title}
                </span>
              )}
              <button
                aria-label={`删除会话 ${s.title}`}
                className="hidden shrink-0 rounded px-1 text-paper/40 hover:text-[#e89a8a] group-hover:block"
                onClick={(e) => {
                  e.stopPropagation();
                  void removeSession(s.id);
                }}
              >
                ✕
              </button>
            </div>
          );
        })}
      </nav>

      <div className="border-t border-white/10 px-5 py-3 text-[11px] leading-relaxed text-paper/40">
        各会话知识库挂载与上下文严格隔离
      </div>
    </aside>
  );
}
