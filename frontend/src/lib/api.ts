/** Fetch / SSE 流式通信客户端封装 */

import type { EvidenceRef, Intent, PolymorphicMessage } from '@/types/message';
import type { EvidenceBundle, IngestAsset } from '@/types/evidence';

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000/api/v1';

export interface StreamHandlers {
  onMeta?: (meta: { messageId: string; intent: Intent }) => void;
  onDelta?: (text: string) => void;
  onEvidence?: (list: EvidenceRef[]) => void;
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
      h.onEvidence?.(p.list as EvidenceRef[]);
      break;
    case 'track_delta':
      h.onTrackDelta?.(p.index as number, p.model_name as string, p.text as string);
      break;
    case 'track_done':
      h.onTrackDone?.(p.index as number, p.model_name as string);
      break;
    case 'card':
      h.onCard?.(payload as unknown as PolymorphicMessage);
      break;
    case 'done':
      h.onDone?.();
      break;
  }
}

// ------------------------------------------------------------- chat stream --
export async function streamChat(
  body: { sessionId: string; text?: string; imageB64?: string; forceIntent?: Intent },
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

// -------------------------------------------------------------- evidence ---
export async function fetchEvidence(chunkUrl: string): Promise<EvidenceBundle | null> {
  try {
    const resp = await fetch(`${API_BASE}${chunkUrl.replace(API_BASE, '')}`);
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
export async function uploadAsset(
  sessionId: string,
  mediaType: 'audio' | 'board',
  file: File,
): Promise<IngestAsset> {
  const form = new FormData();
  form.append('session_id', sessionId);
  form.append('media_type', mediaType);
  form.append('file', file);
  const resp = await fetch(`${API_BASE}/ingest`, { method: 'POST', body: form });
  if (!resp.ok) throw new Error(`入库失败 ${resp.status}`);
  const d = await resp.json();
  return {
    id: d.asset.id,
    kind: mediaType,
    uri: d.asset.uri,
    filename: d.asset.filename || file.name,
    lectureDate: d.asset.lecture_date || '',
    chunkCount: d.chunks_added,
    pitfalls: d.pitfalls_extracted ?? [],
    createdAt: Date.now(),
  };
}
