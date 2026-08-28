'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com


/** 管理员控制台：左侧导航（概览 / 四类模型 / 安全）+ 概览仪表盘 + 配置热生效 */
import { useCallback, useEffect, useState } from 'react';
import {
  Brain,
  Eye,
  Gauge,
  GraduationCap,
  Image as ImageIcon,
  KeyRound,
  LayoutDashboard,
  Mic,
  SlidersHorizontal,
} from 'lucide-react';
import {
  AdminConfigView,
  AdminStats,
  MediaSettings,
  ModelSectionConfig,
  adminLogin,
  adminLogout,
  changeAdminPassword,
  fetchAdminConfig,
  fetchAdminStats,
  getAdminToken,
  resetAdminSection,
  saveAdminConfig,
  testAdminModel,
} from '@/lib/api';
import AdminLoginCard from '@/components/admin/AdminLoginCard';
import ModelSectionCard, { TestState } from '@/components/admin/ModelSectionCard';
import ToastStack, { ToastItem } from '@/components/admin/Toast';
import { SECTION_META, SectionKey } from '@/components/admin/presets';

type TabKey = 'overview' | SectionKey | 'media' | 'security';
type Drafts = Record<SectionKey, ModelSectionConfig>;

const NAV: Array<{ key: TabKey; label: string; icon: typeof Gauge }> = [
  { key: 'overview', label: '概览', icon: LayoutDashboard },
  { key: 'llm', label: '大语言模型', icon: Brain },
  { key: 'embedding', label: '嵌入模型', icon: Eye },
  { key: 'asr', label: '语音转文字', icon: Mic },
  { key: 'vlm', label: '多模态识图', icon: ImageIcon },
  { key: 'media', label: '媒体压缩', icon: SlidersHorizontal },
  { key: 'security', label: '密码与安全', icon: KeyRound },
];

const SECTION_KEYS: SectionKey[] = ['llm', 'embedding', 'asr', 'vlm'];

