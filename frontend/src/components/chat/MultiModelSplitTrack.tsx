'use client';

/** 多模型分屏轨道：原地 2~4 栏并发流式对比 */
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
  if (!tracks.length) return null;
  const allDone = tracks.every((t) => t.status === 'done');
  const cols = Math.min(tracks.length, 4);

  return (
    <div className="msg-enter">
      <div className="mb-2 flex items-center gap-2">
        <span className="rounded-md bg-ink px-2.5 py-1 font-display text-[12px] font-bold text-paper">
          🔀 多模型分屏比对
        </span>
        <span className="text-[11.5px] text-ink-faint">
          {allDone ? `${tracks.length} 个模型已全部完成` : '并发流式生成中…'}
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
            <div className="max-h-[420px] overflow-y-auto pr-1">
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
