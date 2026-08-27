/** 会话隔离状态：管理 activeSessionId、各会话消息队列（Cherry Studio 式严格隔离） */
import { create } from 'zustand';
import type { PolymorphicMessage } from '@/types/message';
import { db } from '@/db';

export interface SessionMeta {
  id: string;
  title: string;
  createdAt: number;
}

interface SessionState {
  sessions: SessionMeta[];
  activeSessionId: string;
  messagesBySession: Record<string, PolymorphicMessage[]>;
  streamingMessageId: string | null;

  init: () => Promise<void>;
  createSession: (title?: string) => Promise<string>;
  switchSession: (id: string) => void;
  removeSession: (id: string) => Promise<void>;
  renameSession: (id: string, title: string) => Promise<void>;

  appendMessage: (msg: PolymorphicMessage) => void;
  patchMessage: (sessionId: string, id: string, patch: Partial<PolymorphicMessage>) => void;
  appendDelta: (sessionId: string, id: string, text: string) => void;
  appendTrackDelta: (sessionId: string, id: string, index: number, text: string) => void;
  finishTrack: (sessionId: string, id: string, index: number) => void;
  setStreamingId: (id: string | null) => void;
}

export const useSessionStore = create<SessionState>((set, get) => ({
  sessions: [],
  activeSessionId: '',
  messagesBySession: {},
  streamingMessageId: null,

  async init() {
    let sessions = await db.sessions.toArray();
    if (sessions.length === 0) {
      const meta: SessionMeta = {
        id: crypto.randomUUID().slice(0, 12),
        title: '高数 · 反常积分专题',
        createdAt: Date.now(),
      };
      await db.sessions.add(meta);
      sessions = [meta];
    }
    set({
      sessions: [...sessions].sort((a, b) => b.createdAt - a.createdAt),
      activeSessionId: get().activeSessionId || sessions[0].id,
    });
  },

  async createSession(title = '新对话') {
    const meta: SessionMeta = { id: crypto.randomUUID().slice(0, 12), title, createdAt: Date.now() };
    await db.sessions.add(meta);
    set((s) => ({
      sessions: [meta, ...s.sessions],
      messagesBySession: { ...s.messagesBySession, [meta.id]: [] },
      activeSessionId: meta.id,
    }));
    return meta.id;
  },

  switchSession(id) {
    set({ activeSessionId: id });
  },

  async removeSession(id) {
    await db.sessions.delete(id);
    await db.messages.where('sessionId').equals(id).delete();
    const rest = get().sessions.filter((s) => s.id !== id);
    set((s) => {
      const next = { ...s.messagesBySession };
      delete next[id];
      return {
        sessions: rest,
        messagesBySession: next,
        activeSessionId: s.activeSessionId === id ? (rest[0]?.id ?? '') : s.activeSessionId,
      };
    });
    if (get().sessions.length === 0) await get().createSession();
  },

  async renameSession(id, title) {
    await db.sessions.update(id, { title });
    set((s) => ({ sessions: s.sessions.map((m) => (m.id === id ? { ...m, title } : m)) }));
  },

  appendMessage(msg) {
    const sid = msg.sessionId;
    set((s) => ({
      messagesBySession: { ...s.messagesBySession, [sid]: [...(s.messagesBySession[sid] ?? []), msg] },
    }));
    void db.messages.put(msg);
  },

  patchMessage(sessionId, id, patch) {
    set((s) => ({
      messagesBySession: {
        ...s.messagesBySession,
        [sessionId]: (s.messagesBySession[sessionId] ?? []).map((m) => (m.id === id ? { ...m, ...patch } : m)),
      },
    }));
  },

  appendDelta(sessionId, id, text) {
    set((s) => ({
      messagesBySession: {
        ...s.messagesBySession,
        [sessionId]: (s.messagesBySession[sessionId] ?? []).map((m) =>
          m.id === id ? { ...m, content: m.content + text } : m,
        ),
      },
    }));
  },

  appendTrackDelta(sessionId, id, index, text) {
    set((s) => ({
      messagesBySession: {
        ...s.messagesBySession,
        [sessionId]: (s.messagesBySession[sessionId] ?? []).map((m) => {
          if (m.id !== id || !m.comparePayload) return m;
          const tracks = m.comparePayload.tracks.map((t, i) => (i === index ? { ...t, content: t.content + text } : t));
          return { ...m, comparePayload: { tracks } };
        }),
      },
    }));
  },

  finishTrack(sessionId, id, index) {
    set((s) => ({
      messagesBySession: {
        ...s.messagesBySession,
        [sessionId]: (s.messagesBySession[sessionId] ?? []).map((m) => {
          if (m.id !== id || !m.comparePayload) return m;
          const tracks = m.comparePayload.tracks.map((t, i) => (i === index ? { ...t, status: 'done' as const } : t));
          return { ...m, comparePayload: { tracks } };
        }),
      },
    }));
  },

  setStreamingId(id) {
    set({ streamingMessageId: id });
  },
}));
