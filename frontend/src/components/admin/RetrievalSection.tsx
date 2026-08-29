'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com

/** 检索调权分区：语义化预设（默认展示）+ 高级原始系数（折叠）+ 考点定版管理 */
import { useCallback, useEffect, useState } from 'react';
import { Award, ChevronDown, RotateCcw, SlidersHorizontal } from 'lucide-react';
import {
  AdminChunkInfo,
  AdminConfigView,
  RetrievalWeights,
  fetchAdminChunks,
  saveRetrievalWeights,
  setChunkCanonical,
} from '@/lib/api';

const PRESETS: Array<{ key: RetrievalWeights['profile']; label: string; desc: string }> = [
  { key: 'strict', label: '严格跟随老师', desc: '定版解法权重拉满 + 随堂时间加权，AI 严格按老师当前讲法' },
  { key: 'balanced', label: '均衡（推荐）', desc: '定版与时间适度加权，兼顾新授进度与历史内容' },
  { key: 'explore', label: '自由探索', desc: '解除定版与时间加权，纯语义检索，适合出题与横向对比' },
];

const FIELDS: Array<{ key: keyof Omit<RetrievalWeights, 'profile'>; label: string; max: number; step: number }> = [
  { key: 'vector', label: '向量语义权重', max: 1, step: 0.05 },
  { key: 'lexical', label: '词面重合权重', max: 1, step: 0.05 },
  { key: 'bm25', label: 'BM25 关键词权重', max: 1, step: 0.05 },
  { key: 'canonical_bonus', label: '定版解法加成', max: 1, step: 0.1 },
  { key: 'time_alpha', label: '时间衰减强度 α', max: 1, step: 0.05 },
];

