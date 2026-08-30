'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com

/** 会员登录 / 注册：学生入口，注册即送免费档（存储 200MB + 基础模型） */
import { useRouter } from 'next/navigation';
import { useState } from 'react';
import { GraduationCap, Mail } from 'lucide-react';
import { login as apiLogin, register as apiRegister } from '@/lib/api';

const TIERS = [
  { name: '免费版', price: '¥0', storage: '200MB', model: '基础模型' },
  { name: '专业版', price: '¥19/月', storage: '2GB', model: '高级模型' },
  { name: '旗舰版', price: '¥49/月', storage: '10GB', model: '最强模型' },
];

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (busy || !email || !password) return;
    setBusy(true);
    setError('');
    try {
      if (mode === 'login') await apiLogin(email, password);
      else await apiRegister(email, password);
      router.push('/');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-zinc-950 px-6 py-10">
      <div className="w-full max-w-3xl">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-2xl border border-white/10 bg-white/5" aria-hidden>
            <GraduationCap size={22} strokeWidth={1.5} className="text-zinc-200" />
          </div>
          <h1 className="text-xl font-semibold text-zinc-100">课堂原法助教 · 会员中心</h1>
          <p className="mt-1 text-[12px] text-zinc-500">平台与模型由我们提供，按阶段付费解锁更大存储与更强模型</p>
        </div>

        <div className="grid gap-5 md:grid-cols-[1fr_280px]">
          {/* 登录/注册卡片 */}
          <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-6 backdrop-blur-sm">
            <div className="mb-4 inline-flex overflow-hidden rounded-full border border-white/10" role="group">
              {(['login', 'register'] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => { setMode(m); setError(''); }}
                  className={`px-4 py-1.5 text-[12.5px] transition ${
                    mode === m ? 'bg-blue-600 font-semibold text-white' : 'text-zinc-400 hover:text-zinc-200'
                  }`}
                >
                  {m === 'login' ? '登录' : '注册（送免费档）'}
                </button>
              ))}
            </div>

            <label className="mb-3 block">
              <span className="mb-1 flex items-center gap-1.5 text-[11.5px] text-zinc-400">
                <Mail size={12} strokeWidth={1.5} /> 邮箱
              </span>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@school.edu"
                autoComplete="email"
                className="w-full rounded-lg border border-white/10 bg-black/30 px-3 py-2.5 text-[13px] text-zinc-100 placeholder-zinc-600 outline-none transition focus:border-blue-500"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-[11.5px] text-zinc-400">
                密码{mode === 'register' && '（≥8 位，含字母和数字）'}
              </span>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && void submit()}
                placeholder="••••••••"
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                className="w-full rounded-lg border border-white/10 bg-black/30 px-3 py-2.5 text-[13px] text-zinc-100 placeholder-zinc-600 outline-none transition focus:border-blue-500"
              />
            </label>

            {error && (
              <div className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-[12px] text-red-300">
                {error}
              </div>
            )}

            <button
              onClick={() => void submit()}
              disabled={busy || !email || !password}
              className="mt-4 w-full rounded-lg bg-blue-600 py-2.5 text-[13.5px] font-bold text-white transition hover:bg-blue-500 active:scale-[0.99] disabled:opacity-40"
            >
              {busy ? '处理中…' : mode === 'login' ? '登录' : '注册并进入'}
            </button>
            <p className="mt-3 text-center text-[10.5px] leading-relaxed text-zinc-600">
              注册即同意 AGPL-3.0-or-Commercial 双重许可条款；
              <br />
              你的会话内容仅本人可见（服务端强制隔离）。
            </p>
          </div>

          {/* 档位表 */}
          <div className="space-y-2.5">
            {TIERS.map((t, i) => (
              <div
                key={t.name}
                className={`rounded-xl border px-4 py-3 ${
                  i === 1 ? 'border-blue-500/40 bg-blue-500/[0.07]' : 'border-white/10 bg-white/[0.03]'
                }`}
              >
                <div className="flex items-baseline justify-between">
                  <span className="text-[13px] font-semibold text-zinc-100">{t.name}</span>
                  <span className="text-[12px] text-zinc-400">{t.price}</span>
                </div>
                <div className="mt-0.5 text-[11px] text-zinc-500">
                  存储 {t.storage} · {t.model}
                  {i === 1 && <span className="ml-1.5 text-blue-400">最受欢迎</span>}
                </div>
              </div>
            ))}
            <p className="px-1 text-[10.5px] leading-relaxed text-zinc-600">
              注册后默认免费档；开通/变更档位请联系 fennengxiong@qq.com（在线支付对接中）。
            </p>
          </div>
        </div>

        <div className="mt-5 text-center">
          <a href="/" className="text-[11.5px] text-zinc-500 transition hover:text-zinc-300">← 返回工作台</a>
        </div>
      </div>
    </div>
  );
}
