'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com

/** "Powered by" 署名徽标 + 关于弹窗：双重许可与商业授权规则说明 */
import { useState } from 'react';
import { GraduationCap, Mail, X } from 'lucide-react';

export default function AboutDialog() {
  const [open, setOpen] = useState(false);

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="flex w-full items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.04] px-2 py-1.5 text-[10.5px] text-paper/55 transition hover:border-white/25 hover:text-paper/85"
        title="关于 · 许可与商业授权"
      >
        <GraduationCap size={12} strokeWidth={1.5} aria-hidden />
        Powered by <b className="font-semibold">AI Classroom Tutor</b>
      </button>

      {open && (
        <div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-zinc-950/50 p-6 backdrop-blur-sm"
          onClick={() => setOpen(false)}
          role="dialog"
          aria-modal="true"
          aria-label="关于本软件"
        >
          <div
            className="w-full max-w-md rounded-2xl border border-rule bg-white p-6 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start justify-between">
              <div className="flex items-center gap-2.5">
                <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-zinc-900" aria-hidden>
                  <GraduationCap size={16} strokeWidth={1.5} className="text-zinc-100" />
                </span>
                <div>
                  <div className="text-[14.5px] font-semibold text-ink">AI Classroom Tutor</div>
                  <div className="text-[11px] text-ink-faint">课堂原法助教 · v1.1.0</div>
                </div>
              </div>
              <button
                onClick={() => setOpen(false)}
                aria-label="关闭"
                className="rounded-md p-1 text-ink-faint transition hover:bg-paper-deep hover:text-ink"
              >
                <X size={15} strokeWidth={1.5} />
              </button>
            </div>

            <div className="mt-4 space-y-3 text-[12.5px] leading-relaxed text-ink-soft">
              <p>
                本软件采用 <b className="text-ink">AGPL-3.0-or-Commercial 双重许可</b>：
              </p>
              <ul className="list-disc space-y-1 pl-5">
                <li>
                  开源使用遵循 <b>AGPL-3.0</b>：网络分发 / SaaS / 修改部署必须向用户提供全部源代码，并保留本署名徽标；
                </li>
                <li>
                  闭源商用、私有化部署、OEM 贴牌或移除版权标识，须购买<b>商业授权</b>（社区版免费、机构版 / SaaS 版 / OEM 买断详见 README）；
                </li>
                <li>AI 生成内容仅供学习参考，可能存在错误，请自行核验后使用。</li>
              </ul>
              <a
                href="mailto:fennengxiong@qq.com?subject=AI%20Classroom%20Tutor%20%E5%95%86%E4%B8%9A%E6%8E%88%E6%9D%83%E5%92%A8%E8%AF%A2"
                className="inline-flex items-center gap-1.5 rounded-lg border border-chalk/40 bg-chalk-soft px-3 py-1.5 text-[12px] font-medium text-chalk transition hover:bg-chalk hover:text-white"
              >
                <Mail size={13} strokeWidth={1.5} />
                fennengxiong@qq.com
              </a>
              <p className="text-[11px] text-ink-faint">© 2026 fennengxiong. 未经授权移除版权标识即构成侵权。</p>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
