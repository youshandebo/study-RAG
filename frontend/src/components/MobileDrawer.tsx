'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com


/** 通用移动端抽屉：基于原生 <dialog> 实现。
 *  - showModal 自动处理焦点进入（首个可聚焦元素）与关闭后焦点返回（触发按钮）。
 *  - 原生 Escape 关闭会派发 close 事件，统一回调 onClose。
 *  - 点击遮罩（dialog 自身区域，而非内部内容）关闭。
 *  用于 xl 以下替代常驻侧栏，不干预大屏三列布局。 */
import { useEffect, useRef, type ReactNode } from 'react';
import { X } from 'lucide-react';

interface MobileDrawerProps {
  open: boolean;
  onClose: () => void;
  ariaLabel: string;
  /** 面板内容容器类名（默认深色导航面板样式，匹配会话侧栏） */
  panelClassName?: string;
  children: ReactNode;
}

export default function MobileDrawer({
  open,
  onClose,
  ariaLabel,
  panelClassName = 'bg-[#101013] text-paper',
  children,
}: MobileDrawerProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dlg = dialogRef.current;
    if (!dlg) return;
    if (open && !dlg.open) {
      dlg.showModal();
    } else if (!open && dlg.open) {
      dlg.close();
    }
  }, [open]);

  return (
    <dialog
      ref={dialogRef}
      aria-label={ariaLabel}
      onClose={onClose}
      onClick={(e) => {
        // 点击遮罩（dialog 自身，而非内部内容）关闭
        if (e.target === dialogRef.current) onClose();
      }}
      className="m-0 fixed inset-y-0 left-0 w-64 max-w-[85vw] overflow-hidden bg-transparent p-0 backdrop:bg-zinc-950/40"
    >
      <div className={`relative flex h-full flex-col ${panelClassName}`}>
        <button
          type="button"
          onClick={onClose}
          aria-label="关闭"
          className="absolute right-2 top-2 z-10 rounded-md p-1.5 text-paper/70 transition hover:bg-white/10 hover:text-paper"
        >
          <X size={16} strokeWidth={1.5} aria-hidden />
        </button>
        <div className="min-h-0 flex-1">{children}</div>
      </div>
    </dialog>
  );
}
