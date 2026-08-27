'use client';

/** 多态消息渲染分发器：按 type 派发到具体卡片组件；统一携带会话双方头像标识 */
import Markdown from './Markdown';
import SolveSolutionCard from './SolveSolutionCard';
import SocraticDialogueCard from './SocraticDialogueCard';
import QuizInteractiveCard from './QuizInteractiveCard';
import MultiModelSplitTrack from './MultiModelSplitTrack';
import type { PolymorphicMessage } from '@/types/message';

export default function MessageCardRenderer({
  message,
  streaming,
}: {
  message: PolymorphicMessage;
  streaming: boolean;
}) {
  // 用户消息：右侧对齐的墨绿气泡 + 「我」头像
  if (message.role === 'user') {
    return (
      <div className="msg-enter flex items-end justify-end gap-2.5">
        <div className="max-w-[76%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-board px-4 py-2.5 text-[14px] leading-relaxed text-paper shadow-[0_4px_16px_-6px_rgba(34,56,47,0.45)]">
          {message.content}
        </div>
        <div
          aria-hidden
          className="font-display flex h-8 w-8 shrink-0 select-none items-center justify-center rounded-full border border-white/20 bg-board text-[12px] font-bold text-[#a8e3d3]"
        >
          我
        </div>
      </div>
    );
  }

  // 助教消息：左侧 🎓 头像 + 多态卡片本体 + 用量小字
  return (
    <div className="msg-enter flex items-start gap-2.5">
      <div
        aria-hidden
        className="sticky top-0 flex h-8 w-8 shrink-0 select-none items-center justify-center rounded-full border border-chalk/30 bg-chalk-soft text-[15px] shadow-sm"
        title="课堂专属助教"
      >
        🎓
      </div>
      <div className="min-w-0 flex-1">
        {renderAssistantCard(message, streaming)}
        <UsageFootnote usage={message.usage} />
      </div>
    </div>
  );
}

/** Cherry Studio 式每轮用量小字：完成后显示在回复末尾 */
function UsageFootnote({ usage }: { usage?: PolymorphicMessage['usage'] }) {
  if (!usage || !Number.isFinite(usage.total) || usage.total <= 0) return null;
  const secs = (usage.duration_ms / 1000).toFixed(1);
  const pct = Math.min(100, (usage.context_used / usage.context_limit) * 100);
  return (
    <div
      className="mt-1.5 flex flex-wrap items-center gap-x-2.5 gap-y-0.5 pl-1 text-[11px] text-ink-faint"
      title={`估算口径 · 上下文已用 ${usage.context_used}/${usage.context_limit} tokens`}
    >
      <span className="inline-flex items-center gap-1">
        ⚡ 本轮
        <b className="font-mono font-semibold text-ink-soft">↑{usage.input}</b>
        <b className="font-mono font-semibold text-ink-soft">↓{usage.output}</b>
        <span className="font-mono">合计 {usage.total} tokens</span>
      </span>
      <span className="inline-flex items-center gap-1">
        🕐 {secs}s
      </span>
      <span className="hidden items-center gap-1.5 sm:inline-flex">
        📊 上下文 {pct.toFixed(1)}%
      </span>
    </div>
  );
}

function renderAssistantCard(message: PolymorphicMessage, streaming: boolean) {
  switch (message.type) {
    case 'solve_card':
      return <SolveSolutionCard message={message} streaming={streaming} />;
    case 'socratic_card':
      return <SocraticDialogueCard message={message} streaming={streaming} />;
    case 'quiz_card':
      return <QuizInteractiveCard message={message} streaming={streaming} />;
    case 'parallel_compare':
      return <MultiModelSplitTrack message={message} streaming={streaming} />;
    default:
      return (
        <div className="paper-card margin-rule msg-enter px-5 py-4">
          <div className="font-display mb-2 flex items-center gap-1.5 text-[12.5px] font-bold tracking-wide text-chalk">
            🤖 课堂专属助教
            {streaming && (
              <span className="inline-flex items-center gap-1 rounded-full bg-chalk-soft px-1.5 py-0.5 text-[10px] font-medium">
                <span className="h-1 w-1 animate-ping rounded-full bg-chalk" />
                正在作答
              </span>
            )}
          </div>
          <Markdown text={message.content} className={streaming ? 'stream-cursor' : ''} />
        </div>
      );
  }
}
