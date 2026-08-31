'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com


/** 全能输入框（Cherry Studio 式复合卡片面板）：
 * 顶部上下文 Chip（章节范围 / 场景模式）+ LaTeX 实时预览 + 附件预览槽 +
 * 自适应高度 + 斜杠指令 + 深度推导开关 + 字数统计 + 历史回溯（↑ 调出上一问） */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import katex from 'katex';
import {
  BookOpen, Brain, Camera, ChevronDown, Compass, FileText, Gauge,
  GitCompareArrows, Lightbulb, RotateCcw, SendHorizontal, Sigma, Square, Zap,
} from 'lucide-react';
import ModeChangeDialog from './ModeChangeDialog';
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

/** 斜杠指令模板：输入 / 触发，教育场景 Prompt 预制件 */
const SLASH_COMMANDS = [
  { cmd: '/错题诊断', desc: '分析解题步骤中的逻辑漏洞', template: '请逐步检查我下面的解题过程，指出第一处逻辑漏洞并解释原因：\n\n' },
  { cmd: '/板书对账', desc: '对比课堂板书与教材标准解法', template: '对比老师课堂板书与标准解法，解释板书里补充的技巧什么时候能用：\n\n' },
  { cmd: '/出同类题', desc: '根据当前知识点出巩固练习', template: '基于我们最近学的知识点，出一道同类型的变式题让我练习，先不要给答案。' },
  { cmd: '/公式推导', desc: '完整推导一个公式', template: '请完整推导以下公式的来历，每一步都说明依据：\n\n' },
  { cmd: '/复习总结', desc: '总结近期课堂要点', template: '请把这几次课的核心考点、易错点整理成一份复习清单。' },
];

/** 深度推导开关：开启后注入 system 级提示，要求完整证明步骤 */
const DEEP_REASONING_SUFFIX = '\n\n（请展示完整推导：每一步给出依据，关键步骤不要跳过。）';

/** 场景模式预设（Chip 快捷切换；explore 切换前弹确认防误触） */
const SCENE_PRESETS = [
  { key: 'lecture' as const, label: '随堂模式', desc: '优先近期板书与讲解，跟得上当前进度', icon: Zap },
  { key: 'review' as const, label: '期末复习', desc: '全局检索不偏科，早期重点一视同仁', icon: BookOpen },
  { key: 'explore' as const, label: '更多解法', desc: '解除"老师原法"限制，综合知识库多思路', icon: Compass },
];

/** 提取已闭合的最后一个公式（$..$ 或 $$..$$）做实时预览 */
function lastMathSnippet(text: string): string | null {
  const blocks = Array.from(text.matchAll(/\$\$([^$]+?)\$\$/g));
  if (blocks.length) return blocks[blocks.length - 1][1].trim();
  const inlines = Array.from(text.matchAll(/\$([^$\n]+?)\$/g));
  if (inlines.length) return inlines[inlines.length - 1][1].trim();
  return null;
}

