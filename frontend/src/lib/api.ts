// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
/** Fetch / SSE 流式通信客户端封装 */

import type { EvidenceRef, Intent, PolymorphicMessage, UsageInfo } from '@/types/message';
import type { EvidenceBundle, IngestAsset } from '@/types/evidence';

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000/api/v1';
/** 后端源（剥离 /api/v1）：静态板书等资源直连后端 */
export const API_ORIGIN = API_BASE.replace(/\/api\/v1\/?$/, '');

export interface StreamHandlers {
  onMeta?: (meta: { messageId: string; intent: Intent }) => void;
  onDelta?: (text: string) => void;
  onEvidence?: (list: EvidenceRef[]) => void;
  onUsage?: (usage: UsageInfo) => void;
  onTrackDelta?: (index: number, modelName: string, text: string) => void;
  onTrackDone?: (index: number, modelName: string) => void;
  onCard?: (card: PolymorphicMessage) => void;
  onDone?: () => void;
  onError?: (err: Error) => void;
}

/** 解析单条 SSE 帧 */
function parseSSEChunk(chunk: string): { event: string; data: string } | null {
  const lines = chunk.split('\n');
  let event = 'message';
  let data = '';
  for (const line of lines) {
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) data += line.slice(5).trim();
  }
  return data ? { event, data } : null;
}

async function consumeSSE(resp: Response, handlers: StreamHandlers): Promise<void> {
  const reader = resp.body?.getReader();
  if (!reader) throw new Error('响应无内容流');
  const decoder = new TextDecoder();
  let buffer = '';

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split('\n\n');
    buffer = frames.pop() ?? '';
    for (const frame of frames) {
      const parsed = parseSSEChunk(frame);
      if (!parsed) continue;
      let payload: unknown = {};
      try {
        payload = JSON.parse(parsed.data);
      } catch {
        continue;
      }
      dispatch(parsed.event, payload, handlers);
    }
  }
  handlers.onDone?.();
}

/** 后端 pydantic 序列化为 snake_case，前端协议为 camelCase —— 在分发层统一转换 */
function normalizeEvidence(e: Record<string, unknown>): EvidenceRef {
  return {
    audioId: String(e.audio_id ?? ''),
    timestampRange: (e.timestamp_range as [string, string]) ?? ['00:00', '00:00'],
    audioSnippetUrl: String(e.audio_snippet_url ?? ''),
    boardImageUrl: String(e.board_image_url ?? ''),
    transcriptSnippet: String(e.transcript_snippet ?? ''),
    boardCaption: String(e.board_caption ?? ''),
  };
}

export function normalizeCard(raw: Record<string, unknown>): PolymorphicMessage {
  const msg: Record<string, unknown> = { ...raw };
  msg.sessionId = raw.session_id ?? '';
  msg.createdAt = raw.created_at ?? Date.now();
  if (raw.usage && typeof raw.usage === 'object') msg.usage = raw.usage; // 键名前后端一致，原样透传

  const sp = raw.solve_payload as Record<string, unknown> | undefined | null;
  if (sp) {
    msg.solvePayload = {
      examPoint: String(sp.exam_point ?? ''),
      difficulty: Number(sp.difficulty ?? 3) as 1 | 2 | 3 | 4 | 5,
      pitfalls: (sp.pitfalls as string[]) ?? [],
      steps: (sp.steps as string[]) ?? [],
      evidenceList: ((sp.evidence_list as Record<string, unknown>[]) ?? []).map(normalizeEvidence),
    };
  }
  const so = raw.socratic_payload as Record<string, unknown> | undefined | null;
  if (so) {
    msg.socraticPayload = {
      currentStepIndex: Number(so.current_step_index ?? 0),
      totalSteps: Number(so.total_steps ?? 4),
      guidingQuestion: String(so.guiding_question ?? ''),
      hints: (so.hints as string[]) ?? [],
    };
  }
  const qz = raw.quiz_payload as Record<string, unknown> | undefined | null;
  if (qz) {
    msg.quizPayload = {
      questionText: String(qz.question_text ?? ''),
      options: (qz.options as string[]) ?? undefined,
      targetPitfall: String(qz.target_pitfall ?? ''),
      explanation: String(qz.explanation ?? ''),
    };
  }
  const cp = raw.compare_payload as Record<string, unknown> | undefined | null;
  if (cp) {
    const tracks = (cp.tracks as Record<string, unknown>[]) ?? [];
    msg.comparePayload = {
      tracks: tracks.map((tr) => ({
        modelName: String(tr.model_name ?? ''),
        content: String(tr.content ?? ''),
        status: (tr.status === 'done' ? 'done' : 'streaming') as 'streaming' | 'done',
      })),
    };
  }
  for (const k of ['session_id', 'created_at', 'solve_payload', 'socratic_payload', 'quiz_payload', 'compare_payload']) {
    delete msg[k];
  }
  return msg as unknown as PolymorphicMessage;
}

