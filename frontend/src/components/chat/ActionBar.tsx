'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com


/** 对话内嵌入式交互操作条：苏格拉底 / 靶向自测 / 分屏比对 */
import { useSessionStore } from '@/stores/useSessionStore';
import { streamChat } from '@/lib/api';
import type { PolymorphicMessage } from '@/types/message';

export default function ActionBar({ sourceMessage }: { sourceMessage: PolymorphicMessage }) {
  const { activeSessionId, appendMessage, setStreamingId } = useSessionStore();
  const busy = useSessionStore((s) => s.streamingMessageId !== null);

  const trigger = (intent: 'socratic' | 'quiz' | 'compare') => {
    if (busy) return;
    const question =
      sourceMessage.solvePayload?.examPoint ??
      sourceMessage.content.slice(0, 60) ??
      '反常积分敛散性';
    const userMsg: PolymorphicMessage = {
      id: crypto.randomUUID(),
      sessionId: activeSessionId,
      role: 'user',
      type: 'general_text',
      createdAt: Date.now(),
      content:
        intent === 'socratic'
          ? '💡 请用苏格拉底式启发教我'
          : intent === 'quiz'
            ? '⚡ 调取易错点出题考我'
            : '🔀 分屏对比其他模型的解法',
    };
    appendMessage(userMsg);
    setStreamingId('pending');
    void streamChat(
      {
        sessionId: activeSessionId,
        text: intent === 'socratic' ? '教我' : intent === 'quiz' ? '考我' : `对比 ${question}`,
        forceIntent: intent,
      },
      {
        onMeta: () => undefined,
        onDone: () => setStreamingId(null),
        onError: () => setStreamingId(null),
      },
    );
  };

  return (
    <div className="mt-3 flex flex-wrap gap-2 border-t border-rule/70 pt-3" role="toolbar" aria-label="卡片操作">
      <button
        disabled={busy}
        onClick={() => trigger('socratic')}
        className="rounded-md border border-chalk/40 bg-chalk-soft px-3 py-1.5 text-[12.5px] text-chalk transition hover:bg-chalk hover:text-white disabled:opacity-40"
      >
        💡 苏格拉底式启发教我
      </button>
      <button
        disabled={busy}
        onClick={() => trigger('quiz')}
        className="rounded-md border border-warn/40 bg-warn-soft px-3 py-1.5 text-[12.5px] text-warn transition hover:bg-warn hover:text-white disabled:opacity-40"
      >
        ⚡ 调取易错点出题考我
      </button>
      <button
        disabled={busy}
        onClick={() => trigger('compare')}
        className="rounded-md border border-cinnabar/40 bg-cinnabar-soft px-3 py-1.5 text-[12.5px] text-cinnabar transition hover:bg-cinnabar hover:text-white disabled:opacity-40"
      >
        🔀 分屏对比其他模型
      </button>
    </div>
  );
}
