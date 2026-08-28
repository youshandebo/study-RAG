'use client';

/** 靶向测验卡片：选项作答 + 即时诊断反馈 */
import { useState } from 'react';
import Markdown from './Markdown';
import { gradeQuiz } from '@/lib/api';
import type { PolymorphicMessage } from '@/types/message';

interface GradeInfo {
  correct: boolean;
  chosen: string;
  attribution: string;
  suggestion: string;
}

export default function QuizInteractiveCard({
  message,
  streaming,
}: {
  message: PolymorphicMessage;
  streaming: boolean;
}) {
  const payload = message.quizPayload;
  const [selected, setSelected] = useState<number | null>(null);
  const [grade, setGrade] = useState<GradeInfo | null>(null);
  const [grading, setGrading] = useState(false);

  const answer = async (index: number) => {
    if (grade || grading) return;
    setSelected(index);
    setGrading(true);
    const g = await gradeQuiz(index);
    setGrade(g);
    setGrading(false);
  };

  return (
    <div className="paper-card msg-enter border-warn/40 px-5 py-4">
      <div className="mb-3 flex items-center gap-2">
        <span className="rounded-md bg-warn px-2.5 py-1 font-display text-[12px] font-bold text-white">
          ⚡ 靶向自测
        </span>
        {payload?.targetPitfall && (
          <span className="truncate text-[11.5px] text-ink-faint">针对陷阱：{payload.targetPitfall}</span>
        )}
      </div>

      <Markdown text={message.content} className={streaming ? 'stream-cursor' : ''} />

      {!streaming && payload?.options && (
        <div className="mt-3 space-y-2" role="radiogroup" aria-label="选项">
          {payload.options.map((opt, i) => {
            const isChosen = selected === i;
            const state = !grade
              ? 'idle'
              : i === 0
                ? 'correct'
                : isChosen
                  ? 'wrong'
                  : 'idle';
            return (
              <button
                key={i}
                role="radio"
                aria-checked={isChosen}
                disabled={!!grade || grading}
                onClick={() => answer(i)}
                className={`w-full rounded-lg border px-4 py-2.5 text-left text-[13.5px] transition ${
                  state === 'correct'
                    ? 'border-chalk bg-chalk-soft text-chalk'
                    : state === 'wrong'
                      ? 'border-cinnabar bg-cinnabar-soft text-cinnabar'
                      : 'border-rule bg-white hover:border-warn hover:bg-warn-soft/50'
                }`}
              >
                <Markdown text={opt} />
              </button>
            );
          })}
        </div>
      )}

      {grading && <div className="skeleton mt-3 h-10 w-full" />}

      {grade && (
        <div
          className={`mt-3 rounded-lg border px-4 py-3 ${
            grade.correct ? 'border-chalk/50 bg-chalk-soft/60' : 'border-cinnabar/50 bg-cinnabar-soft/60'
          }`}
        >
          <div className={`font-display text-[13px] font-bold ${grade.correct ? 'text-chalk' : 'text-cinnabar'}`}>
            {grade.correct ? '✅ 回答正确' : '❌ 回答有误'} · 选择了 {grade.chosen}
          </div>
          <div className="mt-1.5 text-[13px] text-ink-soft">
            <Markdown text={grade.attribution} />
          </div>
          <div className="mt-1.5 text-[12.5px] text-ink-faint">📌 {grade.suggestion}</div>
          {!grade.correct && payload?.explanation && (
            <details className="mt-2">
              <summary className="cursor-pointer text-[12.5px] font-semibold text-ink-soft">查看完整解析</summary>
              <div className="mt-1.5 text-[13px] text-ink-soft">
                <Markdown text={payload.explanation} />
              </div>
            </details>
          )}
        </div>
      )}
    </div>
  );
}
