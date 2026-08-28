'use client';

/** 消息流容器：虚拟长列表优化（窗口化渲染）+ 自动滚动 + 空状态 */
import { useEffect, useRef } from 'react';
import { BookOpen, Camera, GitCompareArrows, Lightbulb, Zap } from 'lucide-react';
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
    <div
      ref={scrollRef}
      onScroll={onScroll}
      className={`flex-1 overflow-y-auto px-6 ${messages.length === 0 ? 'flex flex-col py-5' : 'py-5'}`}
    >
      {messages.length === 0 ? (
        <div className="m-auto w-full">
          <EmptyState />
        </div>
      ) : (
        <div className="mx-auto max-w-3xl space-y-5">
          {collapsed > 0 && (
            <div className="text-center text-[11.5px] text-ink-faint">
              ↑ 已折叠更早的 {collapsed} 条消息（滚动到顶部自动加载完整历史）
            </div>
          )}
          {visible.map((m) => (
            <MessageCardRenderer key={m.id} message={m} streaming={m.id === streamingMessageId} />
          ))}
          <div ref={bottomRef} />
        </div>
      )}
    </div>
  );
}

function EmptyState() {
  const chips = [
    { icon: Camera, label: '拍照解题' },
    { icon: Lightbulb, label: '启发教我' },
    { icon: Zap, label: '出题考我' },
    { icon: GitCompareArrows, label: '分屏比对' },
  ];
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <div className="flex h-11 w-11 items-center justify-center rounded-xl border border-rule/60 bg-white" aria-hidden>
        <BookOpen size={18} strokeWidth={1.5} className="text-chalk" />
      </div>
      <div className="mt-4 text-[17px] font-semibold text-ink">把课堂装进一次对话</div>
      <div className="mx-auto mt-2 max-w-md text-[13px] leading-relaxed text-ink-soft">
        拖入或粘贴一张题目照片，助教将按 <b>老师课堂原法</b> 推导解题、提示易错陷阱，
        并附上课堂录音与板书的原始证据。
      </div>
      <div className="mt-6 grid grid-cols-2 gap-2 text-[12.5px] text-ink-soft md:grid-cols-4">
        {chips.map((c) => (
          <span key={c.label} className="inline-flex items-center justify-center gap-1.5 rounded-full border border-rule/60 bg-white px-3 py-1.5">
            <c.icon size={14} strokeWidth={1.5} className="text-ink-faint" aria-hidden />
            {c.label}
          </span>
        ))}
      </div>
      <div className="mt-4 text-[11.5px] text-ink-faint">
        也可在右侧「入库」抽屉上传课堂录音 / 板书 / 笔记，提问时自动检索引用
      </div>
    </div>
  );
}