export default function OmniChatInput() {
  const [text, setText] = useState('');
  const [image, setImage] = useState<{ dataUrl: string; b64: string; name: string } | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [deepReasoning, setDeepReasoning] = useState(false);   // 深度推导开关
  const [slashOpen, setSlashOpen] = useState(false);           // 斜杠指令弹层
  const [slashIdx, setSlashIdx] = useState(0);                 // 键盘选中项
  const [chapterOpen, setChapterOpen] = useState(false);       // 章节 Chip 弹层
  const [sceneOpen, setSceneOpen] = useState(false);           // 场景模式 Chip 弹层
  const [chapterDraft, setChapterDraft] = useState('');
  const historyRef = useRef<string[]>([]);                     // ↑ 历史回溯
  const historyPosRef = useRef(-1);

  const { sessions, activeSessionId, updateSessionMeta, appendMessage, setStreamingId, streamingMessageId, registerAbort } = useSessionStore();
  const activeSession = sessions.find((x) => x.id === activeSessionId);
  const exploreMode = activeSession?.retrievalMode === 'explore';
  const scene: 'lecture' | 'review' | 'explore' = exploreMode
    ? 'explore'
    : activeSession?.retrievalMode?.startsWith('review') ? 'review' : 'lecture';
  const activeChapter = activeSession?.chapter ?? '';
  // 切到"更多解法"时强制阅读弹窗（5s + 红色确认），防误触
  const [pendingExplore, setPendingExplore] = useState(false);
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
      setImage({ dataUrl, b64: dataUrl.split(',')[1] ?? '', name: file.name || 'clipboard.png' });
    };
    reader.readAsDataURL(file);
  }, []);

  // 自适应高度：min 52px / max 220px 超出内部滚动（Cherry Studio 手感）
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(220, Math.max(52, el.scrollHeight))}px`;
  }, [text]);

  // 打开章节弹层时预填当前章节
  useEffect(() => {
    if (chapterOpen) setChapterDraft(activeChapter);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chapterOpen]);

  // LaTeX 实时预览：仅在已闭合 $...$ 时渲染，语法错误即时提示
  const latexPreview = useMemo(() => {
    const tex = lastMathSnippet(text);
    if (!tex || tex.length > 400) return null;
    try {
      const html = katex.renderToString(tex, { throwOnError: true, strict: false, trust: false, displayMode: false });
      return { tex, html, error: null as string | null };
    } catch (e) {
      return { tex, html: '', error: (e as Error).message.replace(/^KaTeX parse error:\s*/, '') };
    }
  }, [text]);

  // 斜杠指令过滤
  const slashMatches = text.startsWith('/')
    ? SLASH_COMMANDS.filter((c) => c.cmd.startsWith(text.split(' ')[0]))
    : [];
  useEffect(() => {
    setSlashOpen(slashMatches.length > 0);
    setSlashIdx(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);

  const applySlash = (tpl: string, cmd: string) => {
    setText(tpl);
    setSlashOpen(false);
    textareaRef.current?.focus();
    void cmd;
  };

  const onPaste = (e: React.ClipboardEvent) => {
    const item = Array.from(e.clipboardData.items).find((i) => i.type.startsWith('image/'));
    if (item) {
      const file = item.getAsFile();
      if (file) readImage(file);
      e.preventDefault();
    }
  };

  const applyChapter = () => {
    if (activeSession) updateSessionMeta(activeSessionId, { chapter: chapterDraft.trim() });
    setChapterOpen(false);
  };

  const switchScene = (key: 'lecture' | 'review' | 'explore') => {
    if (!activeSession || key === scene) { setSceneOpen(false); return; }
    if (key === 'explore') { setPendingExplore(true); setSceneOpen(false); return; } // 切换前强制阅读
    updateSessionMeta(activeSessionId, { retrievalMode: key });
    setSceneOpen(false);
  };

  const send = () => {
    if (busy || (!text.trim() && !image)) return;
    const outgoing = text.trim();
    if (outgoing) {
      historyRef.current = [outgoing, ...historyRef.current.filter((h) => h !== outgoing)].slice(0, 30);
      historyPosRef.current = -1;
    }
    const userMsg: PolymorphicMessage = {
      id: crypto.randomUUID(),
      sessionId: activeSessionId,
      role: 'user',
      type: 'general_text',
      createdAt: Date.now(),
      content: image ? `[上传题目照片${outgoing ? `：${outgoing}` : ''}]` : outgoing,
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
        // 深度推导开关：追加步骤完整性要求（后端拼入 prompt）
        text: deepReasoning ? outgoing + DEEP_REASONING_SUFFIX : outgoing,
        imageB64: image?.b64,
        courseId: activeSession?.courseId ?? '',
        chapter: activeChapter,   // 顶部 Chip 指定的章节软过滤
        retrievalMode: activeSession?.retrievalMode ?? 'lecture',
        timeAlphaOverride: activeSession?.timeAlphaOverride ?? null,
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

  const popoverOpen = chapterOpen || sceneOpen;
  const sceneLabel = SCENE_PRESETS.find((p) => p.key === scene)?.label ?? '随堂模式';

  return (
    <div className="border-t border-rule bg-paper px-6 py-4">
      <div className="mx-auto max-w-3xl">
        {/* Chip 弹层打开时的点击捕获层（点外部关闭） */}
        {popoverOpen && (
          <div className="fixed inset-0 z-20" onClick={() => { setChapterOpen(false); setSceneOpen(false); }} aria-hidden />
        )}

        <div
          className={`relative rounded-xl border bg-white px-4 pb-3 pt-2.5 transition ${
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
          {/* ---- 顶部上下文 Chip 栏（Cherry Studio 式）---- */}
          <div className="mb-2 flex flex-wrap items-center gap-1.5 border-b border-dashed border-rule/70 pb-2">
            {/* 章节范围 Chip */}
            <div className="relative">
              <button
                onClick={() => { setChapterOpen((v) => !v); setSceneOpen(false); }}
                title="限定检索的章节范围（空 = 全部章节）"
                className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-[12px] transition active:scale-95 ${
                  activeChapter
                    ? 'border-blue-500/50 bg-blue-500/10 font-medium text-blue-600'
                    : 'border-rule bg-white/70 text-ink-soft hover:border-chalk hover:text-chalk'
                }`}
              >
                <BookOpen size={13} strokeWidth={1.5} aria-hidden />
                <span className="max-w-[180px] truncate">{activeChapter || '全部章节'}</span>
                <ChevronDown size={11} strokeWidth={1.5} className="opacity-60" aria-hidden />
              </button>
              {chapterOpen && (
                <div className="absolute bottom-full left-0 z-30 mb-2 w-72 rounded-xl border border-rule bg-white p-3.5 shadow-xl">
                  <div className="mb-1 text-[12px] font-semibold text-ink">章节范围</div>
                  <p className="mb-2.5 text-[10.5px] leading-relaxed text-ink-faint">
                    限定后只在所选章节的板书/笔记中检索（未标注章节的素材仍会参与）。
                  </p>
                  <input
                    autoFocus
                    value={chapterDraft}
                    onChange={(e) => setChapterDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') { e.preventDefault(); applyChapter(); }
                      if (e.key === 'Escape') setChapterOpen(false);
                    }}
                    placeholder="如：第3章 导数与微分 / 3.2 中值定理"
                    className="w-full rounded-lg border border-rule bg-paper px-2.5 py-2 text-[12.5px] outline-none transition focus:border-chalk"
                  />
                  <div className="mt-2.5 flex items-center justify-between">
                    <button
                      onClick={() => { setChapterDraft(''); if (activeSession) updateSessionMeta(activeSessionId, { chapter: '' }); setChapterOpen(false); }}
                      className="text-[11px] text-ink-faint underline-offset-2 transition hover:text-cinnabar hover:underline"
                    >
                      清除限制
                    </button>
                    <button
                      onClick={applyChapter}
                      className="rounded-lg bg-chalk px-3.5 py-1.5 text-[12px] font-semibold text-white transition hover:bg-blue-700"
                    >
                      应用
                    </button>
                  </div>
                </div>
              )}
            </div>

            {/* 场景模式 Chip */}
            <div className="relative">
              <button
                onClick={() => { setSceneOpen((v) => !v); setChapterOpen(false); }}
                title="随堂 / 期末复习 / 更多解法 一键切换"
                className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-[12px] transition active:scale-95 ${
                  scene !== 'lecture'
                    ? scene === 'explore'
                      ? 'border-violet-500/50 bg-violet-500/10 font-medium text-violet-600'
                      : 'border-emerald-500/50 bg-emerald-500/10 font-medium text-emerald-600'
                    : 'border-rule bg-white/70 text-ink-soft hover:border-chalk hover:text-chalk'
                }`}
              >
                <Zap size={13} strokeWidth={1.5} aria-hidden />
                {sceneLabel}
                <ChevronDown size={11} strokeWidth={1.5} className="opacity-60" aria-hidden />
              </button>
              {sceneOpen && (
                <div className="absolute bottom-full left-0 z-30 mb-2 w-80 rounded-xl border border-rule bg-white p-3.5 shadow-xl">
                  <div className="mb-2 text-[12px] font-semibold text-ink">场景模式</div>
                  <div className="space-y-1.5">
                    {SCENE_PRESETS.map((p) => {
                      const Icon = p.icon;
                      const active = scene === p.key;
                      return (
                        <button
                          key={p.key}
                          onClick={() => switchScene(p.key)}
                          className={`flex w-full items-center gap-2.5 rounded-lg border px-3 py-2 text-left transition ${
                            active
                              ? 'border-chalk/50 bg-chalk-soft/60'
                              : 'border-transparent hover:border-rule hover:bg-paper-deep/40'
                          }`}
                        >
                          <Icon size={15} strokeWidth={1.5} className={active ? 'text-chalk' : 'text-ink-faint'} aria-hidden />
                          <span className="min-w-0 flex-1">
                            <span className={`block text-[12.5px] font-medium ${active ? 'text-chalk' : 'text-ink'}`}>{p.label}</span>
                            <span className="block truncate text-[10.5px] text-ink-faint">{p.desc}</span>
                          </span>
                          {active && <span className="shrink-0 text-[10.5px] font-semibold text-chalk">当前</span>}
                        </button>
                      );
                    })}
                  </div>
                  {/* 时间偏好强度：随堂模式下可微调 */}
                  <div className="mt-3 border-t border-dashed border-rule/70 pt-2.5">
                    <div className="mb-1 flex items-baseline justify-between">
                      <span className="flex items-center gap-1 text-[11.5px] font-semibold text-ink-soft">
                        <Gauge size={12} strokeWidth={1.5} aria-hidden /> 时间偏好 α
                      </span>
                      <span className="font-mono text-[10.5px] text-ink-faint">
                        {activeSession?.timeAlphaOverride != null ? activeSession.timeAlphaOverride.toFixed(2) : '跟随模式'}
                      </span>
                    </div>
                    <input
                      type="range"
                      min={0}
                      max={0.5}
                      step={0.05}
                      value={activeSession?.timeAlphaOverride ?? 0.2}
                      onChange={(e) =>
                        activeSession && updateSessionMeta(activeSessionId, { timeAlphaOverride: Number(e.target.value) })
                      }
                      className="w-full accent-blue-600"
                    />
                    <p className="mt-1 text-[10px] leading-relaxed text-ink-faint">
                      拉高偏向最近讲的内容；期末等大跨度复习建议调低。仅影响排序，不影响正确性。
                    </p>
                    {activeSession?.timeAlphaOverride != null && (
                      <button
                        onClick={() => activeSession && updateSessionMeta(activeSessionId, { timeAlphaOverride: null })}
                        className="mt-1.5 inline-flex items-center gap-1 text-[10.5px] text-chalk underline-offset-2 hover:underline"
                      >
                        <RotateCcw size={10} strokeWidth={1.5} /> 恢复模式默认
                      </button>
                    )}
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* LaTeX 实时预览条：输入 $...$ 即时渲染，语法错误即时提示 */}
          {latexPreview && (
            <div className="mb-2 flex animate-rise items-center gap-2.5 overflow-x-auto rounded-lg border border-rule/70 bg-paper/70 px-3 py-2">
              <span className="flex shrink-0 items-center gap-1 text-[10.5px] font-semibold text-ink-faint">
                <Sigma size={12} strokeWidth={1.5} aria-hidden /> 公式预览
              </span>
              {latexPreview.error ? (
                <span className="text-[11.5px] text-cinnabar" title={latexPreview.tex}>
                  语法有误：{latexPreview.error}
                </span>
              ) : (
                <span
                  className="min-w-0 text-[13px] text-ink"
                  // KaTeX 输出（trust:false，用户本地文本），XSS 安全
                  dangerouslySetInnerHTML={{ __html: latexPreview.html }}
                />
              )}
            </div>
          )}

          {/* 附件预览槽：Cherry Studio 式缩略卡片（文件名 + 一键移除） */}
          {image && (
            <div className="mb-2.5 flex animate-rise items-center gap-2.5 rounded-lg border border-rule bg-white/70 p-2">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={image.dataUrl} alt="待识别的题目图片" className="h-14 w-14 shrink-0 rounded-md object-cover" />
              <div className="min-w-0 flex-1">
                <div className="truncate text-[12.5px] font-medium text-ink-soft">{image.name}</div>
                <div className="mt-0.5 text-[11px] text-ink-faint">粘贴/拖入完成 · 发送后将自动 OCR 提取公式并匹配课堂解法</div>
              </div>
              <button onClick={() => setImage(null)} className="shrink-0 rounded-md p-1 text-ink-faint transition hover:bg-cinnabar-soft hover:text-cinnabar" aria-label="移除图片">
                ✕
              </button>
            </div>
          )}

          {/* 斜杠指令弹层（输入 / 触发） */}
          {slashOpen && (
            <div className="absolute bottom-full left-0 z-30 mb-2 w-80 overflow-hidden rounded-xl border border-rule bg-white shadow-xl">
              {slashMatches.map((c, i) => (
                <button
                  key={c.cmd}
                  onClick={() => applySlash(c.template, c.cmd)}
                  onMouseEnter={() => setSlashIdx(i)}
                  className={`flex w-full items-center gap-2.5 px-3.5 py-2.5 text-left transition ${
                    i === slashIdx ? 'bg-chalk-soft/70' : 'hover:bg-paper-deep/50'
                  }`}
                >
                  <FileText size={14} strokeWidth={1.5} className="shrink-0 text-chalk" aria-hidden />
                  <span className="min-w-0">
                    <span className="block text-[12.5px] font-semibold text-ink">{c.cmd}</span>
                    <span className="block truncate text-[11px] text-ink-faint">{c.desc}</span>
                  </span>
                </button>
              ))}
            </div>
          )}

          {/* 输入区 */}
          <div className="input-scroll flex items-end gap-2 px-1 py-1">
            <textarea
              ref={textareaRef}
              value={text}
              onChange={(e) => setText(e.target.value)}
              onPaste={onPaste}
              onKeyDown={(e) => {
                // 斜杠弹层激活时：↑↓ 选择，Enter/Tab 确认，Esc 关闭
                if (slashOpen && slashMatches.length) {
                  if (e.key === 'ArrowDown') { e.preventDefault(); setSlashIdx((i) => (i + 1) % slashMatches.length); return; }
                  if (e.key === 'ArrowUp') { e.preventDefault(); setSlashIdx((i) => (i - 1 + slashMatches.length) % slashMatches.length); return; }
                  if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); applySlash(slashMatches[slashIdx].template, slashMatches[slashIdx].cmd); return; }
                  if (e.key === 'Escape') { setSlashOpen(false); return; }
                }
                if (e.key === 'Escape') { setChapterOpen(false); setSceneOpen(false); return; }
                // 空输入时 ↑ 调出上一条历史提问（Cherry Studio 手感）
                if (e.key === 'ArrowUp' && !text && historyRef.current.length) {
                  e.preventDefault();
                  historyPosRef.current = Math.min(historyPosRef.current + 1, historyRef.current.length - 1);
                  setText(historyRef.current[historyPosRef.current]);
                  return;
                }
                if (e.key === 'ArrowDown' && !text && historyPosRef.current > 0) {
                  e.preventDefault();
                  historyPosRef.current -= 1;
                  setText(historyRef.current[historyPosRef.current]);
                  return;
                }
                if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  send();
                }
              }}
              rows={1}
              placeholder="输入题目 / 疑问…（/ 唤起指令模板 · $公式$ 实时预览 · 粘贴板书照片 · ↑ 回溯提问）"
              className="max-h-[220px] min-h-[52px] flex-1 resize-none bg-transparent text-[14px] leading-relaxed outline-none placeholder:text-ink-faint/80"
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

          {/* 底部工具栏：深度推导开关 + 快捷指令 + 字数统计 */}
          <div className="mt-2 flex flex-wrap items-center gap-1.5 border-t border-dashed border-rule/70 pt-2.5">
            <button
              onClick={() => setDeepReasoning((v) => !v)}
              title="深度推导：要求展示完整证明步骤与每步依据"
              aria-pressed={deepReasoning}
              className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-[12px] transition active:scale-95 ${
                deepReasoning
                  ? 'border-violet-500/50 bg-violet-500/10 font-medium text-violet-600'
                  : 'border-rule bg-white/70 text-ink-soft hover:border-chalk hover:text-chalk'
              }`}
            >
              <Brain size={14} strokeWidth={1.5} />
              深度推导
            </button>
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
            <span className="ml-auto flex items-center gap-2.5">
              {text.length > 0 && (
                <span className={`font-mono text-[10.5px] ${text.length > 4000 ? 'text-cinnabar' : 'text-ink-faint'}`}>
                  {text.length} 字
                </span>
              )}
              <span className="hidden text-[10.5px] text-ink-faint sm:block">Enter 发送 · Shift+Enter 换行</span>
            </span>
            <ContextMeter usages={sessionUsages} />
          </div>
        </div>

        {pendingExplore && (
          <ModeChangeDialog
            targetLabel="更多解法"
            description={[
              'AI 将不再强制按老师的标准解法讲解，改为综合知识库做纯语义检索；',
              '可能看到多种思路，包括与课堂讲法不同的解法；',
              '随时可在输入框「场景模式」切回随堂/期末复习，本会话的提问记录不受影响。',
            ]}
            onConfirm={() => {
              if (activeSession) updateSessionMeta(activeSessionId, { retrievalMode: 'explore' });
              setPendingExplore(false);
            }}
            onCancel={() => setPendingExplore(false)}
          />
        )}

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
