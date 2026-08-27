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
    <aside className="relative flex h-full w-64 shrink-0 flex-col bg-board text-paper">
      {/* 黑板粉笔纹理 */}
      <div
        className="pointer-events-none absolute inset-0 opacity-[0.07]"
        style={{
          backgroundImage:
            'radial-gradient(rgba(255,255,255,0.7) 0.5px, transparent 0.5px), radial-gradient(rgba(255,255,255,0.5) 0.5px, transparent 0.5px)',
          backgroundSize: '26px 26px, 44px 44px',
          backgroundPosition: '0 0, 13px 21px',
        }}
      />

      {/* 品牌区 */}
      <div className="relative border-b border-white/10 px-5 py-5">
        <div className="flex items-center gap-2.5">
          <span className="flex h-9 w-9 items-center justify-center rounded-xl border border-white/15 bg-white/10 text-lg" aria-hidden>
            🎓
          </span>
          <div>
            <div className="font-display text-[16px] font-bold leading-tight tracking-wide">课堂原法助教</div>
            <div className="mt-0.5 text-[10px] tracking-[0.18em] text-paper/45">UNIFIED OMNI-CHAT</div>
          </div>
        </div>
      </div>

      {/* 新建会话 */}
      <button
        onClick={() => createSession()}
        className="relative mx-4 mt-4 flex items-center justify-center gap-1.5 rounded-lg border border-dashed border-paper/30 bg-white/[0.04] px-3 py-2.5 text-sm text-paper/90 transition hover:border-[#7fd6c2]/60 hover:bg-[#7fd6c2]/10 hover:text-[#a8e3d3]"
      >
        <span className="text-base leading-none" aria-hidden>＋</span> 新建对话
      </button>

      {/* 会话列表 */}
      <nav className="relative mt-3 flex-1 space-y-1 overflow-y-auto px-3 pb-4" aria-label="会话列表">
        {sessions.map((s) => {
          const active = s.id === activeSessionId;
          return (
            <div
              key={s.id}
              className={`group relative flex cursor-pointer items-center gap-2 rounded-lg px-3 py-2.5 text-sm transition ${
                active
                  ? 'bg-white/[0.12] text-paper shadow-[inset_0_0_0_1px_rgba(255,255,255,0.08)]'
                  : 'text-paper/65 hover:bg-white/[0.06] hover:text-paper'
              }`}
              onClick={() => editingId !== s.id && switchSession(s.id)}
            >
              {active && <span className="absolute left-0 top-2 bottom-2 w-[3px] rounded-full bg-gradient-to-b from-[#e8c9a0] to-[#7fd6c2]" />}
              <span className={`shrink-0 text-[13px] ${active ? 'opacity-90' : 'opacity-40'}`} aria-hidden>
                {active ? '📖' : '📄'}
              </span>
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
                className="hidden shrink-0 rounded px-1 text-paper/40 transition hover:text-[#e89a8a] group-hover:block"
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

      <div className="relative border-t border-white/10 px-5 py-3 text-[11px] leading-relaxed text-paper/40">
        各会话知识库挂载与上下文严格隔离
      </div>
    </aside>
  );
}
