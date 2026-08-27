'use client';

/** 对话内快速上传录音/板书资产：悬浮进度 + 完成后更新考点库 */
import { useRef, useState } from 'react';
import { useSessionStore } from '@/stores/useSessionStore';
import { useEvidenceStore } from '@/stores/useEvidenceStore';
import { uploadAsset } from '@/lib/api';

const STAGES = ['上传素材…', 'ASR 转录 / VLM 识别…', '时空对齐切片…', '教学洞察提炼…', '向量入库…'];

export default function QuickIngestPanel() {
  const audioRef = useRef<HTMLInputElement>(null);
  const boardRef = useRef<HTMLInputElement>(null);
  const [error, setError] = useState('');
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const { assets, ingestProgress, setIngestProgress, registerAsset, setTab } = useEvidenceStore();

  const handleUpload = async (kind: 'audio' | 'board', file: File) => {
    setError('');
    // 模拟分阶段进度（演示模式流水线为同步快速完成）
    let stageIdx = 0;
    const timer = window.setInterval(() => {
      stageIdx = Math.min(stageIdx + 1, STAGES.length - 1);
      setIngestProgress({ filename: file.name, percent: Math.min(95, 18 + stageIdx * 19), stage: STAGES[stageIdx] });
    }, 320);
    setIngestProgress({ filename: file.name, percent: 10, stage: STAGES[0] });

    try {
      const asset = await uploadAsset(activeSessionId, kind, file);
      registerAsset(asset);
      setIngestProgress({ filename: file.name, percent: 100, stage: `完成 · 新增 ${asset.chunkCount} 个考点切片` });
      window.setTimeout(() => setIngestProgress(null), 2600);
    } catch (e) {
      setError(`入库失败：${(e as Error).message}（请确认后端已启动）`);
      setIngestProgress(null);
    } finally {
      window.clearInterval(timer);
    }
  };

  return (
    <div className="space-y-4 px-5 py-4">
      <div className="text-[12.5px] leading-relaxed text-ink-faint">
        直接拖入或选择课堂录音 / 板书照片，系统将自动完成 ASR 转录、VLM 手写公式识别、
        时空对齐切片与考点提炼，完成后即可在本对话中检索引用。
      </div>

      {/* 上传按钮组 */}
      <div className="grid grid-cols-2 gap-2">
        <button
          onClick={() => audioRef.current?.click()}
          className="rounded-lg border border-dashed border-chalk/50 bg-chalk-soft/50 px-4 py-6 text-center transition hover:bg-chalk-soft"
        >
          <div className="text-xl" aria-hidden>🎙️</div>
          <div className="mt-1 text-[13px] font-semibold text-chalk">上传课堂录音</div>
          <div className="mt-0.5 text-[11px] text-ink-faint">wav / mp3 / m4a</div>
        </button>
        <button
          onClick={() => boardRef.current?.click()}
          className="rounded-lg border border-dashed border-warn/50 bg-warn-soft/50 px-4 py-6 text-center transition hover:bg-warn-soft"
        >
          <div className="text-xl" aria-hidden>🖼️</div>
          <div className="mt-1 text-[13px] font-semibold text-warn">上传板书照片</div>
          <div className="mt-0.5 text-[11px] text-ink-faint">png / jpg / heic</div>
        </button>
      </div>

      <input
        ref={audioRef}
        type="file"
        accept="audio/*"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) void handleUpload('audio', f);
          e.target.value = '';
        }}
      />
      <input
        ref={boardRef}
        type="file"
        accept="image/*"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) void handleUpload('board', f);
          e.target.value = '';
        }}
      />

      {/* 悬浮进度条 */}
      {ingestProgress && (
        <div className="rounded-lg border border-rule bg-[#fdfaf2] px-4 py-3">
          <div className="mb-1.5 flex items-center justify-between text-[12.5px]">
            <span className="truncate text-ink-soft">{ingestProgress.filename}</span>
            <span className="text-chalk">{ingestProgress.percent}%</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-paper-deep">
            <div
              className="h-full rounded-full bg-chalk transition-all duration-300"
              style={{ width: `${ingestProgress.percent}%` }}
            />
          </div>
          <div className="mt-1.5 text-[11.5px] text-ink-faint">{ingestProgress.stage}</div>
        </div>
      )}

      {error && (
        <div className="rounded-lg border border-cinnabar/40 bg-cinnabar-soft px-4 py-2.5 text-[12.5px] text-cinnabar">
          {error}
        </div>
      )}

      {/* 资产列表 */}
      <div>
        <div className="mb-2 font-display text-[12.5px] font-bold text-ink">📚 已入库资产</div>
        {assets.length === 0 ? (
          <div className="rounded-lg border border-rule bg-paper-deep/40 px-4 py-3 text-[12px] text-ink-faint">
            暂无新入库资产；系统已预置「10月15日 · 反常积分」课堂切片 4 条。
          </div>
        ) : (
          <ul className="space-y-2">
            {assets.map((a) => (
              <li key={a.id} className="rounded-lg border border-rule bg-[#fdfaf2] px-3.5 py-2.5">
                <div className="flex items-center justify-between text-[12.5px]">
                  <span className="font-medium text-ink">{a.filename}</span>
                  <span className="rounded bg-chalk-soft px-1.5 py-0.5 text-[11px] text-chalk">
                    {a.kind === 'audio' ? '🎙️ 录音' : '🖼️ 板书'}
                  </span>
                </div>
                <div className="mt-1 text-[11.5px] text-ink-faint">
                  新增 {a.chunkCount} 个切片
                  {a.pitfalls.length > 0 && ` · 提炼 ${a.pitfalls.length} 条易错规则`}
                </div>
                {a.pitfalls.length > 0 && (
                  <button
                    className="mt-1 text-[11.5px] text-chalk underline-offset-2 hover:underline"
                    onClick={() => setTab('evidence')}
                  >
                    → 前往证据面板查看
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
