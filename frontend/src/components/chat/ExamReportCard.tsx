'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
/** 复习卷批改报告卡片：得分总览 + 逐题分步采分 + 错因分布 + 变式新卷。
 *
 * 渲染契约 = 后端 ExamReportPayload（键名与后端一致）：
 *  - summary：得分率/对错统计/错因分布（运营端可聚合信号）
 *  - questions[].rubric.steps：分步采分点（hit/partial/miss 三态 + follow_through）
 *  - variants：同考点变式题，复用 quiz 渲染口径（题干+选项+答案）
 */
import { useState } from 'react';
import { ChevronDown, ClipboardCheck } from 'lucide-react';
import Markdown from './Markdown';
import type { PolymorphicMessage } from '@/types/message';

type StepHit = 'hit' | 'partial' | 'miss' | '';

const HIT_META: Record<StepHit, { label: string; cls: string; dot: string }> = {
  hit: { label: '得分', cls: 'text-chalk border-chalk/40 bg-chalk-soft', dot: 'bg-chalk' },
  partial: { label: '半对', cls: 'text-warn border-warn/40 bg-warn-soft', dot: 'bg-warn' },
  miss: { label: '失分', cls: 'text-cinnabar border-cinnabar/40 bg-cinnabar-soft', dot: 'bg-cinnabar' },
  '': { label: '—', cls: 'text-ink-faint border-rule bg-paper-deep', dot: 'bg-ink-faint' },
};

const VERDICT_META = {
  true: { label: '答对', cls: 'bg-chalk-soft text-chalk border-chalk/40' },
  false: { label: '答错', cls: 'bg-cinnabar-soft text-cinnabar border-cinnabar/40' },
  null: { label: '待复核', cls: 'bg-paper-deep text-ink-faint border-rule' },
} as const;

function pct(n: number | null | undefined): string {
  if (n == null || !isFinite(n)) return '—';
  return `${Math.round(n * 100)}%`;
}

