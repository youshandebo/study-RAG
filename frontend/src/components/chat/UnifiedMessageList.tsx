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
  return (
    <div className="flex flex-col items-center justify-center py-6 text-center">
      {/* 黑板 Hero 卡 */}
      <div className="relative w-full max-w-xl overflow-hidden rounded-2xl border border-white/10 bg-board px-8 py-10 text-paper shadow-[0_18px_50px_-18px_rgba(34,56,47,0.65)]">
        <div
          className="pointer-events-none absolute inset-0 opacity-[0.1]"
          style={{
            backgroundImage:
              'radial-gradient(rgba(255,255,255,0.7) 0.5px, transparent 0.5px), radial-gradient(rgba(255,255,255,0.5) 0.5px, transparent 0.5px)',
            backgroundSize: '26px 26px, 44px 44px',
            backgroundPosition: '0 0, 13px 21px',
          }}
        />
        {/* 粉笔公式装饰 */}
        <span className="pointer-events-none absolute left-6 top-4 select-none font-display text-2xl italic text-white/15" aria-hidden>
          ∫<sub>a</sub><sup>∞</sup> f(x)dx
        </span>
        <span className="pointer-events-none absolute right-8 top-6 select-none font-display text-xl italic text-white/[0.12]" aria-hidden>
          lim<span className="align-sub text-xs">n→∞</span>
        </span>
        <span className="pointer-events-none absolute bottom-4 left-10 select-none font-display text-lg italic text-white/[0.12]" aria-hidden>
          p&gt;1 ⇒ 收敛 ✓
        </span>
        <span className="pointer-events-none absolute bottom-5 right-6 select-none rotate-[-4deg] font-display text-lg italic text-[#e8c9a0]/40" aria-hidden>
          抓大头 ≠ 乱丢项！
        </span>

        <div className="relative">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-2xl border border-white/15 bg-white/10 text-2xl" aria-hidden>
            🎓
          </div>
          <div className="font-display text-[22px] font-bold tracking-wide text-[#f3efe2]">把课堂装进一次对话</div>
          <div className="mx-auto mt-2.5 max-w-md text-[13px] leading-relaxed text-white/70">
            拖入或粘贴一张题目照片，助教将按 <b className="text-[#c9ece2]">老师课堂原法</b> 推导解题、提示易错陷阱，
            并附上课堂录音与板书的原始证据。
          </div>
          <div className="mt-6 grid grid-cols-2 gap-2 text-[12.5px] md:grid-cols-4">
            {['📸 拍照解题', '💡 启发教我', '⚡ 出题考我', '🔀 分屏比对'].map((t) => (
              <span key={t} className="rounded-full border border-white/15 bg-white/[0.07] px-3 py-1.5 text-white/80">
                {t}
              </span>
            ))}
          </div>
        </div>
      </div>

      <div className="mt-4 text-[11.5px] text-ink-faint">
        也可以在右侧「入库」抽屉上传课堂录音 / 板书 / 笔记，提问时自动检索引用
      </div>
    </div>
  );
}
