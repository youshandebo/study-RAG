'use client';

/** 音画证据同步器：录音波形高亮播放 + 板书原图定位 + 原文转录 */
import { useEffect, useRef, useState } from 'react';
import { useEvidenceStore } from '@/stores/useEvidenceStore';
import { API_ORIGIN } from '@/lib/api';
import Markdown from '@/components/chat/Markdown';

/** 演示音频合成：以 WebAudio 生成轻柔提示音占位原声（真实部署替换为切片音频流） */
function useTonePlayer() {
  const ctxRef = useRef<AudioContext | null>(null);
  const [playing, setPlaying] = useState(false);
  const [progress, setProgress] = useState(0);

  const play = (durationSec: number) => {
    if (!ctxRef.current) {
      ctxRef.current = new (window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)();
    }
    const ctx = ctxRef.current;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.frequency.value = 440;
    osc.type = 'sine';
    gain.gain.value = 0.02;
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    setPlaying(true);
    const startedAt = performance.now();
    const timer = window.setInterval(() => {
      const p = Math.min(1, (performance.now() - startedAt) / (durationSec * 1000));
      setProgress(p);
      if (p >= 1) {
        window.clearInterval(timer);
        osc.stop();
        setPlaying(false);
      }
    }, 100);
  };

  return { playing, progress, play, reset: () => setProgress(0) };
}

export default function EvidenceViewer() {
  const evidence = useEvidenceStore((s) => s.activeEvidence);
  const history = useEvidenceStore((s) => s.evidenceHistory);
  const { playing, progress, play, reset } = useTonePlayer();

  useEffect(() => {
    // 切换证据时重置播放态
    reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [evidence?.chunkId]);

  if (!evidence) {
    return (
      <div className="px-5 py-10 text-center text-[13px] leading-relaxed text-ink-faint">
        点击解题卡片底部的 🔊 录音 / 🖼️ 板书标签，
        <br />
        此处将同步播放课堂原声并定位板书原图。
      </div>
    );
  }

  const { audio, board } = evidence;
  const durationSec = 15; // 演示片段时长
  const playedBars = Math.floor(audio.waveform.length * progress);

  return (
    <div className="space-y-4 px-5 py-4">
      {/* 时间戳头 */}
      <div className="flex items-center gap-2">
        <span className="rounded-md bg-chalk px-2 py-0.5 font-display text-[12px] font-bold text-white">
          {audio.timestampRange[0]} – {audio.timestampRange[1]}
        </span>
        <span className="text-[11.5px] text-ink-faint">{audio.audioId}</span>
      </div>

      {/* 波形播放器 */}
      <div className="rounded-xl border border-rule bg-[#fdfaf2] p-4">
        <div
          className="flex h-16 items-center gap-[2px]"
          role="meter"
          aria-label="播放进度"
          aria-valuenow={Math.round(progress * 100)}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          {audio.waveform.map((amp, i) => (
            <span
              key={i}
              className={`waveform-bar w-[3px] ${i < playedBars && playing ? 'played' : ''}`}
              style={{ height: `${Math.max(8, amp * 0.55)}px` }}
            />
          ))}
        </div>
        <div className="mt-3 flex items-center gap-3">
          <button
            onClick={() => play(durationSec)}
            disabled={playing}
            className="rounded-lg bg-chalk px-3.5 py-1.5 text-[13px] font-semibold text-white transition hover:opacity-90 disabled:opacity-40"
          >
            {playing ? '▶ 播放中…' : '▶ 播放原声片段'}
          </button>
          <span className="text-[11.5px] text-ink-faint">
            考点：{evidence.examPoint || '—'}
          </span>
        </div>
      </div>

      {/* 原文转录 */}
      <div>
        <div className="mb-1.5 font-display text-[12.5px] font-bold text-ink">🎙️ 原声转录</div>
        <div className="rounded-lg border border-rule bg-paper-deep/40 px-4 py-3 text-[13px] leading-relaxed text-ink-soft">
          <Markdown text={audio.transcript} />
        </div>
      </div>

      {/* 板书原图 */}
      <div>
        <div className="mb-1.5 font-display text-[12.5px] font-bold text-ink">🖼️ 板书定位</div>
        <figure className="overflow-hidden rounded-lg border border-rule bg-board">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={board.url.startsWith('http') ? board.url : `${API_ORIGIN}${board.url}`}
            alt={board.caption}
            className="w-full"
            loading="lazy"
          />
          <figcaption className="bg-board px-3 py-2 text-[12px] text-paper/85">{board.caption}</figcaption>
        </figure>
      </div>

      {/* 历史证据 */}
      {history.length > 1 && (
        <div>
          <div className="mb-1.5 font-display text-[12.5px] font-bold text-ink">🗂️ 最近查看</div>
          <div className="flex flex-wrap gap-1.5">
            {history.slice(1).map((h) => (
              <span key={h.chunkId} className="rounded-full border border-rule px-2.5 py-1 text-[11.5px] text-ink-faint">
                {h.audio.timestampRange[0]} · {h.board.caption}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
