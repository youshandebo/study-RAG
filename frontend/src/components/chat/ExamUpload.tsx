'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
import { useRef, useState } from 'react';
import { gradeExamCard } from '@/lib/api';
import { useSessionStore } from '@/stores/useSessionStore';

/** 独立提交状态避免把非流式批改伪装成聊天 SSE；失败保留卷面供重试。 */
export default function ExamUpload() {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState('');
  const [file, setFile] = useState<File>();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const lock = useRef(false);
  const input = useRef<HTMLInputElement>(null);
  const sessionId = useSessionStore((s) => s.activeSessionId);
  const streaming = useSessionStore((s) => s.streamingMessageId);

  async function submit() {
    if (lock.current || !sessionId || (!text.trim() && !file)) return;
    const sid = sessionId;
    const store = useSessionStore.getState();
    const courseId = store.sessions.find((s) => s.id === sid)?.courseId ?? '';
    lock.current = true;
    setPending(true);
    setError('');
    try {
      const message = await gradeExamCard(sid, courseId, text.trim(), file);
      // 请求期间可切换会话，回执仍归属于提交时的会话。
      useSessionStore.getState().appendMessage(message);
      setText('');
      setFile(undefined);
      if (input.current) input.current.value = '';
      setOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : '批改失败。请刷新会话检查报告是否已保存，再决定是否重试。');
    } finally {
      lock.current = false;
      setPending(false);
    }
  }

  return <div className="mb-2 min-w-0">
    <button type="button" aria-expanded={open} onClick={() => setOpen(!open)} className="rounded-full border border-rule px-3 py-1 text-xs text-chalk">复习卷批改{pending ? ' · 批改中' : ''}</button>
    {open && <section aria-label="提交复习卷" className="mt-2 space-y-2 rounded-lg border border-rule bg-paper p-3 text-ink">
      <p className="text-xs text-ink-faint">上传图片或 UTF-8 文本，或直接粘贴卷面。每题另起一行，作答以“解：”或“答：”另起一行。最多批改 12 题；同时填写时优先使用文本。</p>
      <textarea aria-label="卷面文本" disabled={pending} value={text} onChange={(e) => setText(e.target.value)} rows={4} className="w-full min-w-0 rounded border border-rule bg-paper p-2 text-sm" placeholder={'1. 题目\n解：学生过程'} />
      <input ref={input} aria-label="上传复习卷文件" type="file" accept="image/*,.txt,text/plain" disabled={pending} className="block w-full min-w-0 text-xs" onChange={(e) => setFile(e.target.files?.[0])} />
      {error && <p role="alert" className="break-words text-xs text-cinnabar">{error} 如遇网络中断，请先刷新会话核对是否已有报告，避免重复提交。</p>}
      <button type="button" disabled={pending || !!streaming || !sessionId || (!text.trim() && !file)} onClick={() => void submit()} className="rounded bg-chalk px-3 py-2 text-sm text-white disabled:opacity-40">{pending ? '正在批改，请稍候…' : '提交批改'}</button>
    </section>}
  </div>;
}
