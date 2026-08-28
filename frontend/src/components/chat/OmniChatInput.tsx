'use client';

/** 全能输入框：文本 / 拍照上传 / 拖拽 / 粘贴图片 / 指令前缀 / 停止生成 */
import { useCallback, useRef, useState } from 'react';
import { Camera, GitCompareArrows, Lightbulb, SendHorizontal, Square, Zap } from 'lucide-react';
import { useSessionStore } from '@/stores/useSessionStore';
import { streamChat } from '@/lib/api';
import type { PolymorphicMessage, UsageInfo } from '@/types/message';
import ContextMeter from './ContextMeter';

const QUICK_CMDS = [
  { label: '拍照解题', icon: Camera, text: '', action: 'upload' as const },
  { label: '教我', icon: Lightbulb, text: '教我', intent: 'socratic' as const },
  { label: '考我', icon: Zap, text: '考我', intent: 'quiz' as const },
  { label: '对比', icon: GitCompareArrows, text: '对比一下不同模型的解法', intent: 'compare' as const },
];

export default function OmniChatInput() {
  const [text, setText] = useState('');
  const [image, setImage] = useState<{ dataUrl: string; b64: string } | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  const { activeSessionId, appendMessage, setStreamingId, streamingMessageId, registerAbort } = useSessionStore();
  const busy = streamingMessageId !== null;
  // 当前会话每轮用量（供右下角上下文容量面板聚合展示）
  const messagesMap = useSessionStore((s) => s.messagesBySession);
  const sessionUsages = (messagesMap[activeSessionId] ?? [])
    .map((m) => m.usage)
    .filter((u): u is UsageInfo => !!u);

  const readImage = useCallback((file: File) => {
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result);
      setImage({ dataUrl, b64: dataUrl.split(',')[1] ?? '' });
    };
    reader.readAsDataURL(file);
  }, []);

  const onPaste = (e: React.ClipboardEvent) => {
    const item = Array.from(e.clipboardData.items).find((i) => i.type.startsWith('image/'));
    if (item) {
      const file = item.getAsFile();
      if (file) readImage(file);
      e.preventDefault();
    }
  };

  const send = () => {
    if (busy || (!text.trim() && !image)) return;
    const userMsg: PolymorphicMessage = {
      id: crypto.randomUUID(),
      sessionId: activeSessionId,
      role: 'user',
      type: 'general_text',
      createdAt: Date.now(),
      content: image ? `[上传题目照片${text.trim() ? `：${text.trim()}` : ''}]` : text.trim(),
    };
    appendMessage(userMsg);

    const pendingId = crypto.randomUUID();
    appendMessage({
      id: pendingId,
      sessionId: activeSessionId,
      role: 'assistant',
      type: 'general_text',
      createdAt: Date.now() + 1,
      content: '',
    });
    setStreamingId(pendingId);

    const controller = new AbortController();
    abortRef.current = controller;
    registerAbort(controller);

    void streamChat(
      {
        sessionId: activeSessionId,
        text: text.trim(),
        imageB64: image?.b64,
      },
      {
        onMeta: (meta) => useSessionStore.setState((s) => ({
          messagesBySession: {
            ...s.messagesBySession,
            [activeSessionId]: (s.messagesBySession[activeSessionId] ?? []).map((m) =>
              m.id === pendingId ? { ...m, id: meta.messageId } : m,
            ),
          },
          streamingMessageId: meta.messageId,
        })),
        onDelta: (piece) => useSessionStore.getState().appendDelta(activeSessionId, pendingId, piece),
        onTrackDelta: (index, _name, piece) =>
          useSessionStore.getState().appendTrackDelta(activeSessionId, pendingId, index, piece),
        onTrackDone: (index) => useSessionStore.getState().finishTrack(activeSessionId, pendingId, index),
        onCard: (card) => {
          const store = useSessionStore.getState();
          // 用最终卡片替换占位（若 id 已因 meta 改名则按位置兜底）；legacyId 同步清理 IndexedDB 旧占位行
          const list = store.messagesBySession[activeSessionId] ?? [];
          const targetId = list.some((m) => m.id === card.id) ? card.id : pendingId;
          store.patchMessage(
            activeSessionId,
            targetId,
            {
              type: card.type,
              content: card.content,
              solvePayload: card.solvePayload,
              socraticPayload: card.socraticPayload,
              quizPayload: card.quizPayload,
              comparePayload: card.comparePayload,
              intent: card.intent,
              usage: card.usage,
            },
            pendingId,
          );
        },
        onDone: () => setStreamingId(null),
        onError: () => {
          useSessionStore.getState().patchMessage(activeSessionId, pendingId, {
            content: '⚠️ 连接助教失败，请确认后端服务已启动（默认 http://localhost:8000）。',
          });
          setStreamingId(null);
        },
      },
      controller.signal,
    );

    setText('');
    setImage(null);
  };

  const stop = () => {
    abortRef.current?.abort();
    registerAbort(null);
    setStreamingId(null);
  };

  return (
    <div className="border-t border-rule bg-paper px-6 py-4">
      <div className="mx-auto max-w-3xl">
        <div
          className={`rounded-xl border bg-white px-4 pb-3 pt-3 transition ${
            dragOver ? 'border-chalk ring-2 ring-chalk/20' : 'border-rule focus-within:border-chalk/70 focus-within:ring-2 focus-within:ring-chalk/15'
          }`}
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragOver(false);
            const file = Array.from(e.dataTransfer.files)[0];
            if (file?.type.startsWith('image/')) readImage(file);
          }}
        >
          {/* 图片预览 */}
          {image && (
            <div className="mb-2.5 flex items-center gap-2 rounded-lg border border-rule bg-white/70 p-2">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={image.dataUrl} alt="待识别的题目图片" className="h-14 w-14 rounded object-cover" />
              <span className="flex-1 text-[12.5px] text-ink-soft">题目图片已就绪，将自动提取公式并匹配课堂解法</span>
              <button onClick={() => setImage(null)} className="px-2 text-ink-faint hover:text-cinnabar" aria-label="移除图片">
                ✕
              </button>
            </div>
          )}

          {/* 输入区 */}
          <div className="input-scroll flex items-end gap-2 px-1 py-1">
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              onPaste={onPaste}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  send();
                }
              }}
              rows={1}
              placeholder="输入题目 / 疑问，或拖拽 · 粘贴题目照片…"
              className="max-h-36 min-h-[28px] flex-1 resize-none bg-transparent text-[14px] leading-relaxed outline-none placeholder:text-ink-faint/80"
            />
            {busy ? (
              <button
                onClick={stop}
                className="inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-cinnabar px-4 py-2 text-[13px] font-semibold text-white transition hover:opacity-90 active:scale-[0.98]"
              >
                <Square size={12} strokeWidth={2} /> 停止
              </button>
            ) : (
              <button
                onClick={send}
                disabled={!text.trim() && !image}
                title="Enter 发送，Shift+Enter 换行"
                aria-label="发送"
                className="inline-flex shrink-0 items-center gap-1 rounded-lg bg-chalk px-3.5 py-2 text-[13px] font-semibold text-white transition hover:bg-blue-700 active:scale-[0.98] disabled:opacity-40"
              >
                发送 <SendHorizontal size={14} strokeWidth={1.5} />
              </button>
            )}
          </div>

          {/* 快捷指令 */}
          <div className="mt-2 flex flex-wrap items-center gap-1.5 border-t border-dashed border-rule/70 pt-2.5">
            {QUICK_CMDS.map((cmd) => {
              const Icon = cmd.icon;
              const cls =
                'inline-flex items-center gap-1.5 rounded-full border border-rule bg-white/70 px-3 py-1 text-[12px] text-ink-soft transition hover:border-chalk hover:text-chalk active:scale-95';
              return cmd.action === 'upload' ? (
                <button key={cmd.label} onClick={() => fileRef.current?.click()} className={cls}>
                  <Icon size={14} strokeWidth={1.5} />
                  {cmd.label}
                </button>
              ) : (
                <button key={cmd.label} disabled={busy} onClick={() => setText(cmd.text)} className={`${cls} disabled:opacity-40`}>
                  <Icon size={14} strokeWidth={1.5} />
                  {cmd.label}
                </button>
              );
            })}
            <span className="ml-auto hidden text-[10.5px] text-ink-faint sm:block">Enter 发送 · Shift+Enter 换行</span>
            <ContextMeter usages={sessionUsages} />
          </div>
        </div>

        <input
          ref={fileRef}
          type="file"
          accept="image/*"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) readImage(f);
            e.target.value = '';
          }}
        />
      </div>
    </div>
  );
}