export default function AdminPage() {
  const [phase, setPhase] = useState<'loading' | 'login' | 'ready'>('loading');
  const [tab, setTab] = useState<TabKey>('overview');

  const [config, setConfig] = useState<AdminConfigView | null>(null);
  const [drafts, setDrafts] = useState<Drafts | null>(null);
  const [mediaDraft, setMediaDraft] = useState<MediaSettings | null>(null);
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [tests, setTests] = useState<Record<SectionKey, TestState>>({
    llm: { running: false, result: null },
    embedding: { running: false, result: null },
    asr: { running: false, result: null },
    vlm: { running: false, result: null },
  });
  const [savingKey, setSavingKey] = useState<SectionKey | 'all' | null>(null);
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  const toast = useCallback((kind: ToastItem['kind'], text: string) => {
    setToasts((prev) => [...prev.slice(-3), { id: Date.now() + Math.random(), kind, text }]);
  }, []);

  const loadAll = useCallback(async (): Promise<boolean> => {
    try {
      const [cfg, st] = await Promise.all([fetchAdminConfig(), fetchAdminStats()]);
      setConfig(cfg);
      if (cfg.media) setMediaDraft({ ...cfg.media });
      setDrafts({
        llm: { ...cfg.llm },
        embedding: { ...cfg.embedding },
        asr: { ...cfg.asr },
        vlm: { ...cfg.vlm },
      });
      setStats(st);
      return true;
    } catch (e) {
      if ((e as Error).message === 'UNAUTHORIZED') {
        setPhase('login');
        return false;
      }
      toast('err', `后端连接失败：${(e as Error).message}`);
      return false;
    }
  }, [toast]);

  useEffect(() => {
    (async () => {
      if (!getAdminToken()) {
        setPhase('login');
        return;
      }
      if (await loadAll()) setPhase('ready');
    })();
  }, [loadAll]);

  const handleLogin = async (password: string): Promise<string | null> => {
    try {
      await adminLogin(password);
      if (await loadAll()) setPhase('ready');
      return null;
    } catch (e) {
      return (e as Error).message;
    }
  };

  const handleLogout = async () => {
    await adminLogout();
    setPhase('login');
    setConfig(null);
    setDrafts(null);
  };

  const patchField = (kind: SectionKey, key: keyof ModelSectionConfig, value: string) =>
    setDrafts((prev) => (prev ? { ...prev, [kind]: { ...prev[kind], [key]: value } } : prev));

  const applyPreset = (kind: SectionKey, preset: { base_url: string; model: string; provider?: string }) =>
    setDrafts((prev) =>
      prev
        ? {
            ...prev,
            [kind]: {
              ...prev[kind],
              base_url: preset.base_url,
              model: preset.model,
              ...(preset.provider ? { provider: preset.provider } : kind === 'llm' ? { provider: 'openai-compatible' } : {}),
            },
          }
        : prev,
    );

  const handleSave = async (kind: SectionKey) => {
    if (!drafts) return;
    setSavingKey(kind);
    if (kind === ('media' as SectionKey) && mediaDraft) {
      try {
        const view = await saveAdminConfig({ media: mediaDraft });
        setConfig(view);
        if (view.media) setMediaDraft({ ...view.media });
        toast('ok', '媒体压缩参数已保存，即时生效');
      } catch (e) {
        toast('err', `保存失败：${(e as Error).message}`);
      } finally {
        setSavingKey(null);
      }
      return;
    }
    const d = drafts[kind];
    try {
      const view = await saveAdminConfig({
        [kind]: {
          ...(kind === 'llm' ? { provider: d.provider ?? '' } : {}),
          base_url: d.base_url,
          api_key: d.api_key, // 掩码值后端识别为「保持不变」
          model: d.model,
        },
      } as Parameters<typeof saveAdminConfig>[0]);
      setConfig(view);
      setDrafts((prev) => (prev ? { ...prev, [kind]: { ...view[kind] } } : prev));
      toast('ok', `${SECTION_META[kind].short}配置已保存，即时生效`);
      void fetchAdminStats().then(setStats).catch(() => undefined);
    } catch (e) {
      toast('err', `保存失败：${(e as Error).message}`);
    } finally {
      setSavingKey(null);
    }
  };

  const handleTest = async (kind: SectionKey) => {
    setTests((p) => ({ ...p, [kind]: { running: true, result: null } }));
    try {
      const r = await testAdminModel(kind);
      setTests((p) => ({
        ...p,
        [kind]: { running: false, result: { ok: r.ok, message: r.message } },
      }));
      if (!r.ok) toast('err', `${SECTION_META[kind].short}连通性测试未通过`);
    } catch (e) {
      const msg = (e as Error).message === 'UNAUTHORIZED' ? '会话过期，请重新登录' : (e as Error).message;
      setTests((p) => ({ ...p, [kind]: { running: false, result: { ok: false, message: msg } } }));
    }
  };

  const handleReset = async (kind: SectionKey) => {
    setSavingKey(kind);
    try {
      const view = await resetAdminSection(kind);
      setConfig(view);
      setDrafts((prev) => (prev ? { ...prev, [kind]: { ...view[kind] } } : prev));
      setTests((p) => ({ ...p, [kind]: { running: false, result: null } }));
      toast('info', `${SECTION_META[kind].short}已回落 .env 默认配置`);
    } catch (e) {
      toast('err', `重置失败：${(e as Error).message}`);
    } finally {
      setSavingKey(null);
    }
  };

  // -------------------------------------------------------------- render ----
  if (phase === 'loading') {
    return (
      <div className="flex min-h-screen items-center justify-center bg-paper">
        <div className="flex flex-col items-center gap-3">
          <div className="h-8 w-8 animate-spin rounded-full border-[3px] border-rule border-t-chalk" />
          <span className="text-[12.5px] text-ink-faint">正在进入管理控制台…</span>
        </div>
      </div>
    );
  }

  if (phase === 'login') return <AdminLoginCard onLogin={handleLogin} />;

  return (
    <div className="min-h-screen bg-paper">
      {/* 顶栏 */}
      <header className="sticky top-0 z-40 border-b border-rule bg-white/90 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-5 py-3">
          <div className="flex items-center gap-2.5">
            <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-zinc-900" aria-hidden>
              <GraduationCap size={16} strokeWidth={1.5} className="text-zinc-200" />
            </span>
            <div>
              <div className="font-display text-[15px] font-bold leading-tight text-ink">管理员控制台</div>
              <div className="text-[11px] text-ink-faint">模型配置即时热生效 · 无需重启</div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {stats && (
              <span
                className={`hidden items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium sm:inline-flex ${
                  stats.mock_mode ? 'bg-warn-soft text-warn' : 'bg-chalk-soft text-chalk'
                }`}
              >
                <span className={`h-1.5 w-1.5 rounded-full ${stats.mock_mode ? 'bg-warn' : 'bg-chalk'}`} />
                {stats.mock_mode ? '内置演示引擎' : '真实 API 模式'}
              </span>
            )}
            <a
              href="/"
              className="rounded-lg border border-rule bg-white/60 px-3 py-1.5 text-[12.5px] text-ink-soft transition hover:border-chalk hover:text-chalk"
            >
              ⟩ 返回工作台
            </a>
            <button
              onClick={() => void handleLogout()}
              className="rounded-lg border border-cinnabar/30 bg-cinnabar-soft/70 px-3 py-1.5 text-[12.5px] text-cinnabar transition hover:bg-cinnabar-soft"
            >
              退出登录
            </button>
          </div>
        </div>
      </header>

      {/* 移动端横向导航 */}
      <nav className="scrollbar-none flex gap-1.5 overflow-x-auto border-b border-rule bg-paper/70 px-4 py-2 lg:hidden">
        {NAV.map((n) => (
          <button
            key={n.key}
            onClick={() => setTab(n.key)}
            className={`inline-flex shrink-0 items-center gap-1.5 rounded-full px-3 py-1.5 text-[12px] transition ${
              tab === n.key ? 'bg-zinc-900 font-semibold text-white' : 'bg-paper-deep/70 text-ink-soft'
            }`}
          >
            <n.icon size={13} strokeWidth={1.5} />
            {n.label}
          </button>
        ))}
      </nav>

      <div className="mx-auto flex max-w-6xl gap-6 px-5 py-6">
        {/* 桌面端侧边导航 */}
        <aside className="sticky top-[76px] hidden h-fit w-52 shrink-0 flex-col gap-1 lg:flex">
          {NAV.map((n) => {
            const sk = n.key as SectionKey;
            const ready = SECTION_KEYS.includes(sk) && config?.[sk]?.configured;
            return (
              <button
                key={n.key}
                onClick={() => setTab(n.key)}
                className={`group flex items-center gap-2.5 rounded-xl px-3.5 py-2.5 text-left text-[13px] transition ${
                  tab === n.key
                    ? 'bg-board text-white shadow-md'
                    : 'text-ink-soft hover:bg-paper-deep/70 hover:text-ink'
                }`}
              >
                <n.icon size={15} strokeWidth={1.5} aria-hidden />
                <span className={`flex-1 font-medium ${tab === n.key ? '' : 'group-hover:text-ink'}`}>{n.label}</span>
                {SECTION_KEYS.includes(sk) && (
                  <span
                    className={`h-1.5 w-1.5 rounded-full ${
                      ready ? 'bg-emerald-300' : tab === n.key ? 'bg-white/40' : 'bg-rule'
                    }`}
                    title={ready ? '已接入真实模型' : '演示兜底模式'}
                  />
                )}
              </button>
            );
          })}
          <div className="mt-4 rounded-xl border border-dashed border-rule px-3.5 py-3 text-[10.5px] leading-relaxed text-ink-faint">
            配置持久化于<br />
            <code className="font-mono">backend/data/<br />runtime_config.json</code><br />
            替换嵌入模型后请重新上传素材
          </div>
        </aside>

        {/* 主内容 */}
        <main className="min-w-0 flex-1 pb-16">
          {tab === 'overview' && stats && config && (
            <Overview stats={stats} onGoto={setTab} />
          )}

          {SECTION_KEYS.map((kind) =>
            tab === kind && drafts ? (
              <ModelSectionCard
                key={kind}
                kind={kind}
                draft={drafts[kind]}
                configured={config?.[kind]?.configured ?? false}
                test={tests[kind]}
                saving={savingKey === kind}
                onField={(k, v) => patchField(kind, k, v)}
                onPreset={(p) => applyPreset(kind, p)}
                onSave={() => void handleSave(kind)}
                onTest={() => void handleTest(kind)}
                onReset={() => void handleReset(kind)}
              />
            ) : null,
          )}

          {tab === 'media' && mediaDraft && <MediaSection draft={mediaDraft} setDraft={setMediaDraft} saving={savingKey === ('media' as SectionKey)} onSave={() => void handleSave('media' as SectionKey)} />}

          {tab === 'security' && <SecuritySection onToast={toast} />}
        </main>
      </div>

      <ToastStack items={toasts} onDismiss={(id) => setToasts((prev) => prev.filter((t) => t.id !== id))} />
    </div>
  );
}

