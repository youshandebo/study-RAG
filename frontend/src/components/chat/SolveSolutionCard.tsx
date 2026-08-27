'use client';

/** 拍照解题卡片：考点徽章 + 难度星级 + 老师原法推导 + 易错预警 + 证据链 + 操作条 */
import Markdown from './Markdown';
import PitfallAlertCard from './PitfallAlertCard';
import ActionBar from './ActionBar';
import { useEvidenceStore } from '@/stores/useEvidenceStore';
import { fetchEvidence } from '@/lib/api';
import type { PolymorphicMessage } from '@/types/message';

function EvidenceChip({
  label,
  icon,
  onClick,
}: {
  label: string;
  icon: string;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className="inline-flex items-center gap-1.5 rounded-full border border-chalk/35 bg-chalk-soft px-3 py-1 text-[12px] text-chalk transition hover:bg-chalk hover:text-white"
    >
      <span aria-hidden>{icon}</span>
      {label}
    </button>
  );
}

export default function SolveSolutionCard({
  message,
  streaming,
}: {
  message: PolymorphicMessage;
  streaming: boolean;
}) {
  const payload = message.solvePayload;
  const activateEvidence = useEvidenceStore((s) => s.activateEvidence);

  const openEvidence = async (snippetUrl: string) => {
    const bundle = await fetchEvidence(snippetUrl);
    if (bundle) activateEvidence(bundle);
  };

  return (
    <div className="paper-card margin-rule msg-enter px-5 py-4">
      {/* 头部徽章行 */}
      {payload && (
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <span className="badge-point rounded-md px-2.5 py-1 font-display text-[12.5px] font-bold">
            🏷️ 核心考点 · {payload.examPoint}
          </span>
          <span className="badge-diff text-[13px]" aria-label={`难度 ${payload.difficulty} 星`}>
            {'⭐'.repeat(payload.difficulty)}
            <span className="ml-1 text-[11px] text-ink-faint">复杂度 {payload.difficulty}/5</span>
          </span>
        </div>
      )}

      {/* 老师原法推导正文 */}
      <div className="mb-1 font-display text-[13.5px] font-bold text-ink">📝 老师原法推导</div>
      <Markdown text={message.content} className={streaming ? 'stream-cursor' : ''} />

      {/* 步骤概览 */}
      {payload?.steps?.length ? (
        <details className="mt-3 rounded-lg border border-rule bg-paper-deep/50 px-4 py-2.5" open={!streaming}>
          <summary className="cursor-pointer select-none text-[12.5px] font-semibold text-ink-soft">
            解题步骤拆解（按老师课堂风格）
          </summary>
          <ol className="mt-2 space-y-1.5">
            {payload.steps.map((step, i) => (
              <li key={i} className="flex gap-2 text-[13px] text-ink-soft">
                <span className="mt-[1px] flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-chalk text-[11px] font-bold text-white">
                  {i + 1}
                </span>
                <Markdown text={step} />
              </li>
            ))}
          </ol>
        </details>
      ) : null}

      {/* 易错预警 */}
      {payload?.pitfalls?.length ? <PitfallAlertCard pitfalls={payload.pitfalls} /> : null}

      {/* 证据链条 */}
      {payload?.evidenceList?.length ? (
        <div className="mt-3">
          <div className="mb-1.5 font-display text-[12.5px] font-bold text-chalk">🔍 课堂证据链条</div>
          <div className="flex flex-wrap gap-2">
            {payload.evidenceList.map((ev, i) => (
              <span key={i} className="inline-flex gap-1.5">
                <EvidenceChip
                  icon="🔊"
                  label={`录音 ${ev.timestampRange[0]} 原声`}
                  onClick={() => openEvidence(ev.audioSnippetUrl)}
                />
                <EvidenceChip
                  icon="🖼️"
                  label={ev.boardCaption || '板书原图'}
                  onClick={() => openEvidence(ev.audioSnippetUrl)}
                />
              </span>
            ))}
          </div>
        </div>
      ) : null}

      {!streaming && <ActionBar sourceMessage={message} />}
    </div>
  );
}
