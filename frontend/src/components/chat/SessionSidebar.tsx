'use client';

/** 会话隔离列表：新建 / 切换 / 重命名 / 删除（Cherry Studio 式严格 sessionId 隔离） */
import { useState } from 'react';
import { FileText, Plus } from 'lucide-react';
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
    <aside className="flex h-full w-64 shrink-0 flex-col border-r border-rule bg-[#101013] text-paper">
      {/* 品牌区 */}
      <div className="border-b border-white/10 px-5 py-5">
        <div className="flex items-center gap-2.5">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg border border-white/10 bg-white/[0.06]" aria-hidden>
            <FileText size={16} strokeWidth={1.5} className="text-zinc-300" />
          </span>
          <div>
            <div className="text-[14.5px] font-semibold leading-tight">课堂原法助教</div>
            <div className="mt-0.5 text-[10px] tracking-[0.18em] text-paper/40">UNIFIED OMNI-CHAT</div>
          </div>
        </div>
      </div>

      {/* 新建会话 */}
      <button
        onClick={() => createSession()}
        className="mx-4 mt-4 flex items-center justify-center gap-1.5 rounded-lg border border-dashed border-white/15 bg-white/[0.03] px-3 py-2.5 text-sm text-paper/85 transition hover:border-white/30 hover:bg-white/[0.06]"
      >
        <Plus size={14} strokeWidth={1.5} /> 新建对话
      </button>

      {/* 会话列表 */}
      <nav className="mt-3 flex-1 space-y-1 overflow-y-auto px-3 pb-4" aria-label="会话列表">
        {sessions.map((s) => {
          const active = s.id === activeSessionId;
          return (
            <div
              key={s.id}
              className={`group relative flex cursor-pointer items-center gap-2 rounded-lg px-3 py-2.5 text-sm transition ${
                active
                  ? 'bg-white/[0.08] text-paper'
                  : 'text-paper/60 hover:bg-white/[0.04] hover:text-paper'
              }`}
              onClick={() => editingId !== s.id && switchSession(s.id)}
            >
              {active && <span className="absolute left-0 top-2 bottom-2 w-[2px] rounded-full bg-blue-500" />}
              <FileText
                size={14}
                strokeWidth={1.5}
                aria-hidden
                className={`shrink-0 ${active ? 'text-zinc-300' : 'text-zinc-500'}`}
              />
              {editingId === s.id ? (
                <input
                  autoFocus
                  value={draftTitle}
                  onChange={(e) => setDraftTitle(e.target.value)}
                  onBlur={() => commitRename(s.id)}
                  onKeyDown={(e) => e.key === 'Enter' && commitRename(s.id)}
                  className="w-full rounded border border-white/20 bg-transparent px-1.5 py-0.5 text-sm outline-none"
                />
              ) : (
                <span className="flex-1 truncate" onDoubleClick={() => { setEditingId(s.id); setDraftTitle(s.title); }}>
                  {s.title}
                </span>
              )}
              <button
                aria-label={`删除会话 ${s.title}`}
                className="hidden shrink-0 rounded px-1 text-paper/40 transition hover:text-red-400 group-hover:block"
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

      <div className="border-t border-white/10 px-5 py-3 text-[11px] leading-relaxed text-paper/35">
        各会话知识库挂载与上下文严格隔离
      </div>
    </aside>
  );
}
