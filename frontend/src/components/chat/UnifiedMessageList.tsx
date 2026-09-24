'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com


/** 消息流容器：虚拟长列表优化（窗口化渲染）+ 自动滚动 + 空状态 */
import { useEffect, useRef, useState } from 'react';
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
  const [expanded, setExpanded] = useState(false);
  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    nearBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  };

  // 切会话必须重置"贴底"判定与展开态：在 A 会话上滑看历史后切到 B，
  // nearBottom 仍是 false，新会话会停在半空看不到最新消息（更看不到
  // 正在流式的回答）。这里用瞬时跳转而非平滑滚动——切会话不是滚动动画场景。
  useEffect(() => {
    nearBottom.current = true;
    setExpanded(false);
    bottomRef.current?.scrollIntoView({ block: 'end' });
  }, [activeSessionId]);

  useEffect(() => {
    if (nearBottom.current) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }
  }, [messages, streamingMessageId]);

  const visible = expanded ? messages : messages.slice(-WINDOW_SIZE);
  const collapsed = messages.length - visible.length;

  return (
    <div
      ref={scrollRef}
      onScroll={onScroll}
      className={`flex-1 overflow-y-auto px-4 sm:px-6 ${messages.length === 0 ? 'flex flex-col py-5' : 'py-5'}`}
    >
      {messages.length === 0 ? (
        <div className="m-auto w-full">
          <EmptyState />
        </div>
      ) : (
        <div className="mx-auto max-w-3xl space-y-5">
          {/* 折叠提示必须是可点的真入口：原文案写着"滚动到顶部自动加载完整历史"，
              但压根没有加载逻辑——超过窗口的更早消息永久不可见，用户被文案骗着
              反复上滑。这里改为显式展开/收起。 */}
          {collapsed > 0 && (
            <button
              type="button"
              onClick={() => setExpanded(true)}
              className="mx-auto block rounded-full border border-rule/60 px-3 py-1 text-center text-[11.5px] text-ink-faint transition hover:border-chalk/50 hover:text-chalk"
            >
              ↑ 展开更早的 {collapsed} 条消息
            </button>
          )}
          {expanded && messages.length > WINDOW_SIZE && (
            <button
              type="button"
              onClick={() => setExpanded(false)}
              className="mx-auto block rounded-full border border-rule/60 px-3 py-1 text-center text-[11.5px] text-ink-faint transition hover:border-chalk/50 hover:text-chalk"
            >
              ↓ 仅显示最近 {WINDOW_SIZE} 条
            </button>
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
