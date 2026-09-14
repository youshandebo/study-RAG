'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com


/** 苏格拉底交互卡片：阶段徽标 + 提示阶梯 + 渐进线索 + 收敛自测（结构化作答） */
import { useEffect, useState } from 'react';
import Markdown from './Markdown';
import { useSessionStore } from '@/stores/useSessionStore';
import { useTutorStore } from '@/stores/useTutorStore';
import { streamChat } from '@/lib/api';
import type { PolymorphicMessage } from '@/types/message';

/** 阶段徽标文案与配色（阶段值由服务端 FSM 下发，前端只做呈现） */
const PHASE_META: Record<string, { label: string; cls: string }> = {
  diagnosing: { label: '🔍 诊断中', cls: 'bg-paper-deep text-ink-soft' },
  guiding: { label: '🧭 引导点拨', cls: 'bg-chalk text-white' },
  reflecting: { label: '🤔 反思中', cls: 'bg-warn-soft text-warn' },
  converging: { label: '🎯 概念自测', cls: 'bg-warn text-white' },
  resolved: { label: '✅ 已掌握', cls: 'bg-chalk-soft text-chalk' },
  revealed: { label: '📖 答案揭晓', cls: 'bg-cinnabar-soft text-cinnabar' },
};

/** 提示阶梯条：Level 1 概念点拨 / 2 局部残卷 / 3 关键分支 */
function HintLadder({ level, max }: { level: number; max: number }) {
  const total = Math.max(1, max);
  return (
    <span className="flex items-center gap-1" title={`提示阶梯 ${level}/${total}（越高提示越具体）`}>
      {Array.from({ length: total }).map((_, i) => (
        <span
          key={i}
          className={`h-1.5 w-5 rounded-full ${i < level ? 'bg-chalk' : 'bg-white/40'}`}
        />
      ))}
      <span className="ml-0.5 text-[10.5px] font-medium opacity-90">L{level}</span>
    </span>
  );
}

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
  }, [payload?.currentStepIndex]); // eslint-disable-line react-hooks/exhaustive-deps

  const send = (text: string) => {
    if (!text.trim() || busy) return;
    const userMsg: PolymorphicMessage = {
      id: crypto.randomUUID(),
      sessionId: activeSessionId,
      role: 'user',
      type: 'general_text',
      createdAt: Date.now(),
      content: text.trim(),
    };
    appendMessage(userMsg);
    setReply('');
    setStreamingId('pending');
    void streamChat(
      { sessionId: activeSessionId, text: text.trim(), forceIntent: 'socratic' },
      { onDone: () => setStreamingId(null), onError: () => setStreamingId(null) },
    );
  };

  const phase = payload?.phase ?? '';
  const meta = PHASE_META[phase];
  const selftest = payload?.selftest ?? null;
  const hasOptions = !!selftest?.options?.length;
  const terminal = phase === 'resolved' || phase === 'revealed';

  return (
    <div className="paper-card msg-enter border-chalk/40 bg-[#f4f7f4] px-5 py-4">
      {/* 阶段徽标 + 提示阶梯 / 旧版进度轨道（无 phase 字段时回退） */}
      {payload && (
        <div className="mb-3 flex flex-wrap items-center gap-3">
          {meta ? (
            <span className={`inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 font-display text-[12px] font-bold ${meta.cls}`}>
              {meta.label}
            </span>
          ) : (
            <span className="rounded-md bg-chalk px-2.5 py-1 font-display text-[12px] font-bold text-white">
              💡 苏格拉底伴学
            </span>
          )}
          {phase === 'guiding' && (
            <HintLadder level={payload.hintLevel ?? 0} max={payload.maxHintLevel ?? 3} />
          )}
          <span className="ml-auto text-[11.5px] text-ink-faint">
            {meta ? payload.phaseLabel : `第 ${Math.min(payload.currentStepIndex + 1, payload.totalSteps)} / ${payload.totalSteps} 步`}
          </span>
        </div>
      )}

      {/* 逃逸拦截：温和提示，不训斥 */}
      {payload?.guardBlocked && (
        <div className="mb-2.5 flex items-start gap-2 rounded-lg border border-warn/40 bg-warn-soft/50 px-3 py-2 text-[12.5px] text-ink-soft">
          <span aria-hidden>🛡️</span>
          <span>已拦下"直接要答案"的请求——咱们把这一步再拆小一点，你自己推出来会更牢。</span>
        </div>
      )}

      {/* 引导提问 / 自测题干 */}
      <Markdown text={message.content} className={streaming ? 'stream-cursor' : ''} />

      {/* 渐进线索 */}
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

      {/* 收敛自测：单选选项点即提交（判分确定性命中率远高于手敲长句） */}
      {!streaming && !terminal && hasOptions && (
        <div className="mt-3 space-y-2" role="radiogroup" aria-label="自测选项">
          {selftest!.options.map((opt, i) => (
            <button
              key={i}
              role="radio"
              aria-checked={false}
              disabled={busy}
              onClick={() => send(opt)}
              className="w-full rounded-lg border border-rule bg-white px-4 py-2.5 text-left text-[13.5px] transition hover:border-warn hover:bg-warn-soft/50 disabled:opacity-50"
            >
              <Markdown text={opt} />
            </button>
          ))}
        </div>
      )}

      {/* 填空 / 普通引导作答 */}
      {!streaming && !terminal && !hasOptions && (
        <div className="mt-3 flex gap-2">
          <input
            value={reply}
            onChange={(e) => setReply(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && send(reply)}
            placeholder={phase === 'converging' ? '写下你的答案（判分会按数学等价判定）…' : '用你自己的话回答，助教只做引导不给答案…'}
            className="input-scroll flex-1 rounded-lg px-3.5 py-2 text-sm outline-none"
          />
          <button
            onClick={() => send(reply)}
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
