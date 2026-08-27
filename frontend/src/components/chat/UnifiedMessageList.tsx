'use client';

/** 消息流容器：虚拟长列表优化（窗口化渲染）+ 自动滚动 + 空状态 */
import { useEffect, useRef } from 'react';
import MessageCardRenderer from './MessageCardRenderer';
import { useSessionStore } from '@/stores/useSessionStore';

const WINDOW_SIZE = 60; // 保留最近 N 条在 DOM 中，更早的消息折叠
const EMPTY: never[] = [];

export default function UnifiedMessageList() {
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const streamingMessageId = useSessionStore((s) => s.streamingMessageId);
  // 注意：必须先取稳定的 map 引用，再派生数组，避免 ?? [] 每次生成新引用触发无限重渲染
  const messagesMap = useSessionStore((s) => s.messagesBySession);
  const messages = messagesMap[activeSessionId] ?? EMPTY;
  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const nearBottom = useRef(true);
  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    nearBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  };

  useEffect(() => {
    if (nearBottom.current) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }
  }, [messages, streamingMessageId]);

  const visible = messages.slice(-WINDOW_SIZE);
  const collapsed = messages.length - visible.length;

  return (
    <div ref={scrollRef} onScroll={onScroll} className="flex-1 overflow-y-auto px-6 py-5">
      <div className="mx-auto max-w-3xl space-y-5">
        {collapsed > 0 && (
          <div className="text-center text-[11.5px] text-ink-faint">
            ↑ 已折叠更早的 {collapsed} 条消息（滚动到顶部自动加载完整历史）
          </div>
        )}

        {messages.length === 0 && <EmptyState />}

        {visible.map((m) => (
          <MessageCardRenderer key={m.id} message={m} streaming={m.id === streamingMessageId} />
        ))}

        <div ref={bottomRef} />
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center py-24 text-center">
      <div className="font-display text-2xl font-bold text-ink">把课堂装进一次对话</div>
      <div className="mt-2 max-w-md text-[13.5px] leading-relaxed text-ink-soft">
        拖入或粘贴一张题目照片，助教将按 <b>老师课堂原法</b> 推导解题、提示易错陷阱，
        并附上课堂录音与板书的原始证据。
      </div>
      <div className="mt-6 grid grid-cols-2 gap-2 text-[12.5px] text-ink-faint md:grid-cols-4">
        {['📸 拍照解题', '💡 启发教我', '⚡ 出题考我', '🔀 分屏比对'].map((t) => (
          <span key={t} className="rounded-full border border-rule bg-[#fdfaf2] px-3 py-1.5">{t}</span>
        ))}
      </div>
    </div>
  );
}
