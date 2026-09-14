'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com


/** 错题本抽屉：列表过滤 + Leitner 闪卡复习 + 变式题练兵（+ 租户卡点看板页签）。
 *
 * 设计取舍：不复用右侧 `EvidenceDrawer` 那条 aside——那是主布局里的常驻列，
 * 错题本是"按需弹出的个人学习空间"，用覆盖式抽屉更合适，也不挤压对话画布。
 */
import { useState } from 'react';
import { X } from 'lucide-react';
import Markdown from '@/components/chat/Markdown';
import ConceptStrugglesWidget from './ConceptStrugglesWidget';
import { generateVariant, reviewMistake } from '@/lib/api';
import { useNotebookStore, type NotebookFilter } from '@/stores/useNotebookStore';
import type { MistakeItem, VariantQuestion } from '@/types/notebook';

const SOURCE_LABEL: Record<string, { label: string; cls: string }> = {
  passive_converge: { label: '自测失败', cls: 'bg-cinnabar-soft text-cinnabar' },
  active_manual: { label: '手动收录', cls: 'bg-chalk-soft text-chalk' },
  exam_failed: { label: '考试失分', cls: 'bg-warn-soft text-warn' },
};

const FILTERS: { key: NotebookFilter; label: string }[] = [
  { key: '', label: '全部' },
  { key: 'active', label: '待复习' },
  { key: 'mastered', label: '已攻克' },
];

function fmtRelative(ms: number): string {
  if (!ms) return '—';
  const diff = ms - Date.now();
  const day = 86_400_000;
  const days = Math.round(diff / day);
  if (days < 0) return `已逾期 ${-days} 天`;
  if (days === 0) return '今天';
  if (days === 1) return '明天';
  return `${days} 天后`;
}

/** Leitner 盒子进度格（1~5） */
function BoxTrack({ box }: { box: number }) {
  return (
    <span className="flex items-center gap-0.5" title={`Leitner Box ${box}/5（越高复习间隔越长）`}>
      {Array.from({ length: 5 }).map((_, i) => (
        <span
          key={i}
          className={`h-2.5 w-2.5 rounded-sm ${i < box ? (box >= 5 ? 'bg-chalk' : 'bg-warn') : 'bg-rule'}`}
        />
      ))}
    </span>
  );
}

