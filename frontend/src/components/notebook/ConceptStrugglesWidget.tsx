'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com


/** 租户级高频卡点看板：本机构学生共性盲区（教研视角）。
 *
 * 权限：仅 `role === 'tenant_admin'` 或平台超管可渲染；**服务端另有强制校验**，
 * 前端 role 只用于显隐，不作为授权依据。数据只含本租户（后端 resolve_tenant 注入）。
 */
import { useEffect, useState } from 'react';
import { Download, RefreshCw } from 'lucide-react';
import { exportConceptStruggles, fetchConceptStruggles } from '@/lib/api';
import type { ConceptStruggle } from '@/types/notebook';

export default function ConceptStrugglesWidget() {
  const [rows, setRows] = useState<ConceptStruggle[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState(false);

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      setRows(await fetchConceptStruggles(50));
    } catch (e) {
      setError((e as Error).message || '加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const visible = expanded ? rows : rows.slice(0, 5);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-[12px] text-ink-faint">
          按知识点聚合本机构学生的错题数与被动挂科率，用于定位共性盲区。
        </p>
        <div className="flex shrink-0 gap-1.5">
          <button
            onClick={() => void load()}
            disabled={loading}
            aria-label="刷新"
            title="刷新"
            className="rounded-md border border-rule/60 p-1.5 text-ink-soft transition hover:border-rule disabled:opacity-40"
          >
            <RefreshCw size={13} strokeWidth={1.5} className={loading ? 'animate-spin' : ''} />
          </button>
          <button
            onClick={() => void exportConceptStruggles(50).catch((e) => setError((e as Error).message))}
            className="inline-flex items-center gap-1 rounded-md border border-rule/60 px-2 py-1.5 text-[12px] text-ink-soft transition hover:border-rule"
          >
            <Download size={12} strokeWidth={1.5} />
            导出 CSV
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-cinnabar/40 bg-cinnabar-soft/50 px-3 py-2 text-[12.5px] text-cinnabar">
          {error}
        </div>
      )}

      {!loading && rows.length === 0 && !error && (
        <p className="rounded-lg border border-dashed border-rule px-3 py-6 text-center text-[12.5px] text-ink-faint">
          本机构暂无错题记录。
        </p>
      )}

      <ul className="space-y-2">
        {visible.map((r, i) => (
          <li key={r.conceptTag} className="rounded-lg border border-rule/70 bg-white px-3 py-2.5">
            <div className="flex items-center gap-2">
              <span className="font-display text-[11px] font-bold text-ink-faint">#{i + 1}</span>
              <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-ink" title={r.conceptTag}>
                {r.conceptTag}
              </span>
              <span className="shrink-0 rounded bg-paper-deep px-1.5 py-0.5 text-[10.5px] text-ink-soft">
                {r.mistakes} 次
              </span>
            </div>
            <div className="mt-2 flex items-center gap-2">
              <span className="w-16 shrink-0 text-[10.5px] text-ink-faint">掌握度</span>
              <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-rule">
                <span
                  className={`block h-full ${r.avgMastery >= 60 ? 'bg-chalk' : r.avgMastery >= 30 ? 'bg-warn' : 'bg-cinnabar'}`}
                  style={{ width: `${Math.max(2, Math.min(100, r.avgMastery))}%` }}
                />
              </span>
              <span className="w-10 shrink-0 text-right font-mono text-[10.5px] text-ink-soft">{r.avgMastery}</span>
            </div>
            <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[10.5px] text-ink-faint">
              <span>被动挂科 <b className="font-mono text-cinnabar">{r.passiveConverge}</b></span>
              <span>挂科率 <b className="font-mono text-ink-soft">{(r.convergeFailRate * 100).toFixed(0)}%</b></span>
              <span>已攻克 <b className="font-mono text-chalk">{r.mastered}</b></span>
            </div>
          </li>
        ))}
      </ul>

      {rows.length > 5 && (
        <button
          onClick={() => setExpanded((v) => !v)}
          className="w-full rounded-md border border-rule/60 py-1.5 text-[12px] text-ink-soft transition hover:border-rule"
        >
          {expanded ? '收起' : `展开其余 ${rows.length - 5} 个知识点`}
        </button>
      )}
    </div>
  );
}