function dispatch(event: string, payload: unknown, h: StreamHandlers): void {
  const p = payload as Record<string, unknown>;
  switch (event) {
    case 'meta':
      h.onMeta?.({ messageId: p.message_id as string, intent: p.intent as Intent });
      break;
    case 'delta':
      h.onDelta?.(p.text as string);
      break;
    case 'evidence':
      h.onEvidence?.(((p.list as Record<string, unknown>[]) ?? []).map(normalizeEvidence));
      break;
    case 'usage':
      h.onUsage?.(p.usage as UsageInfo);
      break;
    case 'track_delta':
      h.onTrackDelta?.(p.index as number, p.model_name as string, p.text as string);
      break;
    case 'track_done':
      h.onTrackDone?.(p.index as number, p.model_name as string);
      break;
    case 'card':
      h.onCard?.(normalizeCard(payload as Record<string, unknown>));
      break;
    case 'done':
      h.onDone?.();
      break;
  }
}

// ------------------------------------------------------------- chat stream --
export async function streamChat(
  body: {
    sessionId: string;
    text?: string;
    imageB64?: string;
    forceIntent?: Intent;
    courseId?: string;
    retrievalMode?: 'lecture' | 'review' | 'explore';
  },
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  try {
    const resp = await fetch(`${API_BASE}/chat/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: body.sessionId,
        text: body.text ?? '',
        image_b64: body.imageB64 ?? null,
        force_intent: body.forceIntent ?? null,
        course_id: body.courseId ?? '',
        retrieval_mode: body.retrievalMode ?? 'lecture',
      }),
      signal,
    });
    if (!resp.ok) throw new Error(`后端响应 ${resp.status}`);
    await consumeSSE(resp, handlers);
  } catch (err) {
    if ((err as Error).name === 'AbortError') {
      handlers.onDone?.();
      return;
    }
    handlers.onError?.(err as Error);
  }
}

// ------------------------------------------------------------ compare only --
export async function streamCompare(
  body: { question: string; modelKeys?: string[] },
  handlers: StreamHandlers,
): Promise<void> {
  try {
    const resp = await fetch(`${API_BASE}/compare/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: body.question, model_keys: body.modelKeys ?? null }),
    });
    if (!resp.ok) throw new Error(`后端响应 ${resp.status}`);
    await consumeSSE(resp, handlers);
  } catch (err) {
    handlers.onError?.(err as Error);
  }
}

// ---------------------------------------------------------------- sessions --
export async function fetchSessions() {
  const resp = await fetch(`${API_BASE}/sessions`);
  return resp.json();
}

export async function fetchMessages(sessionId: string) {
  const resp = await fetch(`${API_BASE}/sessions/${sessionId}/messages`);
  return resp.json();
}

// ----------------------------------------------------------------- quiz ----
export async function gradeQuiz(optionIndex: number) {
  const resp = await fetch(`${API_BASE}/quiz/grade?option_index=${optionIndex}`, { method: 'POST' });
  return resp.json() as Promise<{ correct: boolean; chosen: string; attribution: string; suggestion: string }>;
}

// ----------------------------------------------------------------- evidence ---
export async function fetchEvidence(chunkUrl: string): Promise<EvidenceBundle | null> {
  // chunkUrl 形如 /api/v1/evidence/audio/{id}，本身含版本前缀；剥掉 API_BASE 的前缀避免拼接成双份
  const origin = API_BASE.replace(/\/api\/v1\/?$/, '');
  try {
    const resp = await fetch(`${origin}${chunkUrl}`);
    if (!resp.ok) return null;
    const d = await resp.json();
    return {
      chunkId: d.chunk_id,
      audio: {
        chunkId: d.chunk_id,
        audioId: d.audio_id,
        timestampRange: d.timestamp_range,
        transcript: d.transcript,
        waveform: d.waveform ?? [],
      },
      board: { url: d.board_image_url, caption: d.board_caption, boardIndex: d.board_index },
      examPoint: d.exam_point,
    };
  } catch {
    return null;
  }
}

// ----------------------------------------------------------------- ingest --
export type IngestMediaType = 'audio' | 'board' | 'text';

export async function uploadAsset(
  sessionId: string,
  mediaType: IngestMediaType,
  file: File,
  textContent = '',
): Promise<IngestAsset> {
  const form = new FormData();
  form.append('session_id', sessionId);
  form.append('media_type', mediaType);
  if (file) form.append('file', file);
  if (textContent) form.append('text_content', textContent);
  const resp = await fetch(`${API_BASE}/ingest`, { method: 'POST', body: form });
  if (!resp.ok) {
    let detail = `${resp.status}`;
    try {
      const err = await resp.json();
      if (err?.detail) detail = typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail);
    } catch { /* ignore */ }
    throw new Error(`入库失败 ${detail}`);
  }
  const d = await resp.json();
  return {
    id: d.asset.id,
    kind: mediaType,
    uri: d.asset.uri,
    filename: d.asset.filename || file?.name || '',
    lectureDate: d.asset.lecture_date || '',
    chunkCount: d.chunks_added,
    pitfalls: d.pitfalls_extracted ?? [],
    createdAt: Date.now(),
  };
}

