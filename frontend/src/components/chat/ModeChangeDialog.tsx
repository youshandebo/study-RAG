'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com

/** 模式切换强制阅读弹窗：倒计时结束前无法确认，防止学生误触后觉得"AI 变笨了" */
import { useEffect, useState } from 'react';
import { AlertTriangle, X } from 'lucide-react';

export default function ModeChangeDialog({
  targetLabel,
  description,
  onConfirm,
  onCancel,
}: {
  targetLabel: string;
  description: string[];
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const [remain, setRemain] = useState(5);

  useEffect(() => {
    if (remain <= 0) return;
    const timer = window.setTimeout(() => setRemain((v) => v - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [remain]);

  return (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-zinc-950/55 p-6 backdrop-blur-sm"
      onClick={onCancel}
      role="dialog"
      aria-modal="true"
      aria-label="切换确认"
    >
      <div
        className="w-full max-w-sm rounded-2xl border border-rule bg-white p-6 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between">
          <div className="flex items-center gap-2.5">
            <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-amber-500/10" aria-hidden>
              <AlertTriangle size={17} strokeWidth={1.5} className="text-amber-600" />
            </span>
            <div>
              <div className="text-[14.5px] font-semibold text-ink">切换到「{targetLabel}」</div>
              <div className="text-[11px] text-ink-faint">请先阅读以下说明（{remain}s）</div>
            </div>
          </div>
          <button
            onClick={onCancel}
            aria-label="取消"
            className="rounded-md p-1 text-ink-faint transition hover:bg-paper-deep hover:text-ink"
          >
            <X size={15} strokeWidth={1.5} />
          </button>
        </div>

        <ul className="mt-4 list-disc space-y-1.5 pl-5 text-[12.5px] leading-relaxed text-ink-soft">
          {description.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>

        <div className="mt-5 flex gap-2.5">
          <button
            onClick={onCancel}
            className="flex-1 rounded-lg border border-rule bg-white py-2.5 text-[13px] font-medium text-ink-soft transition hover:bg-paper-deep"
          >
            还是跟老师原法
          </button>
          <button
            onClick={() => remain <= 0 && onConfirm()}
            disabled={remain > 0}
            title={remain > 0 ? `请等待 ${remain} 秒` : '我已阅读并理解'}
            className={`flex-1 rounded-lg py-2.5 text-[13px] font-bold text-white transition ${
              remain > 0
                ? 'cursor-not-allowed bg-zinc-300 text-zinc-500'
                : 'animate-pulse bg-red-600 hover:bg-red-700'
            }`}
          >
            {remain > 0 ? `请稍候 ${remain}s` : '确认切换'}
          </button>
        </div>
      </div>
    </div>
  );
}
