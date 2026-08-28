'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com


/** 多模型分屏轨道：原地 2~4 栏并发流式对比，支持同步滚动与一键复制 */
import { useRef, useState } from 'react';
import { Check, Columns3, Copy, Link2, Link2Off } from 'lucide-react';
import Markdown from './Markdown';
import type { PolymorphicMessage } from '@/types/message';

const TRACK_ACCENT = ['text-chalk border-chalk/40', 'text-cinnabar border-cinnabar/40', 'text-warn border-warn/40', 'text-[#5b4a8a] border-[#5b4a8a]/40'];

export default function MultiModelSplitTrack({
  message,
  streaming,
}: {
  message: PolymorphicMessage;
  streaming: boolean;
}) {
  const tracks = message.comparePayload?.tracks ?? [];
  const [syncScroll, setSyncScroll] = useState(false);
  const [copied, setCopied] = useState(false);
  const scrollers = useRef<Array<HTMLDivElement | null>>([]);
  const syncingRef = useRef(false);

  if (!tracks.length) return null;
  const allDone = tracks.every((t) => t.status === 'done');
  const cols = Math.min(tracks.length, 4);

  // 同步滚动：以触发栏为准按滚动比例映射到其余栏
  const onScroll = (idx: number) => {
    if (!syncScroll || syncingRef.current) return;
    const src = scrollers.current[idx];
    if (!src) return;
    syncingRef.current = true;
    const ratio = src.scrollTop / Math.max(1, src.scrollHeight - src.clientHeight);
    scrollers.current.forEach((el, j) => {
      if (j === idx || !el) return;
      el.scrollTop = ratio * (el.scrollHeight - el.clientHeight);
    });
    requestAnimationFrame(() => {
      syncingRef.current = false;
    });
  };

  const copyAll = async () => {
    const nl = String.fromCharCode(10); // 模板里的换行统一在此构造，避免转义干扰
    const text = tracks
      .map((t) => '## ' + t.modelName + nl + nl + t.content)
      .join(nl + nl + '---' + nl + nl);
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      /* 剪贴板不可用时静默 */
    }
  };

  return (
    <div className="msg-enter">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="inline-flex items-center gap-1.5 rounded-md bg-ink px-2.5 py-1 text-[12px] font-semibold text-paper">
          <Columns3 size={12} strokeWidth={1.5} aria-hidden />
          多模型分屏比对
        </span>
        <span className="text-[11.5px] text-ink-faint">
          {allDone ? `${tracks.length} 个模型已全部完成` : '并发流式生成中…'}
        </span>
        <span className="ml-auto flex items-center gap-1.5">
          <button
            onClick={() => setSyncScroll((v) => !v)}
            title="同步垂直滚动"
            aria-pressed={syncScroll}
            className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[11.5px] transition ${
              syncScroll
                ? 'border-chalk/50 bg-chalk-soft text-chalk'
                : 'border-rule bg-white/70 text-ink-faint hover:text-ink-soft'
            }`}
          >
            {syncScroll ? <Link2 size={12} strokeWidth={1.5} /> : <Link2Off size={12} strokeWidth={1.5} />}
            同步滚动
          </button>
          <button
            onClick={() => void copyAll()}
            disabled={!allDone}
            title="复制全部解题 LaTeX 源码"
            className="inline-flex items-center gap-1.5 rounded-md border border-rule bg-white/70 px-2.5 py-1 text-[11.5px] text-ink-faint transition hover:text-ink-soft disabled:opacity-40"
          >
            {copied ? <Check size={12} strokeWidth={1.5} className="text-chalk" /> : <Copy size={12} strokeWidth={1.5} />}
            {copied ? '已复制' : '复制源码'}
          </button>
        </span>
      </div>
      <div
        className={`grid gap-3 ${cols >= 4 ? 'md:grid-cols-4' : cols === 3 ? 'md:grid-cols-3' : 'md:grid-cols-2'}`}
      >
        {tracks.map((t, i) => (
          <div key={i} className={`paper-card min-w-0 border-t-[3px] px-4 py-3 ${TRACK_ACCENT[i % TRACK_ACCENT.length]}`}>
            <div className="mb-2 flex items-center justify-between">
              <span className="font-display text-[12.5px] font-bold">{t.modelName}</span>
              {t.status === 'streaming' ? (
                <span className="stream-cursor text-[11px] text-ink-faint">生成中</span>
              ) : (
                <span className="text-[11px] text-chalk">✓ 完成</span>
              )}
            </div>
            <div
              ref={(el) => {
                scrollers.current[i] = el;
              }}
              onScroll={() => onScroll(i)}
              className="max-h-[420px] overflow-y-auto pr-1"
            >
              <Markdown text={t.content || '…'} />
            </div>
            <div className="mt-2 border-t border-rule/60 pt-1.5 text-right text-[11px] text-ink-faint">
              {t.content.length} 字符
            </div>
          </div>
        ))}
      </div>
      {streaming && !allDone && <div className="skeleton mt-3 h-2 w-full" />}
    </div>
  );
}
