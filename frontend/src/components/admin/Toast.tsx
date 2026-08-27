'use client';

/** 右下角轻量 Toast 通知栈（无第三方依赖） */
import { useEffect } from 'react';

export interface ToastItem {
  id: number;
  kind: 'ok' | 'err' | 'info';
  text: string;
}

const STYLE: Record<ToastItem['kind'], { icon: string; cls: string }> = {
  ok: { icon: '✓', cls: 'border-chalk/40 bg-chalk-soft text-chalk' },
  err: { icon: '✕', cls: 'border-cinnabar/40 bg-cinnabar-soft text-cinnabar' },
  info: { icon: 'ℹ', cls: 'border-rule bg-[#fdfaf2] text-ink-soft' },
};

export default function ToastStack({ items, onDismiss }: { items: ToastItem[]; onDismiss: (id: number) => void }) {
  useEffect(() => {
    const timers = items.map((t) => window.setTimeout(() => onDismiss(t.id), 3400));
    return () => timers.forEach(window.clearTimeout);
  }, [items, onDismiss]);

  if (items.length === 0) return null;
  return (
    <div className="fixed bottom-6 right-6 z-50 flex w-72 flex-col gap-2">
      {items.map((t) => (
        <div
          key={t.id}
          onClick={() => onDismiss(t.id)}
          className={`animate-rise cursor-pointer rounded-lg border px-4 py-3 text-[12.5px] font-medium shadow-lg ${STYLE[t.kind].cls}`}
        >
          <span className="mr-1.5 font-bold">{STYLE[t.kind].icon}</span>
          {t.text}
        </div>
      ))}
    </div>
  );
}
