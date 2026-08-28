'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com


/** 苏格拉底交互卡片：引导问答、渐进提示、作答框、进度轨道 */
import { useEffect, useState } from 'react';
import Markdown from './Markdown';
import { useSessionStore } from '@/stores/useSessionStore';
import { useTutorStore } from '@/stores/useTutorStore';
import { streamChat } from '@/lib/api';
import type { PolymorphicMessage } from '@/types/message';

export default function SocraticDialogueCard({
  message,
  streaming,
}: {
  message: PolymorphicMessage;
  streaming: boolean;
}) {
  const payload = message.socraticPayload;
  const [showHints, setShowHints] = useState(false);
  const [reply, setReply] = useState('');
  const { activeSessionId, appendMessage, setStreamingId } = useSessionStore();
  const busy = useSessionStore((s) => s.streamingMessageId !== null);
  const sync = useTutorStore((s) => s.sync);

  useEffect(() => {
    if (payload) sync(activeSessionId, payload);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [payload?.currentStepIndex]);

  const submit = () => {
    if (!reply.trim() || busy) return;
    const userMsg: PolymorphicMessage = {
      id: crypto.randomUUID(),
      sessionId: activeSessionId,
      role: 'user',
      type: 'general_text',
      createdAt: Date.now(),
      content: reply.trim(),
    };
    appendMessage(userMsg);
    setReply('');
    setStreamingId('pending');
    void streamChat(
      { sessionId: activeSessionId, text: reply.trim(), forceIntent: 'socratic' },
      { onDone: () => setStreamingId(null), onError: () => setStreamingId(null) },
    );
  };

  return (
    <div className="paper-card msg-enter border-chalk/40 bg-[#f4f7f4] px-5 py-4">
      {/* 进度轨道 */}
      {payload && (
        <div className="mb-3 flex items-center gap-3">
          <span className="rounded-md bg-chalk px-2.5 py-1 font-display text-[12px] font-bold text-white">
            💡 苏格拉底伴学
          </span>
          <div className="flex flex-1 items-center gap-1" aria-label={`引导进度 ${payload.currentStepIndex}/${payload.totalSteps}`}>
            {Array.from({ length: payload.totalSteps }).map((_, i) => (
              <span
                key={i}
                className={`h-1.5 flex-1 rounded-full ${
                  i < payload.currentStepIndex ? 'bg-chalk' : i === payload.currentStepIndex ? 'bg-chalk/60' : 'bg-rule'
                }`}
              />
            ))}
          </div>
          <span className="text-[11.5px] text-ink-faint">
            第 {Math.min(payload.currentStepIndex + 1, payload.totalSteps)} / {payload.totalSteps} 步
          </span>
        </div>
      )}

      {/* 引导提问 */}
      <Markdown text={message.content} className={streaming ? 'stream-cursor' : ''} />

      {/* 渐进提示 */}
      {payload?.hints?.length ? (
        <div className="mt-2">
          <button
            onClick={() => setShowHints((v) => !v)}
            className="text-[12.5px] text-chalk underline-offset-2 hover:underline"
          >
            {showHints ? '收起线索提示' : `展开 ${payload.hints.length} 条渐进线索提示`}
          </button>
          {showHints && (
            <ul className="mt-2 space-y-1.5 rounded-lg border border-dashed border-chalk/40 bg-chalk-soft/60 px-4 py-3">
              {payload.hints.map((h, i) => (
                <li key={i} className="text-[13px] text-ink-soft">
                  <Markdown text={h} />
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}

      {/* 作答框 */}
      {!streaming && payload && payload.currentStepIndex < payload.totalSteps && (
        <div className="mt-3 flex gap-2">
          <input
            value={reply}
            onChange={(e) => setReply(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && submit()}
            placeholder="用你自己的话回答，助教只做引导不给答案…"
            className="input-scroll flex-1 rounded-lg px-3.5 py-2 text-sm outline-none"
          />
          <button
            onClick={submit}
            disabled={busy || !reply.trim()}
            className="rounded-lg bg-chalk px-4 py-2 text-sm font-semibold text-white transition hover:opacity-90 disabled:opacity-40"
          >
            回答
          </button>
        </div>
      )}
    </div>
  );
}
