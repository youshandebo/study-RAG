'use client';

/** 管理员登录：黑板报风格 */
import { useState } from 'react';

export default function AdminLoginCard({
  onLogin,
}: {
  onLogin: (password: string) => Promise<string | null>; // 返回错误文案，null 表示成功
}) {
  const [password, setPassword] = useState('');
  const [show, setShow] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!password || busy) return;
    setBusy(true);
    setError('');
    const err = await onLogin(password);
    if (err) setError(err);
    setBusy(false);
  };

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-board px-6">
      {/* 黑板粉笔纹理 */}
      <div
        className="pointer-events-none absolute inset-0 opacity-[0.12]"
        style={{
          backgroundImage:
            'radial-gradient(rgba(255,255,255,0.7) 0.5px, transparent 0.5px), radial-gradient(rgba(255,255,255,0.5) 0.5px, transparent 0.5px)',
          backgroundSize: '26px 26px, 44px 44px',
          backgroundPosition: '0 0, 13px 21px',
        }}
      />
      {/* 角落粉笔装饰 */}
      <div className="pointer-events-none absolute left-10 top-8 select-none font-display text-[13px] italic text-white/25">
        knowledge is power ✦
      </div>
      <div className="pointer-events-none absolute bottom-10 right-12 select-none font-display text-[13px] italic text-white/25">
        学而不思则罔
      </div>

      <div className="animate-rise relative w-full max-w-sm">
        <div className="rounded-2xl border border-white/15 bg-white/[0.07] p-8 shadow-[0_24px_60px_-16px_rgba(0,0,0,0.55)] backdrop-blur-md">
          <div className="mb-6 text-center">
            <div className="mx-auto mb-3 flex h-14 w-14 items-center justify-center rounded-2xl border border-white/20 bg-white/10 text-3xl" aria-hidden>
              🎓
            </div>
            <h1 className="font-display text-xl font-bold tracking-wide text-[#f3efe2]">管理员登录</h1>
            <p className="mt-1 text-[12px] text-white/50">课堂原法助教 · 模型与知识库控制台</p>
          </div>

          <div className="relative">
            <input
              type={show ? 'text' : 'password'}
              autoFocus
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && void submit()}
              placeholder="管理员密码"
              className="w-full rounded-lg border border-white/20 bg-black/25 px-3.5 py-2.5 pr-12 text-[13.5px] text-[#f3efe2] placeholder-white/50 outline-none transition focus:border-[#7fd6c2] focus:ring-2 focus:ring-[#7fd6c2]/25"
            />
            <button
              type="button"
              onClick={() => setShow((v) => !v)}
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded px-2 py-1 text-[13px] text-white/60 transition hover:text-white/85"
              aria-label={show ? '隐藏密码' : '显示密码'}
            >
              {show ? '🙈' : '👁️'}
            </button>
          </div>

          {error && (
            <div className="animate-rise mt-3 rounded-lg border border-[#e8a08d]/50 bg-[#e8a08d]/15 px-3 py-2 text-[12px] text-[#f3c4b5]">
              {error}
            </div>
          )}

          <button
            onClick={() => void submit()}
            disabled={busy || !password}
            className="mt-4 w-full rounded-lg bg-[#7fd6c2] py-2.5 text-[13.5px] font-bold text-[#17332c] transition hover:bg-[#96e2d1] active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy ? (
              <span className="inline-flex items-center gap-2">
                <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-[#17332c]/30 border-t-[#17332c]" />
                验证中…
              </span>
            ) : (
              '进入后台'
            )}
          </button>

          <div className="mt-5 border-t border-white/10 pt-4 text-center">
            <p className="text-[11px] leading-relaxed text-white/55">
              首次部署默认口令 <code className="rounded bg-white/10 px-1.5 py-0.5 font-mono text-[10.5px]">admin123</code>
              <br />
              可用环境变量 <code className="font-mono text-[10.5px]">ADMIN_PASSWORD</code> 覆盖
            </p>
            <a href="/" className="mt-3 inline-block text-[11.5px] text-[#7fd6c2]/80 underline-offset-2 transition hover:text-[#7fd6c2] hover:underline">
              ← 返回工作台
            </a>
          </div>
        </div>
      </div>
    </div>
  );
}