/** 顶部得分总览条：得分率大字 + 对/错/待复核计数 + 错因分布 chips */
function SummaryBar({ summary }: { summary: NonNullable<PolymorphicMessage['examReportPayload']>['summary'] }) {
  const dist = Object.entries(summary.error_distribution ?? {}).filter(([k]) => k !== 'none');
  const ERROR_LABEL: Record<string, string> = {
    concept: '概念理解有误',
    computation: '计算失误',
    method: '方法选择不当',
    omission: '步骤跳跃或遗漏',
  };
  return (
    <div className="rounded-lg border border-rule bg-paper-deep/60 px-4 py-3">
      <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2">
        <div className="flex items-baseline gap-1.5">
          <span className="text-2xl font-bold tabular-nums text-ink">{pct(summary.score_rate)}</span>
          <span className="text-[12px] text-ink-faint">得分率</span>
          <span className="ml-1 text-[12px] tabular-nums text-ink-faint">
            ({summary.score_earned}/{summary.score_possible} 分)
          </span>
        </div>
        <div className="flex gap-4 text-[13px] tabular-nums">
          <span className="text-ink-soft">
            共 <b className="text-ink">{summary.total}</b> 题
          </span>
          <span className="text-chalk">对 {summary.correct}</span>
          <span className="text-cinnabar">错 {summary.wrong}</span>
          {summary.needs_review > 0 && <span className="text-warn">待复核 {summary.needs_review}</span>}
        </div>
      </div>
      {dist.length > 0 && (
        <div className="mt-2.5 flex flex-wrap items-center gap-1.5 text-[12px]">
          <span className="text-ink-faint">错因分布</span>
          {dist.map(([k, v]) => (
            <span key={k} className="rounded-full border border-cinnabar/25 bg-cinnabar-soft px-2 py-0.5 text-cinnabar">
              {ERROR_LABEL[k] ?? k} ×{v}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/** 单题采分点条（hit=蓝/partial=琥珀/miss=红 三态点列） */
function StepsBar({ steps }: { steps: NonNullable<NonNullable<PolymorphicMessage['examReportPayload']>['questions'][number]['rubric']>['steps'] }) {
  if (!steps.length) return null;
  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5">
      {steps.map((s) => {
        const meta = HIT_META[s.hit as StepHit] ?? HIT_META[''];
        return (
          <span
            key={s.no}
            title={`${s.name}：${s.awarded}/${s.points} 分`}
            className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11.5px] ${meta.cls}`}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${meta.dot}`} aria-hidden />
            {s.no}. {s.name} · {meta.label} {s.awarded}/{s.points}
            {s.follow_through && <b className="ml-1">后续方法分</b>}
          </span>
        );
      })}
    </div>
  );
}

type Report = NonNullable<PolymorphicMessage['examReportPayload']>;

export default function ExamReportCard({ message, streaming }: { message: PolymorphicMessage; streaming: boolean }) {
  const payload = message.examReportPayload;
  if (!payload) return <div role="status" className="paper-card p-4 text-ink-faint">{streaming ? '正在批改复习卷…' : '报告载荷缺失，请重新加载会话。'}</div>;
  return (
    <section className="paper-card margin-rule min-w-0 space-y-4 px-3 py-4 sm:px-5" aria-label="复习卷批改报告">
      <h2 className="flex items-center gap-2 font-semibold text-ink"><ClipboardCheck size={18} />复习卷批改报告</h2>
      <SummaryBar summary={payload.summary} />
      <p className="text-xs text-ink-faint">分步得分仅统计成功完成分步批改的 {payload.summary.graded_by_rubric} 题，不代表全卷总分。未作答 {payload.summary.total - payload.summary.answered} 题。</p>
      <div className="space-y-2">{payload.questions.map((q) => <QuestionPanel key={q.index} q={q} />)}</div>
      {payload.variants.length > 0 && <section className="space-y-2" aria-label="变式练习">
        <h3 className="font-semibold text-ink">同考点变式练习</h3>
        <p className="text-xs text-ink-faint">先独立作答，再展开答案核对。</p>
        {payload.variants.map((v, i) => <VariantPanel key={`${v.for_question}-${i}`} variant={v} />)}
      </section>}
    </section>
  );
}

function VariantPanel({ variant: v }: { variant: Report['variants'][number] }) {
  return <article className="min-w-0 rounded-lg border border-rule p-3 text-ink">
    <div className="mb-2 text-xs text-ink-faint">对应第 {v.for_question} 题 · {v.exam_point} · 难度 {v.difficulty}/5</div>
    <Markdown text={v.question_text} />
    <ol className="mt-2 space-y-1 text-sm">{(v.options ?? []).map((option, i) => <li key={i}><Markdown text={option} /></li>)}</ol>
    <details className="mt-3 rounded border border-rule p-2">
      <summary className="cursor-pointer text-sm text-chalk">查看答案与解析</summary>
      <Markdown text={v.answer || '暂无标准答案，请交老师核对。'} />
      {v.explanation && <Markdown text={v.explanation} />}
      {v.target_pitfall && <p className="mt-2 text-xs text-cinnabar">易错点：{v.target_pitfall}</p>}
    </details>
  </article>;
}

/** 逐题折叠面板：默认只展开错题/待复核题，答对题收起为一行 */
function QuestionPanel({ q }: { q: NonNullable<PolymorphicMessage['examReportPayload']>['questions'][number] }) {
  const key = q.correct === false ? 'false' : q.correct === true ? 'true' : 'null';
  const verdict = VERDICT_META[key as keyof typeof VERDICT_META];
  const [open, setOpen] = useState(q.correct !== true);
  const rubric = q.rubric;
  return (
    <div className="overflow-hidden rounded-lg border border-rule bg-paper">
      <button
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-left transition hover:bg-paper-deep/70"
      >
        <ChevronDown size={14} className={`shrink-0 text-ink-faint transition-transform ${open ? '' : '-rotate-90'}`} aria-hidden />
        <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[11.5px] font-semibold ${verdict.cls}`}>{verdict.label}</span>
        <span className="min-w-0 flex-1 truncate text-[13px] text-ink">
          <span className="mr-1 font-semibold">第{q.index}题</span>
          {q.question_text}
        </span>
        {rubric && (
          <span className="shrink-0 text-[12px] tabular-nums text-ink-faint">
            {rubric.score}/{rubric.full_score} 分
          </span>
        )}
      </button>
      {open && (
        <div className="border-t border-rule px-3.5 pb-3 pt-2">
          {q.student_answer && (
            <div className="mb-2 text-[12.5px] leading-relaxed text-ink-soft">
              <span className="text-ink-faint">学生作答：</span>
              {q.student_answer}
            </div>
          )}
          {q.attribution && (
            <div className="mb-1 text-[12.5px] leading-relaxed text-ink">
              <span className="text-ink-faint">批改结论：</span>
              {q.attribution}
            </div>
          )}
          {q.socratic_followup && (
            <div className="mb-2 rounded border border-chalk/25 bg-chalk-soft px-2.5 py-1.5 text-[12.5px] text-chalk">
              跟进追问：{q.socratic_followup}
            </div>
          )}
          {rubric && <StepsBar steps={rubric.steps} />}
          {rubric?.summary && (
            <div className="mt-2 text-[12px] leading-relaxed text-ink-faint">{rubric.summary}</div>
          )}
        </div>
      )}
    </div>
  );
}
