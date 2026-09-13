'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com

/** 课程大纲树：真实切片聚合数据驱动（讲座 → 章节 → 考点）。
 *
 * 数据源 GET /course/{course_id}/outline；上传完成后监听 outline:refresh 局部刷新。
 * 点考点 = 章节软过滤 Chip + 待填提问注入输入框，形成"导学 → 定向答疑"闭环。
 */
import { useCallback, useEffect, useState } from 'react';
import { Award, BookMarked, GraduationCap, Loader2, RefreshCw, Zap } from 'lucide-react';
import { useSessionStore } from '@/stores/useSessionStore';
import { useTutorStore } from '@/stores/useTutorStore';
import { CourseOutline, fetchCourseOutline } from '@/lib/api';

export default function CourseOutlineTree() {
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const sessions = useSessionStore((s) => s.sessions);
  const updateSessionMeta = useSessionStore((s) => s.updateSessionMeta);
  const setPendingPrompt = useSessionStore((s) => s.setPendingPrompt);
  const activeSession = sessions.find((x) => x.id === activeSessionId);
  const courseId = activeSession?.courseId ?? '';
  const activeChapter = activeSession?.chapter ?? '';
  const reviewMode = activeSession?.retrievalMode === 'review';
  const tutor = useTutorStore((s) => s.bySession[activeSessionId]);
  const mastery = tutor?.mastery ?? 0;

  const [outline, setOutline] = useState<CourseOutline | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    if (!courseId) {
      setOutline(null);
      return;
    }
    setLoading(true);
    setError('');
    try {
      setOutline(await fetchCourseOutline(courseId));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [courseId]);

  useEffect(() => {
    void load();
  }, [load]);

  // 上传完成 → 局部刷新（QuickIngestPanel 派发）
  useEffect(() => {
    const handler = () => void load();
    window.addEventListener('outline:refresh', handler);
    return () => window.removeEventListener('outline:refresh', handler);
  }, [load]);

  // 点考点：章节 Chip 软过滤 + 注入提问，用户按发送即定向答疑
  const askPoint = (chapter: string, point: string) => {
    if (!activeSession) return;
    updateSessionMeta(activeSessionId, { chapter });
    setPendingPrompt(`请讲解「${point}」的核心考点与常见易错点`);
  };

  return (
    <div className="px-5 py-4">
      {/* 检索模式联动条 */}
      <button
        onClick={() => activeSession && updateSessionMeta(activeSessionId, { retrievalMode: reviewMode ? 'lecture' : 'review' })}
        disabled={!activeSession}
        className={`mb-4 flex w-full items-center gap-2.5 rounded-xl border px-4 py-3 text-left transition disabled:opacity-40 ${
          reviewMode ? 'border-violet-500/25 bg-violet-500/5' : 'border-amber-500/25 bg-amber-500/5'
        }`}
      >
        <span aria-hidden>{reviewMode ? <GraduationCap size={16} strokeWidth={1.5} className="text-violet-600" /> : <Zap size={16} strokeWidth={1.5} className="text-amber-600" />}</span>
        <span className="min-w-0 flex-1">
          <span className={`block text-[12.5px] font-semibold ${reviewMode ? 'text-violet-700' : 'text-amber-700'}`}>
            {reviewMode ? '备考/复习模式' : '随堂模式'}
          </span>
          <span className="block truncate text-[11px] text-ink-faint">
            {reviewMode ? '纯语义检索，支持跨月多跳 · 点击切回随堂' : '近 1~2 周新授加权 · 点击切备考'}
          </span>
        </span>
        <BookMarked size={14} strokeWidth={1.5} className="shrink-0 text-ink-faint" aria-hidden />
      </button>

      {/* 掌握度总览 */}
      <div className="mb-4 rounded-xl border border-rule bg-white p-4">
        <div className="mb-2 flex items-center justify-between text-[13px]">
          <span className="font-semibold text-ink">本讲掌握度</span>
          <span className="font-display font-bold text-chalk">{mastery}%</span>
        </div>
        <div className="h-2 overflow-hidden rounded-full bg-paper-deep">
          <div
            className="h-full rounded-full bg-chalk transition-all duration-500"
            style={{ width: `${Math.max(3, mastery)}%` }}
          />
        </div>
        <div className="mt-2 text-[11.5px] text-ink-faint">
          引导步数 {Math.max(0, tutor?.stepIndex ?? 0)}/{tutor?.totalSteps ?? 5} · 伴学轮次 {tutor?.rounds ?? 0}
        </div>
      </div>

      {/* 大纲树：真实切片聚合数据 */}
      {!courseId && (
        <div className="rounded-xl border border-dashed border-rule px-4 py-6 text-center text-[12px] text-ink-faint">
          当前会话未绑定课程。在左侧边栏选择课程新建对话后，此处展示该课程的真实大纲。
        </div>
      )}
      {courseId && loading && !outline && (
        <div className="flex items-center justify-center gap-2 py-8 text-[12px] text-ink-faint">
          <Loader2 size={14} className="animate-spin" aria-hidden /> 正在聚合课程大纲…
        </div>
      )}
      {courseId && error && (
        <div className="rounded-xl border border-cinnabar/30 bg-cinnabar/5 px-4 py-3 text-[12px] text-cinnabar">
          {error}
          <button onClick={() => void load()} className="ml-2 underline">重试</button>
        </div>
      )}
      {courseId && outline && outline.lectures.length === 0 && !loading && (
        <div className="rounded-xl border border-dashed border-rule px-4 py-6 text-center text-[12px] text-ink-faint">
          该课程暂无切片。上传课件/录音后，大纲树将自动生成。
        </div>
      )}

      {outline && outline.lectures.length > 0 && (
        <div className="space-y-2">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[11px] text-ink-faint">
              {outline.lectures.length} 个课件 · 点击考点定向答疑
            </span>
            <button
              onClick={() => void load()}
              disabled={loading}
              className="inline-flex items-center gap-1 text-[11px] text-ink-faint transition hover:text-ink-soft disabled:opacity-40"
              title="重新聚合大纲"
            >
              <RefreshCw size={11} strokeWidth={1.5} className={loading ? 'animate-spin' : ''} aria-hidden /> 刷新
            </button>
          </div>
          {outline.lectures.map((lec) => (
            <details key={lec.lecture_id} open className="rounded-lg border border-rule">
              <summary className="flex cursor-pointer items-center justify-between px-4 py-2.5 text-[13px] font-semibold text-ink">
                <span className="truncate">{lec.title}</span>
                <span className="ml-2 shrink-0 text-[10.5px] font-normal text-ink-faint">{lec.chunk_count} 切片</span>
              </summary>
              <div className="space-y-2 px-4 pb-3">
                {lec.chapters.map((ch) => (
                  <div key={ch.name}>
                    <div className="mb-1.5 text-[12.5px] font-medium text-ink-soft">{ch.name}</div>
                    <ul className="space-y-1.5">
                      {ch.exam_points.map((pt) => {
                        const selected = activeChapter === ch.name;
                        return (
                          <li key={pt.name}>
                            <button
                              onClick={() => askPoint(ch.name, pt.name)}
                              className={`flex w-full items-center justify-between gap-2 rounded-md px-3 py-2 text-left text-[12.5px] transition ${
                                selected
                                  ? 'bg-chalk-soft/70 text-chalk ring-1 ring-chalk/30'
                                  : 'bg-paper-deep/50 text-ink-soft hover:bg-chalk-soft/40 hover:text-ink'
                              }`}
                              title={`点击定向提问「${pt.name}」`}
                            >
                              <span className="min-w-0 flex-1 truncate">{pt.name}</span>
                              <span className="flex shrink-0 items-center gap-1.5">
                                {pt.has_canonical && (
                                  <span className="inline-flex items-center gap-0.5 rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[9.5px] font-bold text-amber-600" title="该考点已有老师定版解法">
                                    <Award size={9} strokeWidth={2} aria-hidden /> 定版
                                  </span>
                                )}
                                <span className="text-[10.5px] text-ink-faint">{pt.chunk_count} 切片</span>
                              </span>
                            </button>
                          </li>
                        );
                      })}
                    </ul>
                  </div>
                ))}
              </div>
            </details>
          ))}
        </div>
      )}
    </div>
  );
}
