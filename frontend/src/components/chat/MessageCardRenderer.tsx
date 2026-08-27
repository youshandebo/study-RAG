'use client';

/** 多态消息渲染分发器：按 type 派发到具体卡片组件 */
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
  // 用户消息：右侧对齐的墨绿气泡
  if (message.role === 'user') {
    return (
      <div className="msg-enter flex justify-end">
        <div className="max-w-[78%] whitespace-pre-wrap rounded-2xl rounded-br-sm bg-board px-4 py-2.5 text-[14px] leading-relaxed text-paper">
          {message.content}
        </div>
      </div>
    );
  }

  // 助教消息：按多态类型分发
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
          <div className="mb-1.5 font-display text-[12.5px] font-bold text-chalk">🤖 课堂专属助教</div>
          <Markdown text={message.content} className={streaming ? 'stream-cursor' : ''} />
        </div>
      );
  }
}
