'use client';

/** 上下文容量面板：仿 Cherry Studio「上下文容量」弹层，数据取最近一轮 usage 事件 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { Gauge } from 'lucide-react';
import type { UsageInfo } from '@/types/message';

const LABEL_COLORS: Record<string, string> = {
  消息: '#4f8de8',
  系统提示词: '#9b7fe8',
  检索上下文: '#38a169',
  回复输出: '#e8b34f',
  其他: '#98a2ad',
};
const FALLBACK_COLOR = '#6aa1d8';

function fmt(n: number): string {
  if (n >= 10000) return `${(n / 10000).toFixed(1)}万`;
  return n.toLocaleString();
}

export default function ContextMeter({ usages }: { usages: UsageInfo[] }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  // 取最近一轮（历史消息按时间升序，倒序找第一个带明细的）
  const latest = [...usages].reverse().find((u) => u.context_breakdown?.length);
  const limit = latest?.context_limit ?? 131072;
  // 容量 = 最近一轮的完整输入侧占用（该口径已含全量历史）
  const used = latest?.context_used ?? 0;
  const pct = Math.min(100, (used / limit) * 100);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener('mousedown', onDown);
    return () => window.removeEventListener('mousedown', onDown);
  }, [open]);

  const rows = useMemo(() => {
    if (!latest) return [];
    const bd = latest.context_breakdown;
    const totalBd = bd.reduce((s, b) => s + (b.tokens || 0), 0) || 1;
    return bd.map((b) => ({
      label: b.label,
      pct: ((b.tokens / totalBd) * 100).toFixed(1),
      color: LABEL_COLORS[b.label] ?? FALLBACK_COLOR,
    }));
  }, [latest]);

  return (
    <div ref={wrapRef} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        title="查看本会话上下文容量占比"
        className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] transition ${
          open ? 'border-chalk bg-chalk-soft text-chalk' : 'border-rule bg-white/70 text-ink-faint hover:text-ink-soft'
        }`}
      >
        <Gauge size={12} strokeWidth={1.5} aria-hidden />
        <span className="font-mono">上下文 {pct.toFixed(1)}%</span>
      </button>

      {open && (
        <div className="animate-rise absolute bottom-full right-0 mb-3 w-80 rounded-xl border border-rule bg-white p-4 shadow-[0_18px_48px_-12px_rgba(50,40,20,0.35)]">
          {/* 标题行 */}
          <div className="mb-2 flex items-baseline justify-between">
            <span className="text-[13px] font-semibold text-ink">上下文容量</span>
            {latest ? (
              <span className="font-mono text-[11px] text-ink-faint">
                {fmt(used)} / {fmt(latest.context_limit)}（{pct.toFixed(1)}%）
              </span>
            ) : (
              <span className="text-[11px] text-ink-faint">暂无数据</span>
            )}
          </div>

          {/* 进度条 */}
          <div className="h-2 overflow-hidden rounded-full bg-paper-deep">
            <div
              className="h-full rounded-full bg-gradient-to-r from-chalk to-[#5aa897] transition-all duration-500"
              style={{ width: `${Math.max(pct, 1)}%` }}
            />
          </div>

          {/* 分项占比 */}
          {rows.length > 0 && (
            <ul className="mt-3 space-y-1.5">
              {rows.map((r) => (
                <li key={r.label} className="flex items-center gap-2 text-[12px]">
                  <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: r.color }} />
                  <span className="flex-1 text-ink-soft">{r.label}</span>
                  <span className="font-mono text-ink-faint">{r.pct}%</span>
                </li>
              ))}
            </ul>
          )}
          {!latest && (
            <p className="mt-2 text-[11.5px] leading-relaxed text-ink-faint">
              完成一轮对话后，这里将展示消息 / 系统提示词 / 检索上下文等分项占比。
            </p>
          )}

          {/* 缓存命中率 */}
          {latest && (
            <>
              <div className="my-3 border-t border-dashed border-rule" />
              <div className="flex items-center justify-between text-[12px]">
                <span className="text-ink-soft">平均缓存命中率</span>
                <span className="font-mono font-semibold text-chalk">{(latest.cache_hit_rate * 100).toFixed(1)}%</span>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
