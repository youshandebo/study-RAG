'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com

/** 会话隔离列表：新建（可绑定课程）/ 搜索 / 按学科分组 / 切换 / 重命名 / 删除 */
import { useMemo, useState } from 'react';
import { BookMarked, FileText, Pencil, Plus, Search, X } from 'lucide-react';
import { useSessionStore } from '@/stores/useSessionStore';
import AboutDialog from './AboutDialog';

// 可绑定课程目录：**部署期静态配置**（课程集不随会话变化，当前无后端目录
// 端点；若未来课程需要动态管理，再立项 /courses 端点并改为接口拉取+兜底）。
const COURSES = [
  { courseId: 'math-calculus-101', subject: '数学', name: '高等数学 · 反常积分专题' },
  { courseId: 'math-linear-102', subject: '数学', name: '线性代数 · 矩阵与行列式' },
  { courseId: 'physics-mech-201', subject: '物理', name: '大学物理 · 力学' },
  { courseId: '', subject: '通识', name: '不绑定课程（全库检索）' },
];

export default function SessionSidebar({ onSelectSession }: { onSelectSession?: () => void }) {
  const { sessions, activeSessionId, createSession, switchSession, removeSession, renameSession, updateSessionMeta } =
    useSessionStore();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState('');
  const [query, setQuery] = useState('');
  const [showCoursePicker, setShowCoursePicker] = useState(false);
  // 删除的二次确认：一次误点会清掉该会话的全部历史（本地 IndexedDB 记录），
  // 不可撤销——用两阶段点击代替浏览器 confirm，既不打断侧栏视觉节奏，
  // 也让触屏用户有明确的"再点一次才真删"的反馈。
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  // 新建时选中的课程
  const [pendingCourse, setPendingCourse] = useState(COURSES[0]);

  // 标题/学科/考点关键词模糊过滤（空格分词 AND）
  const filtered = useMemo(() => {
    const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    if (tokens.length === 0) return sessions;
    return sessions.filter((s) => {
      const hay = `${s.title} ${s.subject ?? ''} ${s.chapter ?? ''}`.toLowerCase();
      return tokens.every((tk) => hay.includes(tk));
    });
  }, [sessions, query]);

  // 按学科分组（保持时间倒序）
  const groups = useMemo(() => {
    const map = new Map<string, typeof filtered>();
    for (const s of filtered) {
      const key = s.subject?.trim() || '未分类';
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(s);
    }
    return Array.from(map.entries()).sort((a, b) => a[0].localeCompare(b[0], 'zh'));
  }, [filtered]);

  const createIn = async () => {
    setShowCoursePicker(false);
    const id = await createSession(`${pendingCourse.name.split('·')[0].trim()} · 新对话`);
    if (pendingCourse.courseId) {
      updateSessionMeta(id, {
        subject: pendingCourse.subject,
        courseId: pendingCourse.courseId,
        chapter: '',
      });
    }
    setPendingCourse(COURSES[0]);
  };

  const commitRename = async (id: string) => {
    if (draftTitle.trim()) await renameSession(id, draftTitle.trim());
    setEditingId(null);
  };

  return (
    <aside className="flex h-full w-full xl:w-64 shrink-0 flex-col border-r border-rule bg-[#101013] text-paper">
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

      {/* 新建对话：点展开课程选择 */}
      <div className="mx-4 mt-4">
        <button
          onClick={() => setShowCoursePicker((v) => !v)}
          className="flex w-full items-center justify-center gap-1.5 rounded-lg border border-dashed border-white/15 bg-white/[0.03] px-3 py-2.5 text-sm text-paper/85 transition hover:border-white/30 hover:bg-white/[0.06]"
        >
          <Plus size={14} strokeWidth={1.5} /> 新建对话{pendingCourse.courseId ? ` · ${pendingCourse.subject}` : ''}
        </button>
        {showCoursePicker && (
          <div className="mt-1.5 space-y-1 rounded-lg border border-white/10 bg-white/[0.04] p-1.5">
            {COURSES.map((c) => (
              <button
                key={c.name}
                onClick={() => setPendingCourse(c)}
                className={`flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-left text-[12px] transition ${
                  pendingCourse.name === c.name ? 'bg-blue-600/25 text-paper' : 'text-paper/60 hover:bg-white/[0.05]'
                }`}
              >
                <BookMarked size={12} strokeWidth={1.5} className="shrink-0 opacity-60" aria-hidden />
                <span className="flex-1 truncate">{c.name}</span>
                <span className="shrink-0 text-[10px] text-paper/40">{c.subject}</span>
              </button>
            ))}
            <button
              onClick={() => void createIn()}
              className="mt-0.5 w-full rounded-md bg-blue-600 py-1.5 text-[12px] font-semibold text-white transition hover:bg-blue-500"
            >
              创建
            </button>
          </div>
        )}
      </div>

      {/* 搜索框 */}
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

      {/* 会话列表：按学科分组 */}
      <nav className="mt-3 flex-1 overflow-y-auto px-3 pb-4" aria-label="会话列表">
        {groups.map(([subject, list]) => (
          <div key={subject} className="mb-3">
            <div className="mb-1 flex items-center gap-1.5 px-2 text-[10px] font-semibold tracking-wider text-paper/35">
              <BookMarked size={10} strokeWidth={1.5} aria-hidden />
              {subject}
              <span className="ml-auto font-normal">{list.length}</span>
            </div>
            <div className="space-y-1">
              {list.map((s) => {
                const active = s.id === activeSessionId;
                return (
                  <div
                    key={s.id}
                    className={`group relative flex cursor-pointer items-center gap-2 rounded-lg px-3 py-2.5 text-sm transition ${
                      active ? 'bg-white/[0.08] text-paper' : 'text-paper/60 hover:bg-white/[0.04] hover:text-paper'
                    }`}
                    onClick={() => {
                      if (editingId !== s.id) {
                        switchSession(s.id);
                        onSelectSession?.();
                      }
                    }}
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
                        className="w-full rounded border border-paper/30 bg-transparent px-1.5 py-0.5 text-sm outline-none"
                      />
                    ) : (
                      <span className="min-w-0 flex-1">
                        <span className="block truncate" onDoubleClick={() => { setEditingId(s.id); setDraftTitle(s.title); }}>
                          {s.title}
                        </span>
                        {s.courseId && (
                          <span className="mt-0.5 block truncate text-[10px] text-paper/35">{s.chapter || s.courseId}</span>
                        )}
                      </span>
                    )}
                    {/* 显式重命名按钮：旧实现只靠 onDoubleClick——触屏双击
                        不可靠、桌面端也无从得知双击可改名（零发现性）。
                        与删除按钮同一触控标准：常驻显示、stopPropagation
                        防误触切会话；编辑态下让位给输入框。 */}
                    {editingId !== s.id && (
                      <button
                        aria-label={`重命名会话 ${s.title}`}
                        title="重命名会话"
                        className="shrink-0 rounded px-1.5 py-0.5 text-[11px] text-paper/40 transition hover:text-blue-300"
                        onClick={(e) => {
                          e.stopPropagation();
                          setEditingId(s.id);
                          setDraftTitle(s.title);
                        }}
                      >
                        <Pencil size={12} strokeWidth={1.5} />
                      </button>
                    )}
                    <button
                      aria-label={
                        confirmDeleteId === s.id ? `确认删除会话 ${s.title}` : `删除会话 ${s.title}`
                      }
                      // 常驻显示（原为 hidden + group-hover:block）：触屏没有 hover，
                      // 移动端根本点不到删除入口；触摸目标一并放大到 24px 级。
                      className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] transition ${
                        confirmDeleteId === s.id
                          ? 'bg-red-500/15 text-red-400'
                          : 'text-paper/40 hover:text-red-400'
                      }`}
                      onClick={(e) => {
                        e.stopPropagation();
                        if (confirmDeleteId === s.id) {
                          void removeSession(s.id);
                          setConfirmDeleteId(null);
                        } else {
                          setConfirmDeleteId(s.id);
                        }
                      }}
                      onBlur={() => confirmDeleteId === s.id && setConfirmDeleteId(null)}
                    >
                      {confirmDeleteId === s.id ? '确认删除' : '✕'}
                    </button>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
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
