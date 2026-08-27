'use client';

/** ⚠️ 易错陷阱预警组件（嵌入解题步骤） */
import Markdown from './Markdown';

export default function PitfallAlertCard({ pitfalls }: { pitfalls: string[] }) {
  if (!pitfalls.length) return null;
  return (
    <div className="pitfall-card my-3 px-4 py-3">
      <div className="mb-1.5 font-display text-[13px] font-bold text-warn">⚠️ 课堂易错陷阱</div>
      <ul className="space-y-1.5">
        {pitfalls.map((p, i) => (
          <li key={i} className="flex gap-2 text-[13px] text-ink-soft">
            <span className="mt-[2px] shrink-0 font-bold text-warn">{i + 1}.</span>
            <Markdown text={p} />
          </li>
        ))}
      </ul>
    </div>
  );
}