// ------------------------------------------------------------------ 概览 ----
function Overview({ stats, onGoto }: { stats: AdminStats; onGoto: (tab: TabKey) => void }) {
  const cards = [
    { label: '对话会话', value: stats.sessions, icon: '💬', tint: 'bg-blue-500/10 text-blue-600' },
    { label: '入库资产', value: stats.assets, icon: '📦', tint: 'bg-amber-500/10 text-amber-600' },
    { label: '知识库切片', value: stats.chunks, icon: '🗂️', tint: 'bg-emerald-500/10 text-emerald-600' },
    { label: '用户上传切片', value: stats.uploaded_chunks, icon: '⬆️', tint: 'bg-rose-500/10 text-rose-500' },
  ];
  return (
    <div className="animate-rise space-y-5">
      {/* 引擎状态横幅 */}
      <div
        className={`relative overflow-hidden rounded-2xl px-6 py-5 text-white shadow-md ${
          stats.mock_mode ? 'bg-gradient-to-r from-amber-600 to-amber-500' : 'bg-gradient-to-r from-zinc-800 to-zinc-700'
        }`}
      >
        <div
          className="pointer-events-none absolute inset-0 opacity-10"
          style={{ backgroundImage: 'radial-gradient(rgba(255,255,255,0.8) 0.5px, transparent 0.5px)', backgroundSize: '22px 22px' }}
        />
        <div className="relative flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-[17px] font-semibold">
              {stats.mock_mode ? '内置演示引擎运行中' : '真实 API 模式运行中'}
            </div>
            <p className="mt-1 text-[12px] text-white/80">
              {stats.mock_mode
                ? '所有模型走本地演示兜底——在左侧配置任一真实模型即可切换。'
                : `可用模型轨道：${stats.tracks.map((t) => t.name).join(' · ') || '（主模型）'}`}
            </p>
          </div>
          <button
            onClick={() => onGoto('llm')}
            className="rounded-lg bg-white/15 px-4 py-2 text-[12.5px] font-semibold backdrop-blur-sm transition hover:bg-white/25"
          >
            {stats.mock_mode ? '去配置模型 →' : '调整模型配置 →'}
          </button>
        </div>
      </div>

      {/* 统计卡 */}
      <div className="grid grid-cols-2 gap-3.5 sm:grid-cols-4">
        {cards.map((c) => (
          <div key={c.label} className="paper-card flex items-center gap-3.5 px-4 py-4">
            <span className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-xl text-xl ${c.tint}`} aria-hidden>
              {c.icon}
            </span>
            <div className="min-w-0">
              <div className="font-display text-[22px] font-bold leading-tight text-ink">{c.value}</div>
              <div className="truncate text-[11.5px] text-ink-faint">{c.label}</div>
            </div>
          </div>
        ))}
      </div>

      {/* 四类模型接入状态 */}
      <div className="paper-card divide-y divide-rule/60">
        <div className="px-5 py-3.5 font-display text-[13px] font-bold text-ink">模型接入状态</div>
        {SECTION_KEYS.map((kind) => {
          const ok = stats.custom_sections[kind];
          const meta = SECTION_META[kind];
          return (
            <button
              key={kind}
              onClick={() => onGoto(kind)}
              className="flex w-full items-center gap-3 px-5 py-3.5 text-left transition hover:bg-paper/70"
            >
              {(() => {
                const Icon = NAV.find((n) => n.key === kind)!.icon;
                return <Icon size={16} strokeWidth={1.5} className="text-ink-soft" aria-hidden />;
              })()}
              <div className="min-w-0 flex-1">
                <div className="text-[13px] font-medium text-ink">{meta.title}</div>
                <div className="truncate text-[11px] text-ink-faint">{meta.desc}</div>
              </div>
              <span
                className={`shrink-0 rounded-full px-2.5 py-1 text-[11px] font-medium ${
                  ok ? 'bg-chalk-soft text-chalk' : 'bg-paper-deep text-ink-faint'
                }`}
              >
                {ok ? '● 面板配置' : '○ 演示兜底'}
              </span>
              <span className="shrink-0 text-ink-faint" aria-hidden>›</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ 安全 ----
function SecuritySection({ onToast }: { onToast: (kind: ToastItem['kind'], text: string) => void }) {
  const [oldP, setOldP] = useState('');
  const [newP, setNewP] = useState('');
  const [newP2, setNewP2] = useState('');
  const [busy, setBusy] = useState(false);

  const strength = Math.min(3, Math.floor(newP.length / 4) + (/\d/.test(newP) ? 1 : 0) + (/[^\w]/.test(newP) ? 1 : 0));
  const strengthLabel = newP.length === 0 ? '' : strength <= 1 ? '偏弱' : strength === 2 ? '一般' : '稳健';
  const strengthColor = strength <= 1 ? 'bg-cinnabar' : strength === 2 ? 'bg-warn' : 'bg-chalk';

  const submit = async () => {
    if (busy) return;
    if (newP !== newP2) {
      onToast('err', '两次输入的新密码不一致');
      return;
    }
    setBusy(true);
    try {
      await changeAdminPassword(oldP, newP);
      onToast('ok', '密码已更新，下次登录使用新口令');
      setOldP(''); setNewP(''); setNewP2('');
    } catch (e) {
      onToast('err', (e as Error).message === 'UNAUTHORIZED' ? '会话过期，请刷新页面重新登录' : (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="paper-card animate-rise px-6 py-5">
      <h2 className="flex items-center gap-1.5 text-[14.5px] font-semibold text-ink">
        <KeyRound size={15} strokeWidth={1.5} aria-hidden />
        管理员密码
      </h2>
      <p className="mt-1 text-[12px] text-ink-faint">修改后立即生效；强烈建议首次部署即更换默认口令 admin123。</p>
      <div className="mt-4 grid gap-3.5 sm:grid-cols-3">
        <Field label="当前密码">
          <input type="password" value={oldP} onChange={(e) => setOldP(e.target.value)} className="input-ghost w-full" />
        </Field>
        <Field label="新密码（≥6 位）">
          <input type="password" value={newP} onChange={(e) => setNewP(e.target.value)} className="input-ghost w-full" />
          {newP && (
            <div className="mt-1.5 flex items-center gap-1.5">
              <div className="flex h-1 flex-1 gap-1">
                {[0, 1, 2].map((i) => (
                  <div key={i} className={`h-full flex-1 rounded-full ${i < strength ? strengthColor : 'bg-rule'}`} />
                ))}
              </div>
              <span className="text-[10px] text-ink-faint">{strengthLabel}</span>
            </div>
          )}
        </Field>
        <Field label="确认新密码">
          <input type="password" value={newP2} onChange={(e) => setNewP2(e.target.value)} className="input-ghost w-full" />
        </Field>
      </div>
      <button
        onClick={() => void submit()}
        disabled={busy || !oldP || !newP}
        className="mt-5 rounded-lg bg-chalk px-5 py-2 text-[12.5px] font-bold text-white shadow-sm transition hover:bg-zinc-800 disabled:opacity-50"
      >
        {busy ? '更新中…' : '更新密码'}
      </button>
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11.5px] font-semibold text-ink-soft">{label}</span>
      {children}
    </label>
  );
}


// ------------------------------------------------------------------ 媒体 ----
function MediaSection({
  draft,
  setDraft,
  saving,
  onSave,
}: {
  draft: MediaSettings;
  setDraft: (m: MediaSettings) => void;
  saving: boolean;
  onSave: () => void;
}) {
  const items: Array<{ key: keyof MediaSettings; label: string; hint: string; min: number; max: number; unit: string }> = [
    { key: 'image_quality', label: '图片质量', hint: 'WebP 质量 1-100，越高越清晰体积越大', min: 1, max: 100, unit: '' },
    { key: 'image_max_edge', label: '最大分辨率', hint: '长边上限像素，超出自动等比缩小', min: 256, max: 8192, unit: 'px' },
    { key: 'audio_bitrate', label: '音频码率', hint: 'Opus 目标码率 16-64（自动钳制 24-32 区间）', min: 16, max: 64, unit: 'kbps' },
    { key: 'audio_max_mb', label: '音频体积上限', hint: '上传录音大小上限', min: 1, max: 500, unit: 'MB' },
  ];
  return (
    <section className="paper-card animate-rise px-6 py-5">
      <h2 className="flex items-center gap-1.5 text-[14.5px] font-semibold text-ink">
        <SlidersHorizontal size={15} strokeWidth={1.5} aria-hidden />
        媒体压缩
      </h2>
      <p className="mt-1 text-[12px] text-ink-faint">
        图片统一转 WebP（限边长 + 锐化笔迹 + 抹 EXIF）；课堂录音转单声道 24kHz Opus（需服务器安装 ffmpeg）。
      </p>
      <div className="mt-4 grid gap-3.5 sm:grid-cols-2">
        {items.map((it) => (
          <label key={it.key} className="block">
            <span className="mb-1 flex items-baseline justify-between">
              <span className="text-[11.5px] font-semibold text-ink-soft">
                {it.label}
                {it.unit && <span className="ml-1 text-[10px] font-normal text-ink-faint">({it.unit})</span>}
              </span>
              <span className="font-mono text-[11px] text-ink">{draft[it.key]}</span>
            </span>
            <input
              type="range"
              min={it.min}
              max={it.max}
              value={draft[it.key]}
              onChange={(e) => setDraft({ ...draft, [it.key]: Number(e.target.value) })}
              className="w-full accent-blue-600"
            />
            <span className="mt-0.5 block text-[10px] text-ink-faint">{it.hint}</span>
          </label>
        ))}
      </div>
      <button
        onClick={onSave}
        disabled={saving}
        className="mt-5 rounded-lg bg-chalk px-5 py-2 text-[12.5px] font-semibold text-white transition hover:bg-blue-700 disabled:opacity-50"
      >
        {saving ? '保存中…' : '保存并生效'}
      </button>
    </section>
  );
}
