'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com


/** 对话内快速上传录音/板书/文字素材：任务化入库 + 真实阶段进度 + 完成后更新考点库 */
import { useRef, useState } from 'react';
import { FileText, Image as ImageIcon, Mic } from 'lucide-react';
import { useSessionStore } from '@/stores/useSessionStore';
import { useEvidenceStore } from '@/stores/useEvidenceStore';
import { submitIngestTask, pollIngestTask, type IngestMediaType } from '@/lib/api';

/** 任务状态 → 进度条百分比：进度反映的是后端真实阶段（排队/处理/完成），
 * 不是旧版按时间轴演出来的假进度（长 ASR 卡 95% 干等、快任务演完五档）。 */
function statusPercent(status: string): number {
  if (status === 'succeeded') return 100;
  if (status === 'running') return 55;
  if (status === 'queued') return 10;
  return 0;
}

export default function QuickIngestPanel() {
  const audioRef = useRef<HTMLInputElement>(null);
  const boardRef = useRef<HTMLInputElement>(null);
  const [error, setError] = useState('');
  const [showTextBox, setShowTextBox] = useState(false);
  const [noteTitle, setNoteTitle] = useState('');
  const [noteText, setNoteText] = useState('');
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const { assets, ingestProgress, setIngestProgress, registerAsset, setTab } = useEvidenceStore();

  const handleUpload = async (kind: IngestMediaType, file: File) => {
    setError('');
    setIngestProgress({ filename: file.name, percent: 5, stage: '已提交，等待受理…' });
    try {
      // 任务化入库：提交立即返回句柄（毫秒级），随后轮询真实状态。
      // 阶段文案来自后端 _STAGE_HINTS，长任务显示"排队/处理中"是真实
      // 事实而非演出来的动画；僵尸任务由后端收尸，客户端不会无限等待。
      const handle = await submitIngestTask(activeSessionId, kind, file);
      const done = await pollIngestTask(handle.taskId, (status, stage) => {
        setIngestProgress({ filename: file.name, percent: statusPercent(status), stage: stage || status });
      });
      if (!done.asset) throw new Error(done.error || '入库失败');
      registerAsset(done.asset);
      setIngestProgress({ filename: file.name, percent: 100, stage: `完成 · 新增 ${done.asset.chunkCount} 个考点切片` });
      window.dispatchEvent(new CustomEvent('outline:refresh')); // 大纲树实时反映新切片
      window.setTimeout(() => setIngestProgress(null), 2600);
    } catch (e) {
      setError(`入库失败：${(e as Error).message}（请确认后端已启动）`);
      setIngestProgress(null);
    }
  };

  const handleTextSubmit = async () => {
    if (!noteText.trim()) return;
    setError('');
    const f = new File([noteText.trim()], `${noteTitle.trim() || '文字笔记'}.txt`, { type: 'text/plain' });
    setIngestProgress({ filename: f.name, percent: 10, stage: '已提交，等待受理…' });
    try {
      // 文字走同一任务通道（media_type=text 由后端从 .txt 文件解码），
      // 与录音/板书保持同一进度语义。
      const handle = await submitIngestTask(activeSessionId, 'text', f);
      const done = await pollIngestTask(handle.taskId, (status, stage) => {
        setIngestProgress({ filename: f.name, percent: statusPercent(status), stage: stage || status });
      });
      if (!done.asset) throw new Error(done.error || '入库失败');
      registerAsset(done.asset);
      setNoteText('');
      setNoteTitle('');
      setShowTextBox(false);
      setIngestProgress({ filename: done.asset.filename || f.name, percent: 100, stage: `完成 · 新增 ${done.asset.chunkCount} 个知识切片` });
      window.dispatchEvent(new CustomEvent('outline:refresh'));
      window.setTimeout(() => setIngestProgress(null), 2600);
    } catch (e) {
      setError(`文字入库失败：${(e as Error).message}`);
      setIngestProgress(null);
    }
  };

  return (
    <div className="space-y-4 px-5 py-4">
      <div className="text-[12.5px] leading-relaxed text-ink-faint">
        直接拖入或选择课堂录音 / 板书照片 / 粘贴文字讲义，系统将自动完成 ASR 转录、VLM 手写公式识别、
        切片与考点提炼并向量入库，完成后即可在本对话中检索引用。
      </div>

      {/* 上传按钮组 */}
      <div className="grid grid-cols-3 gap-2">
        <button
          onClick={() => audioRef.current?.click()}
          className="rounded-lg border border-dashed border-chalk/50 bg-chalk-soft/50 px-2 py-5 text-center transition hover:bg-chalk-soft"
        >
          <Mic size={18} strokeWidth={1.5} className="mx-auto text-chalk" aria-hidden />
          <div className="mt-1 text-[13px] font-semibold text-chalk">课堂录音</div>
          <div className="mt-0.5 text-[11px] text-ink-faint">wav / mp3</div>
        </button>
        <button
          onClick={() => boardRef.current?.click()}
          className="rounded-lg border border-dashed border-warn/50 bg-warn-soft/50 px-2 py-5 text-center transition hover:bg-warn-soft"
        >
          <ImageIcon size={18} strokeWidth={1.5} className="mx-auto text-warn" aria-hidden />
          <div className="mt-1 text-[13px] font-semibold text-warn">板书照片</div>
          <div className="mt-0.5 text-[11px] text-ink-faint">png / jpg</div>
        </button>
        <button
          onClick={() => setShowTextBox((v) => !v)}
          className={`rounded-lg border border-dashed px-2 py-5 text-center transition ${
            showTextBox ? 'border-cinnabar/60 bg-cinnabar-soft' : 'border-rule bg-paper-deep/40 hover:bg-paper-deep'
          }`}
        >
          <FileText size={18} strokeWidth={1.5} className="mx-auto text-ink-soft" aria-hidden />
          <div className="mt-1 text-[13px] font-semibold text-ink">粘贴文字</div>
          <div className="mt-0.5 text-[11px] text-ink-faint">笔记 / 讲义</div>
        </button>
      </div>

      {/* 文字入库表单 */}
      {showTextBox && (
        <div className="space-y-2 rounded-lg border border-rule bg-white/60 px-4 py-3">
          <input
            value={noteTitle}
            onChange={(e) => setNoteTitle(e.target.value)}
            placeholder="笔记标题（可选，如「10月17日 · 级数敛散性」）"
            className="w-full rounded-md border border-rule bg-white/80 px-2.5 py-1.5 text-[12.5px] outline-none focus:border-chalk"
          />
          <textarea
            value={noteText}
            onChange={(e) => setNoteText(e.target.value)}
            rows={6}
            placeholder="粘贴课堂笔记、讲义或题目解析…支持空行分段，系统按语义自动切片入库。"
            className="w-full resize-y rounded-md border border-rule bg-white/80 px-2.5 py-2 text-[12.5px] leading-relaxed outline-none focus:border-chalk"
          />
          <div className="flex items-center justify-between">
            <span className="text-[11px] text-ink-faint">{noteText.length} 字 · 建议单次 ≤ 8000 字</span>
            <button
              onClick={() => void handleTextSubmit()}
              disabled={!noteText.trim() || !!ingestProgress}
              className="rounded-md bg-chalk px-3.5 py-1.5 text-[12px] font-semibold text-white transition hover:bg-zinc-800 disabled:opacity-50"
            >
              入库
            </button>
          </div>
        </div>
      )}

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
        <div className="rounded-lg border border-rule bg-white px-4 py-3">
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
            暂无新入库资产；上传录音 / 板书 / 文字笔记后，入库结果会显示在这里。
          </div>
        ) : (
          <ul className="space-y-2">
            {assets.map((a) => (
              <li key={a.id} className="rounded-lg border border-rule bg-white px-3.5 py-2.5">
                <div className="flex items-center justify-between text-[12.5px]">
                  <span className="font-medium text-ink">{a.filename}</span>
                  <span className="rounded bg-chalk-soft px-1.5 py-0.5 text-[11px] text-chalk">
                    {a.kind === 'audio' ? '录音' : a.kind === 'text' ? '笔记' : '板书'}
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
