'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com

/** 会话隔离列表：新建 / 切换 / 重命名 / 删除 / 搜索（严格 sessionId 隔离） */
import { useMemo, useState } from 'react';
import { FileText, Plus, Search, X } from 'lucide-react';
import { useSessionStore } from '@/stores/useSessionStore';
import AboutDialog from './AboutDialog';

export default function SessionSidebar() {
  const { sessions, activeSessionId, createSession, switchSession, removeSession, renameSession } =
    useSessionStore();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState('');
  const [query, setQuery] = useState('');

  // 标题/学科/考点关键词模糊过滤：空格分词，词间 AND
  const filtered = useMemo(() => {
    const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    if (tokens.length === 0) return sessions;
    return sessions.filter((s) => {
      const hay = s.title.toLowerCase();
      return tokens.every((tk) => hay.includes(tk));
    });
  }, [sessions, query]);

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

      {/* 搜索框：按标题/学科/考点关键词即时过滤 */}
      <div className="relative mx-4 mt-3">
        <Search size={13} strokeWidth={1.5} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-paper/35" aria-hidden />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索会话 / 学科 / 考点…"
          className="w-full rounded-lg border border-white/10 bg-white/[0.04] py-1.5 pl-8 pr-7 text-[12.5px] text-paper/90 placeholder-white/30 outline-none transition focus:border-blue-500/60"
        />
        {query && (
          <button
            onClick={() => setQuery('')}
            aria-label="清空搜索"
            className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-paper/40 hover:text-paper/80"
          >
            <X size={12} strokeWidth={1.5} />
          </button>
        )}
      </div>

      {/* 会话列表 */}
      <nav className="mt-3 flex-1 space-y-1 overflow-y-auto px-3 pb-4" aria-label="会话列表">
        {filtered.map((s) => {
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
        {filtered.length === 0 && (
          <div className="px-3 py-6 text-center text-[11.5px] text-paper/35">
            {sessions.length === 0 ? '暂无会话' : `没有匹配「${query}」的会话`}
          </div>
        )}
      </nav>

      {/* 底部署名徽标（AGPL 部署要求保留；商业授权可移除） */}
      <div className="border-t border-white/10 px-4 py-2.5">
        <AboutDialog />
        <div className="mt-1 text-center text-[10px] leading-relaxed text-paper/30">
          各会话知识库与上下文严格隔离
        </div>
      </div>
    </aside>
  );
}