export default function RetrievalSection({ config }: { config: AdminConfigView }) {
  const weights = config.retrieval;
  const [draft, setDraft] = useState<RetrievalWeights | null>(weights ?? null);
  const [advanced, setAdvanced] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [hint, setHint] = useState('');

  const [chunks, setChunks] = useState<AdminChunkInfo[]>([]);

  useEffect(() => {
    void fetchAdminChunks().then(setChunks).catch(() => undefined);
  }, []);

  const save = useCallback(
    async (next: RetrievalWeights) => {
      setSaving(true);
      setHint('');
      try {
        const view = await saveRetrievalWeights(next);
        setDraft(view.retrieval ? { ...view.retrieval } : next);
        setDirty(false);
        setHint('✅ 已保存并即时生效');
      } catch (e) {
        setHint(`保存失败：${(e as Error).message}`);
      } finally {
        setSaving(false);
      }
    },
    [],
  );

  const applyPreset = (key: RetrievalWeights['profile']) => {
    if (!draft) return;
    setDirty(true);
    // 切预设时清掉高级覆盖，回到该预设的干净值（后端按预设解析）
    setDraft({ ...draft, profile: key });
    void save({ ...draft, profile: key });
  };

  const toggleCanonical = async (c: AdminChunkInfo) => {
    await setChunkCanonical(c.id, !c.is_canonical);
    setChunks((prev) =>
      prev.map((x) =>
        x.exam_point === c.exam_point && x.course_id === c.course_id
          ? { ...x, is_canonical: x.id === c.id ? !c.is_canonical : false, supersedes: x.id === c.id ? c.id : x.supersedes }
          : x,
      ),
    );
    setHint(`已${c.is_canonical ? '取消' : '将「' + c.exam_point + '」设为'}标准解法`);
    void fetchAdminChunks().then(setChunks).catch(() => undefined);
  };

  if (!draft) return null;

  return (
    <section className="paper-card animate-rise px-6 py-5">
      <h2 className="flex items-center gap-1.5 text-[14.5px] font-semibold text-ink">
        <SlidersHorizontal size={15} strokeWidth={1.5} aria-hidden />
        检索调权
      </h2>
      <p className="mt-1 text-[12px] text-ink-faint">
        决定 AI 检索课堂内容的倾向：定版解法权威性、时间衰减强度与各路信号配比。保存即时生效。
      </p>

      {/* 第一层：语义化预设 */}
      <div className="mt-4 grid gap-2.5 sm:grid-cols-3">
        {PRESETS.map((p) => (
          <button
            key={p.key}
            onClick={() => applyPreset(p.key)}
            disabled={saving}
            className={`rounded-xl border px-4 py-3 text-left transition disabled:opacity-50 ${
              draft.profile === p.key
                ? 'border-chalk bg-chalk-soft/60 shadow-sm'
                : 'border-rule bg-white/70 hover:border-chalk/40'
            }`}
          >
            <div className={`text-[13px] font-semibold ${draft.profile === p.key ? 'text-chalk' : 'text-ink'}`}>
              {p.label}
            </div>
            <div className="mt-0.5 text-[11px] leading-relaxed text-ink-faint">{p.desc}</div>
          </button>
        ))}
      </div>

      {/* 第二层：高级原始系数（默认折叠） */}
      <button
        onClick={() => setAdvanced((v) => !v)}
        className="mt-4 inline-flex items-center gap-1 text-[11.5px] text-ink-faint transition hover:text-ink-soft"
      >
        <ChevronDown size={13} strokeWidth={1.5} className={`transition-transform ${advanced ? 'rotate-180' : ''}`} aria-hidden />
        高级设置（原始系数，不确定请勿改动）
      </button>
      {advanced && (
        <div className="mt-3 grid gap-3 rounded-xl border border-dashed border-rule bg-paper-deep/30 px-4 py-4 sm:grid-cols-2">
          {FIELDS.map((f) => (
            <label key={f.key} className="block">
              <span className="mb-1 flex items-baseline justify-between">
                <span className="text-[11.5px] font-medium text-ink-soft">{f.label}</span>
                <span className="font-mono text-[11px] text-ink">{Number(draft[f.key]).toFixed(2)}</span>
              </span>
              <input
                type="range"
                min={0}
                max={f.max}
                step={f.step}
                value={Number(draft[f.key])}
                onChange={(e) => {
                  setDirty(true);
                  setDraft({ ...draft, [f.key]: Number(e.target.value) });
                }}
                className="w-full accent-blue-600"
              />
            </label>
          ))}
          <div className="flex items-end gap-2 sm:col-span-2">
            <button
              onClick={() => {
                const base = PRESETS.find((p) => p.key === draft.profile);
                setHint('已恢复默认（未保存，点保存生效）');
                setDirty(true);
                if (base) {
                  const defaults: Record<string, Record<string, number>> = {
                    strict: { vector: 0.45, lexical: 0.15, bm25: 0.15, canonical_bonus: 0.5, time_alpha: 0.30 },
                    balanced: { vector: 0.50, lexical: 0.20, bm25: 0.20, canonical_bonus: 0.3, time_alpha: 0.20 },
                    explore: { vector: 0.65, lexical: 0.20, bm25: 0.15, canonical_bonus: 0.0, time_alpha: 0.0 },
                  };
                  const d = defaults[base.key];
                  setDraft({ ...draft, ...d } as RetrievalWeights);
                }
              }}
              className="inline-flex items-center gap-1 rounded-md border border-rule px-2.5 py-1 text-[11px] text-ink-faint transition hover:text-ink-soft"
            >
              <RotateCcw size={11} strokeWidth={1.5} /> 恢复当前预设默认
            </button>
          </div>
        </div>
      )}

      <div className="mt-4 flex items-center gap-3">
        <button
          onClick={() => void save(draft)}
          disabled={saving || !dirty}
          className="rounded-lg bg-chalk px-5 py-2 text-[12.5px] font-semibold text-white transition hover:bg-blue-700 disabled:opacity-50"
        >
          {saving ? '保存中…' : dirty ? '保存并生效' : '已保存'}
        </button>
        {hint && <span className={`text-[12px] ${hint.includes('失败') ? 'text-cinnabar' : 'text-chalk'}`}>{hint}</span>}
      </div>

      {/* 定版管理：老师为考点指定标准解法 */}
      <div className="mt-6 border-t border-rule pt-4">
        <h3 className="flex items-center gap-1.5 text-[13px] font-semibold text-ink">
          <Award size={14} strokeWidth={1.5} className="text-amber-600" aria-hidden />
          考点定版管理
        </h3>
        <p className="mt-1 text-[11.5px] text-ink-faint">
          为每个考点指定「标准解法」切片：AI 解题以定版为准，与时间无关；设新定版会自动接管旧版（版本链记录）。
        </p>
        <ul className="mt-3 space-y-2">
          {chunks.map((c) => (
            <li key={c.id} className="flex items-center gap-3 rounded-lg border border-rule bg-white/70 px-3.5 py-2.5">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 text-[12.5px]">
                  <span className="truncate font-medium text-ink">{c.exam_point}</span>
                  {c.is_canonical && (
                    <span className="shrink-0 rounded-full bg-amber-500/15 px-2 py-0.5 text-[10px] font-bold text-amber-600">
                      定版 v{c.method_version}
                    </span>
                  )}
                  {c.supersedes && <span className="shrink-0 text-[10px] text-ink-faint">← 取代 {c.supersedes}</span>}
                </div>
                <div className="mt-0.5 truncate text-[11px] text-ink-faint">{c.text}</div>
              </div>
              <button
                onClick={() => void toggleCanonical(c)}
                className={`shrink-0 rounded-md border px-3 py-1.5 text-[11.5px] transition ${
                  c.is_canonical
                    ? 'border-amber-500/40 bg-amber-500/10 text-amber-600 hover:bg-amber-500/20'
                    : 'border-rule bg-white text-ink-soft hover:border-chalk hover:text-chalk'
                }`}
              >
                {c.is_canonical ? '取消定版' : '设为标准解法'}
              </button>
            </li>
          ))}
          {chunks.length === 0 && <li className="py-4 text-center text-[11.5px] text-ink-faint">暂无切片</li>}
        </ul>
      </div>
    </section>
  );
}