export default function NotebookDrawer({ canManage }: { canManage: boolean }) {
  const { open, tab, setTab, items, loading, statusFilter, setStatusFilter, closeDrawer, refresh } =
    useNotebookStore();
  const [reviewing, setReviewing] = useState<MistakeItem | null>(null);
  const [queue, setQueue] = useState<MistakeItem[]>([]);

  if (!open) return null;

  const startTodayReview = () => {
    // 队列按到期顺序；这里用列表内"待复习"条目近似（角标由后端 due 口径给出）
    const due = items.filter((i) => i.status === 'active' && i.nextReviewAt <= Date.now());
    const pool = due.length ? due : items.filter((i) => i.status === 'active');
    if (!pool.length) return;
    setQueue(pool);
    setReviewing(pool[0]);
  };

  const nextInQueue = () => {
    if (!reviewing) return;
    const idx = queue.findIndex((q) => q.id === reviewing.id);
    const next = queue[idx + 1];
    if (next) setReviewing(next);
    else {
      setReviewing(null);
      setQueue([]);
      void refresh();
    }
  };

  return (
    <>
      <div className="fixed inset-0 z-40 bg-zinc-950/30" onClick={closeDrawer} aria-hidden />
      <aside
        className="fixed inset-y-0 right-0 z-50 flex w-[440px] max-w-[92vw] flex-col border-l border-rule bg-paper shadow-2xl"
        aria-label="错题本"
      >
        <div className="flex items-center justify-between border-b border-rule px-4 py-3">
          <div className="flex items-center gap-2">
            <h2 className="font-display text-[14px] font-bold text-ink">📕 错题本</h2>
            {canManage && (
              <div className="flex gap-1" role="tablist">
                <button
                  role="tab"
                  aria-selected={tab === 'review'}
                  onClick={() => setTab('review')}
                  className={`rounded-md px-2.5 py-1 text-[12px] font-medium transition ${tab === 'review' ? 'bg-ink text-paper' : 'text-ink-soft hover:bg-paper-deep'}`}
                >
                  我的复习
                </button>
                <button
                  role="tab"
                  aria-selected={tab === 'struggles'}
                  onClick={() => setTab('struggles')}
                  className={`rounded-md px-2.5 py-1 text-[12px] font-medium transition ${tab === 'struggles' ? 'bg-ink text-paper' : 'text-ink-soft hover:bg-paper-deep'}`}
                >
                  机构卡点
                </button>
              </div>
            )}
          </div>
          <button onClick={closeDrawer} className="px-2 text-ink-faint hover:text-ink" aria-label="关闭错题本">
            <X size={15} strokeWidth={1.5} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-3">
          {tab === 'struggles' && canManage ? (
            <ConceptStrugglesWidget />
          ) : (
            <div className="space-y-3">
              <div className="flex items-center justify-between gap-2">
                <div className="flex gap-1">
                  {FILTERS.map((f) => (
                    <button
                      key={f.key}
                      onClick={() => setStatusFilter(f.key)}
                      className={`rounded-md px-2.5 py-1 text-[12px] transition ${
                        statusFilter === f.key ? 'bg-chalk-soft font-medium text-chalk' : 'text-ink-soft hover:bg-paper-deep'
                      }`}
                    >
                      {f.label}
                    </button>
                  ))}
                </div>
                <button
                  onClick={startTodayReview}
                  disabled={!items.length}
                  className="rounded-lg bg-chalk px-3 py-1.5 text-[12px] font-semibold text-white transition hover:opacity-90 disabled:opacity-40"
                >
                  今日复习
                </button>
              </div>

              {loading && <div className="skeleton h-16 w-full rounded-lg" />}
              {!loading && items.length === 0 && (
                <p className="rounded-lg border border-dashed border-rule px-3 py-8 text-center text-[12.5px] text-ink-faint">
                  错题本还是空的。做错引导自测题时会自动收进来。
                </p>
              )}

              <ul className="space-y-2">
                {items.map((it) => {
                  const src = SOURCE_LABEL[it.sourceType] ?? SOURCE_LABEL.active_manual;
                  return (
                    <li key={it.id}>
                      <button
                        onClick={() => {
                          setQueue([]);
                          setReviewing(it);
                        }}
                        className="w-full rounded-lg border border-rule/70 bg-white px-3 py-2.5 text-left transition hover:border-warn hover:bg-warn-soft/30"
                      >
                        <div className="flex items-center gap-2">
                          <span className={`rounded px-1.5 py-0.5 text-[10.5px] font-medium ${src.cls}`}>{src.label}</span>
                          <span className="min-w-0 flex-1 truncate text-[12px] text-ink-soft" title={it.conceptTag}>
                            {it.conceptTag}
                          </span>
                          <BoxTrack box={it.leitnerBox} />
                        </div>
                        <p className="mt-1.5 line-clamp-2 text-[13px] text-ink">{it.questionContext || '（无题干）'}</p>
                        <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[10.5px] text-ink-faint">
                          <span>掌握度 <b className="font-mono text-ink-soft">{it.masteryScore}</b></span>
                          <span>复习 <b className="font-mono text-ink-soft">{it.reviewCount}</b> 次</span>
                          <span>
                            下次 <b className={it.nextReviewAt <= Date.now() ? 'text-cinnabar' : 'text-ink-soft'}>
                              {fmtRelative(it.nextReviewAt)}
                            </b>
                          </span>
                        </div>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
        </div>
      </aside>

      {reviewing && (
        <ReviewFlashcard
          item={reviewing}
          onClose={() => {
            setReviewing(null);
            setQueue([]);
            void refresh();
          }}
          onNext={queue.length ? nextInQueue : undefined}
          onReviewed={() => void refresh()}
        />
      )}
    </>
  );
}

/** Leitner 闪卡：作答 → 服务端判分 → 反馈（升盒/降盒）→ 变式题练兵 */
function ReviewFlashcard({
  item,
  onClose,
  onNext,
  onReviewed,
}: {
  item: MistakeItem;
  onClose: () => void;
  onNext?: () => void;
  onReviewed?: () => void;
}) {
  const [answer, setAnswer] = useState('');
  const [grade, setGrade] = useState<
    { correct: boolean | null; reason: string; box: number; status: string; nextReviewAt: number } | null
  >(null);
  const [busy, setBusy] = useState(false);
  const [variant, setVariant] = useState<VariantQuestion | null>(null);
  const [variantBusy, setVariantBusy] = useState(false);
  const [error, setError] = useState('');

  const submit = async (text: string) => {
    if (busy || grade) return;
    if (!text.trim()) return;
    setBusy(true);
    setError('');
    try {
      const res = await reviewMistake(item.id, text.trim());
      setGrade({
        correct: res.correct,
        reason: res.reason,
        box: res.item?.leitnerBox ?? item.leitnerBox,
        status: res.item?.status ?? item.status,
        nextReviewAt: res.item?.nextReviewAt ?? 0,
      });
      onReviewed?.();
    } catch (e) {
      setError((e as Error).message || '提交失败');
    } finally {
      setBusy(false);
    }
  };

  const makeVariant = async () => {
    setVariantBusy(true);
    setError('');
    try {
      const res = await generateVariant(item.id);
      setVariant(res.question);
    } catch (e) {
      setError((e as Error).message || '变式题生成失败');
    } finally {
      setVariantBusy(false);
    }
  };

  const hasOptions = !!item.options?.length;

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-zinc-950/40 p-4" role="dialog" aria-modal="true">
      <div className="flex max-h-[86vh] w-[560px] max-w-full flex-col overflow-hidden rounded-2xl border border-rule bg-paper shadow-2xl">
        <div className="flex items-center justify-between border-b border-rule px-5 py-3">
          <div className="flex items-center gap-2">
            <span className="font-display text-[13px] font-bold text-ink">Leitner 复习</span>
            {item.conceptTag && (
              <span className="rounded bg-paper-deep px-1.5 py-0.5 text-[10.5px] text-ink-soft">{item.conceptTag}</span>
            )}
            <BoxTrack box={grade?.box ?? item.leitnerBox} />
          </div>
          <button onClick={onClose} className="px-2 text-ink-faint hover:text-ink" aria-label="关闭复习">
            <X size={15} strokeWidth={1.5} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          {item.misconception && (
            <div className="mb-3 rounded-lg border border-warn/40 bg-warn-soft/40 px-3 py-2 text-[12px] text-ink-soft">
              <b className="text-warn">诊断误区：</b>
              <Markdown text={item.misconception} />
            </div>
          )}

          <div className="text-[14px] leading-relaxed text-ink">
            <Markdown text={item.questionContext || '（无题干）'} />
          </div>

          {!grade && hasOptions && (
            <div className="mt-4 space-y-2" role="radiogroup" aria-label="复习选项">
              {item.options!.map((opt, i) => (
                <button
                  key={i}
                  role="radio"
                  aria-checked={false}
                  disabled={busy}
                  onClick={() => submit(opt)}
                  className="w-full rounded-lg border border-rule bg-white px-4 py-2.5 text-left text-[13.5px] transition hover:border-warn hover:bg-warn-soft/40 disabled:opacity-50"
                >
                  <Markdown text={opt} />
                </button>
              ))}
            </div>
          )}

          {!grade && !hasOptions && (
            <div className="mt-4 flex gap-2">
              <input
                value={answer}
                onChange={(e) => setAnswer(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && void submit(answer)}
                placeholder="写下你的答案…"
                className="input-scroll flex-1 rounded-lg px-3.5 py-2 text-sm outline-none"
              />
              <button
                onClick={() => void submit(answer)}
                disabled={busy || !answer.trim()}
                className="rounded-lg bg-chalk px-4 py-2 text-sm font-semibold text-white transition hover:opacity-90 disabled:opacity-40"
              >
                提交
              </button>
            </div>
          )}

          {busy && <div className="skeleton mt-3 h-12 w-full rounded-lg" />}

          {grade && (
            <div
              className={`mt-4 rounded-lg border px-4 py-3 ${
                grade.correct === null
                  ? 'border-rule bg-warn-soft/40'
                  : grade.correct
                    ? 'border-chalk/50 bg-chalk-soft/60'
                    : 'border-cinnabar/50 bg-cinnabar-soft/60'
              }`}
            >
              <div
                className={`font-display text-[13px] font-bold ${
                  grade.correct === null ? 'text-ink-soft' : grade.correct ? 'text-chalk' : 'text-cinnabar'
                }`}
              >
                {grade.correct === null
                  ? '⚠️ 本题判分未定，未改变复习调度'
                  : grade.correct
                    ? `✅ 答对了 → 升到 Box ${grade.box}${grade.status === 'mastered' ? '（已攻克 🎉）' : ''}`
                    : '❌ 答错了 → 重置回 Box 1，明天再考一次'}
              </div>
              {grade.reason && <div className="mt-1 text-[12.5px] text-ink-soft">{grade.reason}</div>}
              <div className="mt-1 text-[11.5px] text-ink-faint">
                掌握度与复习间隔已由服务端更新
                {grade.nextReviewAt ? `；下次复习：${fmtRelative(grade.nextReviewAt)}` : ''}
              </div>
            </div>
          )}

          {variant && (
            <div className="mt-4 rounded-lg border border-chalk/40 bg-chalk-soft/40 px-4 py-3">
              <div className="mb-1 text-[12px] font-semibold text-chalk">🔁 变式题练手</div>
              <div className="text-[13.5px] text-ink">
                <Markdown text={variant.questionText} />
              </div>
              {!!variant.options?.length && (
                <ul className="mt-2 space-y-1">
                  {variant.options.map((o, i) => (
                    <li key={i} className="rounded border border-rule/60 bg-white px-3 py-1.5 text-[13px]">
                      <Markdown text={o} />
                    </li>
                  ))}
                </ul>
              )}
              {variant.explanation && (
                <details className="mt-2">
                  <summary className="cursor-pointer text-[12px] font-semibold text-ink-soft">查看解析</summary>
                  <div className="mt-1 text-[12.5px] text-ink-soft">
                    <Markdown text={variant.explanation} />
                  </div>
                </details>
              )}
            </div>
          )}

          {error && <div className="mt-3 text-[12.5px] text-cinnabar">{error}</div>}
        </div>

        <div className="flex items-center justify-between gap-2 border-t border-rule px-5 py-3">
          <button
            onClick={() => void makeVariant()}
            disabled={variantBusy}
            className="rounded-lg border border-chalk/50 px-3 py-1.5 text-[12.5px] font-medium text-chalk transition hover:bg-chalk-soft/50 disabled:opacity-40"
          >
            {variantBusy ? '生成中…' : '生成变式题练手'}
          </button>
          <div className="flex gap-2">
            {onNext && (
              <button
                onClick={onNext}
                className="rounded-lg bg-board px-3 py-1.5 text-[12.5px] font-semibold text-paper transition hover:opacity-90"
              >
                下一题
              </button>
            )}
            <button
              onClick={onClose}
              className="rounded-lg border border-rule/60 px-3 py-1.5 text-[12.5px] text-ink-soft transition hover:border-rule"
            >
              完成
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
