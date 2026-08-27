/** 多模型分屏流状态：维护每次比对会话中各模型的 SSE 缓冲轨道 */
import { create } from 'zustand';

export interface TrackBuffer {
  modelName: string;
  content: string;
  status: 'streaming' | 'done';
  charsPerSec: number;
}

interface TrackState {
  /** compareRunId -> tracks */
  runs: Record<string, TrackBuffer[]>;
  activeRunId: string | null;

  startRun: (runId: string, names: string[]) => void;
  pushDelta: (runId: string, index: number, text: string) => void;
  markDone: (runId: string, index: number) => void;
  endRun: (runId: string) => void;
  clearRun: (runId: string) => void;
}

export const useTrackStore = create<TrackState>((set) => ({
  runs: {},
  activeRunId: null,

  startRun(runId, names) {
    set((s) => ({
      runs: {
        ...s.runs,
        [runId]: names.map((n) => ({ modelName: n, content: '', status: 'streaming', charsPerSec: 0 })),
      },
      activeRunId: runId,
    }));
  },

  pushDelta(runId, index, text) {
    set((s) => {
      const tracks = s.runs[runId];
      if (!tracks) return s;
      return {
        runs: {
          ...s.runs,
          [runId]: tracks.map((t, i) => (i === index ? { ...t, content: t.content + text } : t)),
        },
      };
    });
  },

  markDone(runId, index) {
    set((s) => {
      const tracks = s.runs[runId];
      if (!tracks) return s;
      return {
        runs: { ...s.runs, [runId]: tracks.map((t, i) => (i === index ? { ...t, status: 'done' } : t)) },
      };
    });
  },

  endRun(runId) {
    set((s) => ({ activeRunId: s.activeRunId === runId ? null : s.activeRunId }));
  },

  clearRun(runId) {
    set((s) => {
      const next = { ...s.runs };
      delete next[runId];
      return { runs: next };
    });
  },
}));
