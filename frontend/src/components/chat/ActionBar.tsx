'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com


/** 对话内嵌入式交互操作条：苏格拉底 / 靶向自测 / 分屏比对 */
import { useSessionStore } from '@/stores/useSessionStore';
import { ApiError, streamChat } from '@/lib/api';
import type { PolymorphicMessage } from '@/types/message';

export default function ActionBar({ sourceMessage }: { sourceMessage: PolymorphicMessage }) {
  const { activeSessionId, appendMessage, patchMessage, appendDelta, setStreamingId, registerAbort, removeMessage, setSoftNotice } =
    useSessionStore();
  const busy = useSessionStore((s) => s.streamingMessageId !== null);

  const trigger = (intent: 'socratic' | 'quiz' | 'compare') => {
    if (busy) return;
    const question =
      sourceMessage.solvePayload?.examPoint ??
      sourceMessage.content.slice(0, 60);
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

    // 助手占位气泡：流式回答必须落到这里。原实现只传了 onMeta/onDone/onError，
    // 回答内容（delta 与最终卡片）被完全丢弃——用户点了"苏格拉底式启发教我"
    // 之后只多出一条自己的气泡，永远等不到回复，且失败时也没有任何提示。
    const pendingId = crypto.randomUUID();
    appendMessage({
      id: pendingId,
      sessionId: activeSessionId,
      role: 'assistant',
      type: 'general_text',
      createdAt: Date.now(),
      content: '',
    });
    setStreamingId(pendingId);

    // 注册 abort：快捷操作与输入框共用同一套"停止生成"入口
    const controller = new AbortController();
    registerAbort(controller);

    void streamChat(
      {
        sessionId: activeSessionId,
        text: intent === 'socratic' ? '教我' : intent === 'quiz' ? '考我' : `对比 ${question}`,
        forceIntent: intent,
      },
      {
        onDelta: (piece) => appendDelta(activeSessionId, pendingId, piece),
        onCard: (card) =>
          // 与输入框同一契约：流式期间沿用 pendingId，最终卡片带服务端 id 落定，
          // legacyId 负责清掉 IndexedDB 里的占位旧行
          patchMessage(
            activeSessionId,
            card.id,
            {
              id: card.id,
              type: card.type,
              content: card.content,
              solvePayload: card.solvePayload,
              socraticPayload: card.socraticPayload,
              quizPayload: card.quizPayload,
              comparePayload: card.comparePayload,
              examReportPayload: card.examReportPayload,
              intent: card.intent,
              usage: card.usage,
            },
            pendingId,
          ),
        onDone: () => {
          if (useSessionStore.getState().activeSessionId === activeSessionId) setStreamingId(null);
        },
        onError: (err) => {
          // 与输入框同一反馈路径：429/402 意味着请求压根没开始，占位气泡
          // 不是对话内容——移除气泡，改在输入框下方显示柔性提示（store 共享，
          // 限流附 Retry-After 倒计时）。旧实现一律写 ⚠️ 进气泡，用户既看
          // 不到"何时能再问"，气泡里还留着一条误导性的"对话记录"。
          if (err instanceof ApiError && (err.isRateLimit || err.isQuotaExceeded)) {
            removeMessage(activeSessionId, pendingId);
            setSoftNotice(
              err.isRateLimit
                ? { kind: 'rate', message: err.message, countdown: err.retryAfter ?? 15 }
                : { kind: 'quota', message: err.message },
            );
          } else {
            patchMessage(activeSessionId, pendingId, {
              content: `⚠️ ${err.message || '请求失败，请稍后重试'}`,
            });
          }
          if (useSessionStore.getState().activeSessionId === activeSessionId) setStreamingId(null);
        },
      },
      controller.signal,
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
