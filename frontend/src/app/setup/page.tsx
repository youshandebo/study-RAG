'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com

/** 首次部署初始化向导：浏览器里创建平台管理员，无需改任何配置文件（New API 式体验） */
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import { Rocket } from 'lucide-react';

export default function SetupPage() {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    void (async () => {
      try {
        const resp = await fetch(`${process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000/api/v1'}/auth/setup/status`);
        const data = await resp.json();
        if (!data.needs_setup) router.replace('/login');
      } catch {
        /* 后端未起时留在本页 */
      }
      setChecking(false);
    })();
  }, [router]);

  const submit = async () => {
    if (busy || !email || !password) return;
    setBusy(true);
    setError('');
    try {
      const resp = await fetch(`${process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000/api/v1'}/auth/setup`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.detail ?? '初始化失败');
      // 保存管理员 token（kind=user, tier=max），直接进入工作台
      const { setSassToken, setSassUser } = await import('@/lib/api');
      setSassToken(data.token);
      setSassUser(data.user);
      router.push('/');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (checking) {
    return <div className="flex min-h-screen items-center justify-center bg-zinc-950 text-[12.5px] text-zinc-500">检查部署状态…</div>;
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-zinc-950 px-6">
      <div className="w-full max-w-md rounded-2xl border border-white/10 bg-white/[0.04] p-7 backdrop-blur-sm">
        <div className="mb-5 text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-2xl border border-white/10 bg-white/5" aria-hidden>
            <Rocket size={20} strokeWidth={1.5} className="text-blue-400" />
          </div>
          <h1 className="text-lg font-semibold text-zinc-100">初始化你的部署</h1>
          <p className="mt-1 text-[12px] text-zinc-500">
            创建平台管理员账号。该账号拥有旗舰档权益，<br />同时用于登录管理控制台。
          </p>
        </div>

        <label className="mb-3 block">
          <span className="mb-1 block text-[11.5px] text-zinc-400">管理员邮箱</span>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="owner@school.edu"
            className="w-full rounded-lg border border-white/10 bg-black/30 px-3 py-2.5 text-[13px] text-zinc-100 placeholder-zinc-600 outline-none transition focus:border-blue-500"
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-[11.5px] text-zinc-400">管理员密码（≥8 位，含字母和数字）</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && void submit()}
            placeholder="••••••••"
            className="w-full rounded-lg border border-white/10 bg-black/30 px-3 py-2.5 text-[13px] text-zinc-100 placeholder-zinc-600 outline-none transition focus:border-blue-500"
          />
        </label>

        {error && (
          <div className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-[12px] text-red-300">{error}</div>
        )}

        <button
          onClick={() => void submit()}
          disabled={busy || !email || !password}
          className="mt-5 w-full rounded-lg bg-blue-600 py-2.5 text-[13.5px] font-bold text-white transition hover:bg-blue-500 active:scale-[0.99] disabled:opacity-40"
        >
          {busy ? '初始化中…' : '完成初始化，进入平台'}
        </button>

        <p className="mt-4 text-center text-[10.5px] leading-relaxed text-zinc-600">
          初始化完成后此向导永久关闭。学生注册入口在登录页，<br />默认免费档，你可在管理控制台为他们开通付费档。
        </p>
      </div>
    </div>
  );
}
