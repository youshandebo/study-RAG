'use client';

/** 课程大纲树与考点掌握度分布（基于内置 10月15日 课堂考点 + 伴学掌握度） */
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
  const tutor = useTutorStore((s) => s.bySession[activeSessionId]);
  const mastery = tutor?.mastery ?? 0;

  return (
    <div className="px-5 py-4">
      {/* 掌握度总览 */}
      <div className="mb-4 rounded-xl border border-rule bg-[#fdfaf2] p-4">
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
