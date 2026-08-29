'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com


/** 课程大纲树与考点掌握度分布；节点点击联动检索模式与顶栏课程标签 */
import { BookMarked, GraduationCap, Zap } from 'lucide-react';
import { useSessionStore } from '@/stores/useSessionStore';
import { useTutorStore } from '@/stores/useTutorStore';

const OUTLINE = [
  {
    id: 'ch-5',
    title: '第五章 · 定积分的应用与延伸',
    children: [
      {
        id: 'ch-5-3',
        title: '5.3 反常积分',
        points: [
          { name: 'p-反常积分比较审敛法', difficulty: 4 },
          { name: 'p-积分敛散性判定定理', difficulty: 2 },
          { name: '对数项比阶放缩技巧', difficulty: 4 },
          { name: '比较审敛法常见误区', difficulty: 3 },
        ],
      },
    ],
  },
];

export default function CourseOutlineTree() {
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const sessions = useSessionStore((s) => s.sessions);
  const updateSessionMeta = useSessionStore((s) => s.updateSessionMeta);
  const activeSession = sessions.find((x) => x.id === activeSessionId);
  const reviewMode = activeSession?.retrievalMode === 'review';
  const tutor = useTutorStore((s) => s.bySession[activeSessionId]);
  const mastery = tutor?.mastery ?? 0;


  return (
    <div className="px-5 py-4">
      {/* 检索模式联动条：与顶栏切换按钮状态同步，点大纲节点即绑课程 */}
      <button
        onClick={() => activeSession && updateSessionMeta(activeSessionId, { retrievalMode: reviewMode ? 'lecture' : 'review' })}
        disabled={!activeSession}
        className={`mb-4 flex w-full items-center gap-2.5 rounded-xl border px-4 py-3 text-left transition disabled:opacity-40 ${
          reviewMode ? 'border-violet-500/25 bg-violet-500/5' : 'border-amber-500/25 bg-amber-500/5'
        }`}
      >
        <span aria-hidden>{reviewMode ? <GraduationCap size={16} strokeWidth={1.5} className="text-violet-600" /> : <Zap size={16} strokeWidth={1.5} className="text-amber-600" />}</span>
        <span className="min-w-0 flex-1">
          <span className={`block text-[12.5px] font-semibold ${reviewMode ? 'text-violet-700' : 'text-amber-700'}`}>
            {reviewMode ? '备考/复习模式' : '随堂模式'}
          </span>
          <span className="block truncate text-[11px] text-ink-faint">
            {reviewMode ? '纯语义检索，支持跨月多跳 · 点击切回随堂' : '近 1~2 周新授加权 · 点击切备考'}
          </span>
        </span>
        <BookMarked size={14} strokeWidth={1.5} className="shrink-0 text-ink-faint" aria-hidden />
      </button>

      {/* 掌握度总览 */}
      <div className="mb-4 rounded-xl border border-rule bg-white p-4">
        <div className="mb-2 flex items-center justify-between text-[13px]">
          <span className="font-semibold text-ink">本讲掌握度</span>
          <span className="font-display font-bold text-chalk">{mastery}%</span>
        </div>
        <div className="h-2 overflow-hidden rounded-full bg-paper-deep">
          <div
            className="h-full rounded-full bg-chalk transition-all duration-500"
            style={{ width: `${Math.max(3, mastery)}%` }}
          />
        </div>
        <div className="mt-2 text-[11.5px] text-ink-faint">
          引导步数 {Math.max(0, tutor?.stepIndex ?? 0)}/{tutor?.totalSteps ?? 5} · 伴学轮次 {tutor?.rounds ?? 0}
        </div>
      </div>

      {/* 大纲树 */}
      <div className="space-y-2">
        {OUTLINE.map((chapter) => (
          <details key={chapter.id} open className="rounded-lg border border-rule">
            <summary className="cursor-pointer px-4 py-2.5 text-[13px] font-semibold text-ink">
              {chapter.title}
            </summary>
            <div className="space-y-2 px-4 pb-3">
              {chapter.children?.map((sec) => (
                <div key={sec.id}>
                  <div className="mb-1.5 text-[12.5px] font-medium text-ink-soft">{sec.title}</div>
                  <ul className="space-y-1.5">
                    {sec.points.map((pt) => (
                      <li
                        key={pt.name}
                        className="flex items-center justify-between rounded-md bg-paper-deep/50 px-3 py-2 text-[12.5px]"
                      >
                        <span className="text-ink-soft">{pt.name}</span>
                        <span className="badge-diff text-[11px]">{'⭐'.repeat(pt.difficulty)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          </details>
        ))}
      </div>
    </div>
  );
}