/** 纯文字素材入库：粘贴的课堂笔记/讲义经切片与向量化进入检索库 */
export async function uploadTextAsset(sessionId: string, text: string, title = '文字笔记'): Promise<IngestAsset> {
  return uploadAsset(sessionId, 'text', new File([new Blob([text], { type: 'text/plain' })], `${title}.txt`), text);
}

// ------------------------------------------------------------------ admin --
const ADMIN_TOKEN_KEY = 'admin_token';

export function getAdminToken(): string | null {
  if (typeof window === 'undefined') return null;
  return window.localStorage.getItem(ADMIN_TOKEN_KEY);
}

export function setAdminToken(token: string | null): void {
  if (typeof window === 'undefined') return;
  if (token) window.localStorage.setItem(ADMIN_TOKEN_KEY, token);
  else window.localStorage.removeItem(ADMIN_TOKEN_KEY);
}

function adminHeaders(): Record<string, string> {
  const token = getAdminToken();
  return token ? { 'X-Admin-Token': token } : {};
}

export interface ModelSectionConfig {
  provider?: string;
  base_url: string;
  api_key: string;
  model: string;
  configured?: boolean;
}

export interface RetrievalWeights {
  profile: 'strict' | 'balanced' | 'explore';
  vector: number;
  lexical: number;
  bm25: number;
  canonical_bonus: number;
  time_alpha: number;
}

export interface AdminChunkInfo {
  id: string;
  exam_point: string;
  course_id: string;
  chapter: string;
  lecture_date: string;
  is_canonical: boolean;
  method_version: number;
  supersedes: string | null;
  text: string;
}

export interface MediaSettings {
  image_quality: number;
  image_max_edge: number;
  audio_bitrate: number;
  audio_max_mb: number;
}

export interface AdminConfigView {
  admin_password_set: boolean;
  llm: ModelSectionConfig;
  embedding: ModelSectionConfig;
  asr: ModelSectionConfig;
  vlm: ModelSectionConfig;
  media?: MediaSettings;
  retrieval?: RetrievalWeights;
  model_tracks: Array<{ key: string; name: string }>;
}

export interface AdminStats {
  sessions: number;
  assets: number;
  chunks: number;
  seed_chunks: number;
  uploaded_chunks: number;
  mock_mode: boolean;
  tracks: Array<{ key: string; name: string }>;
  custom_sections: Record<string, boolean>;
  runtime_overridden: boolean;
}

export async function adminLogin(password: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/admin/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data?.detail ?? `登录失败 ${resp.status}`);
  setAdminToken(data.token);
}

export async function adminLogout(): Promise<void> {
  try {
    await fetch(`${API_BASE}/admin/logout`, { method: 'POST', headers: adminHeaders() });
  } catch { /* ignore */ }
  setAdminToken(null);
}

async function adminFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { ...(init?.headers ?? {}), ...adminHeaders() },
  });
  if (resp.status === 401) throw new Error('UNAUTHORIZED');
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data?.detail ?? `请求失败 ${resp.status}`);
  return data as T;
}

export async function fetchAdminConfig(): Promise<AdminConfigView> {
  return adminFetch('/admin/config');
}

export async function saveAdminConfig(
  patch: Partial<Record<'llm' | 'embedding' | 'asr' | 'vlm', Partial<ModelSectionConfig>>> & {
    media?: Partial<MediaSettings>;
  },
): Promise<AdminConfigView> {
  return adminFetch('/admin/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) });
}

export async function saveRetrievalWeights(weights: Partial<RetrievalWeights>): Promise<AdminConfigView> {
  return adminFetch('/admin/config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      retrieval: {
        profile: weights.profile ?? '',
        ...(weights.vector !== undefined ? { vector: String(weights.vector) } : {}),
        ...(weights.lexical !== undefined ? { lexical: String(weights.lexical) } : {}),
        ...(weights.bm25 !== undefined ? { bm25: String(weights.bm25) } : {}),
        ...(weights.canonical_bonus !== undefined ? { canonical_bonus: String(weights.canonical_bonus) } : {}),
        ...(weights.time_alpha !== undefined ? { time_alpha: String(weights.time_alpha) } : {}),
      },
    }),
  });
}

export async function fetchAdminChunks(courseId = ''): Promise<AdminChunkInfo[]> {
  return adminFetch(`/admin/chunks?course_id=${encodeURIComponent(courseId)}`);
}

export async function setChunkCanonical(chunkId: string, canonical: boolean): Promise<void> {
  await adminFetch(`/admin/chunks/${chunkId}/canonical`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ canonical }),
  });
}

export async function resetAdminSection(kind: 'llm' | 'embedding' | 'asr' | 'vlm'): Promise<AdminConfigView> {
  return adminFetch('/admin/config/reset', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind }) });
}

export async function testAdminModel(kind: 'llm' | 'embedding' | 'asr' | 'vlm'): Promise<{ ok: boolean; mode: string; message: string }> {
  return adminFetch(`/admin/config/test?kind=${kind}`, { method: 'POST' });
}

export async function changeAdminPassword(oldPassword: string, newPassword: string): Promise<void> {
  await adminFetch('/admin/password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  });
}

export async function fetchAdminStats(): Promise<AdminStats> {
  return adminFetch('/admin/stats');
}
