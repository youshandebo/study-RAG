// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
/** 会话隔离状态：管理 activeSessionId、各会话消息队列（Cherry Studio 式严格隔离） */
import { create } from 'zustand';
import type { PolymorphicMessage } from '@/types/message';
import { db } from '@/db';

export interface SessionMeta {
  id: string;
  title: string;
  createdAt: number;
  /** 课程作用域：检索强隔离 + 侧栏分组 + 顶栏标签 */
  subject?: string;   // 学科：数学 / 物理 / …
  courseId?: string;  // 课程唯一标识（空=全库检索）
  chapter?: string;
  /** 检索模式：lecture=随堂(时间衰减) / review=备考(纯语义跨月) */
  retrievalMode?: 'lecture' | 'review';
}

interface SessionState {
  sessions: SessionMeta[];
  activeSessionId: string;
  messagesBySession: Record<string, PolymorphicMessage[]>;
  streamingMessageId: string | null;
  /** 当前流式请求控制器：切换会话 / 停止生成时统一中断，防竞态 */
  activeAbort: AbortController | null;
  registerAbort: (controller: AbortController | null) => void;
  abortActive: () => void;

  init: () => Promise<void>;
  createSession: (title?: string) => Promise<string>;
  switchSession: (id: string) => void;
  updateSessionMeta: (id: string, patch: Partial<SessionMeta>) => void;
  removeSession: (id: string) => Promise<void>;
  renameSession: (id: string, title: string) => Promise<void>;

  appendMessage: (msg: PolymorphicMessage) => void;
  patchMessage: (sessionId: string, id: string, patch: Partial<PolymorphicMessage>, legacyId?: string) => void;
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
  activeAbort: null,

  registerAbort(controller) {
    set({ activeAbort: controller });
  },

  abortActive() {
    const controller = get().activeAbort;
    if (controller) {
      controller.abort();
      set({ activeAbort: null, streamingMessageId: null });
    }
  },

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
    if (id !== get().activeSessionId) get().abortActive(); // 离开会话即中断该会话流
    set({ activeSessionId: id });
  },

  /** 更新会话的课程绑定 / 检索模式 */
  updateSessionMeta(id, patch: Partial<SessionMeta>) {
    set((s) => ({ sessions: s.sessions.map((m) => (m.id === id ? { ...m, ...patch } : m)) }));
    void db.sessions.update(id, patch);
  },

  async removeSession(id) {
    get().abortActive();
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

  patchMessage(sessionId, id, patch, legacyId) {
    set((s) => ({
      messagesBySession: {
        ...s.messagesBySession,
        [sessionId]: (s.messagesBySession[sessionId] ?? []).map((m) =>
          m.id === id || m.id === legacyId ? { ...m, ...patch } : m,
        ),
      },
    }));
    // 持久化最终态：SSE meta 事件可能已把内存 id 从占位键改名，IndexedDB 中的旧行要一并清理
    const finalMsg = get().messagesBySession[sessionId]?.find((m) => m.id === id);
    if (!finalMsg) return;
    const persist = async () => {
      if (legacyId && legacyId !== id) await db.messages.delete(legacyId);
      await db.messages.put(finalMsg);
    };
    void persist();
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
