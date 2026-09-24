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
  /** 检索模式：lecture=随堂 / review_narrow=周测 / review_broad=期末 / explore=拓展解法 */
  retrievalMode?: 'lecture' | 'review_narrow' | 'review_broad' | 'review' | 'explore';
  /** 时间偏好强度覆盖（0~1；不设=跟随模式默认值） */
  timeAlphaOverride?: number | null;
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
  /** 移除占位消息：429/402 等"请求未开始"的失败不应在对话里留气泡 */
  removeMessage: (sessionId: string, id: string) => void;
  setStreamingId: (id: string | null) => void;
  /** 大纲树点考点 → 注入输入框的待填提问（OmniChatInput 消费后清空） */
  pendingPrompt: string | null;
  setPendingPrompt: (text: string | null) => void;
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

  /** 更新会话的课程绑定 / 检索模式：本地 Dexie + 服务端（Postgres）双写 */
  updateSessionMeta(id, patch: Partial<SessionMeta>) {
    set((s) => ({ sessions: s.sessions.map((m) => (m.id === id ? { ...m, ...patch } : m)) }));
    void db.sessions.update(id, patch);
    // 只发**变更过的**字段：后端是 model_dump(exclude_none=False) 全量覆盖，
    // 把未变更字段也以 null 发过去会把服务端的课程绑定/检索模式一并清空
    // ——改个章节就丢了课程作用域（前端侧栏还显示着旧绑定，两边不一致，
    // 检索强隔离随之失效）。显式传 null 仍然表示"解除绑定"，语义完整。
    const body: Record<string, unknown> = {};
    if ('subject' in patch) body.subject = patch.subject ?? null;
    if ('courseId' in patch) body.course_id = patch.courseId ?? null;
    if ('chapter' in patch) body.chapter = patch.chapter ?? null;
    if ('retrievalMode' in patch) body.retrieval_mode = patch.retrievalMode ?? null;
    if ('timeAlphaOverride' in patch) body.time_alpha_override = patch.timeAlphaOverride ?? null;
    if (Object.keys(body).length === 0) return;
    void fetch(`${(process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000/api/v1')}/sessions/${id}/meta`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).catch(() => undefined);
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

  removeMessage(sessionId, id) {
    // 占位气泡同步清理 IndexedDB——否则刷新后空占位会复活
    void db.messages.delete(id);
    set((s) => ({
      messagesBySession: {
        ...s.messagesBySession,
        [sessionId]: (s.messagesBySession[sessionId] ?? []).filter((m) => m.id !== id),
      },
    }));
  },

  setStreamingId(id) {
    set({ streamingMessageId: id });
  },

  pendingPrompt: null,
  setPendingPrompt(text) {
    set({ pendingPrompt: text });
  },
}));
