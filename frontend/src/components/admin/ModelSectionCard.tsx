'use client';

/** 单个模型分区的配置卡片：预设一键填充 + 字段编辑 + 连通性测试 */
import { ModelSectionConfig } from '@/lib/api';
import { PRESETS, SECTION_META, SectionKey } from './presets';

export interface TestState {
  running: boolean;
  result: { ok: boolean; message: string } | null;
}

export default function ModelSectionCard({
  kind,
  draft,
  configured,
  test,
  saving,
  onField,
  onPreset,
  onSave,
  onTest,
  onReset,
}: {
  kind: SectionKey;
  draft: ModelSectionConfig;
  configured: boolean;
  test: TestState;
  saving: boolean;
  onField: (key: keyof ModelSectionConfig, value: string) => void;
  onPreset: (preset: (typeof PRESETS)[SectionKey][number]) => void;
  onSave: () => void;
  onTest: () => void;
  onReset: () => void;
}) {
  const meta = SECTION_META[kind];
  const dirty =
    draft.model !== '' || draft.base_url !== '' || (draft.api_key !== '' && !draft.api_key.startsWith('******'));

  return (
    <section className="paper-card animate-rise overflow-hidden">
      {/* 卡片头 */}
      <div className={`flex items-center gap-3 bg-gradient-to-r ${meta.gradient} px-6 py-4`}>
        <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-white/15 text-xl backdrop-blur-sm" aria-hidden>
          {meta.icon}
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="font-display text-[15px] font-bold leading-tight text-white">{meta.title}</h2>
          <p className="mt-0.5 line-clamp-1 text-[11.5px] text-white/70">{meta.desc}</p>
        </div>
        {configured ? (
          <span className="flex shrink-0 items-center gap-1.5 rounded-full bg-white/15 px-2.5 py-1 text-[11px] font-medium text-white backdrop-blur-sm">
            <span className="relative flex h-1.5 w-1.5">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-200 opacity-75" />
              <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald-100" />
            </span>
            已接入
          </span>
        ) : (
          <span className="shrink-0 rounded-full bg-black/25 px-2.5 py-1 text-[11px] text-white/75">演示兜底</span>
        )}
      </div>

      <div className="px-6 py-5">
        {/* 服务商预设 */}
        <div className="mb-4">
          <div className="mb-1.5 text-[11.5px] font-medium text-ink-soft">常用服务商 · 点击一键填充</div>
          <div className="flex flex-wrap gap-1.5">
            {PRESETS[kind].map((p) => {
              const active = draft.base_url === p.base_url && draft.model === p.model;
              return (
                <button
                  key={p.name}
                  onClick={() => onPreset(p)}
                  className={`rounded-full border px-3 py-1 text-[11.5px] transition active:scale-95 ${
                    active
                      ? 'border-chalk bg-chalk text-white shadow-sm'
                      : 'border-rule bg-[#fdfaf2] text-ink-soft hover:border-chalk/60 hover:text-chalk'
                  }`}
                >
                  {p.name}
                </button>
              );
            })}
          </div>
        </div>

        {/* 字段 */}
        <div className="grid gap-3.5 sm:grid-cols-2">
          {kind === 'llm' && (
            <Field label="接口协议" hint="Claude 系列请选 Anthropic">
              <div className="flex gap-1.5">
                {[
                  { v: 'openai-compatible', label: 'OpenAI 兼容' },
                  { v: 'anthropic', label: 'Anthropic' },
                ].map((opt) => (
                  <button
                    key={opt.v}
                    type="button"
                    onClick={() => onField('provider', opt.v)}
                    className={`flex-1 rounded-md border px-2 py-1.5 text-[12px] transition ${
                      (draft.provider || 'openai-compatible') === opt.v
                        ? 'border-chalk bg-chalk-soft font-semibold text-chalk'
                        : 'border-rule bg-white/70 text-ink-faint hover:text-ink-soft'
                    }`}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
            </Field>
          )}
          <Field label="Base URL" hint="一般以 /v1 结尾">
            <input
              value={draft.base_url}
              onChange={(e) => onField('base_url', e.target.value)}
              placeholder="https://api.openai.com/v1"
              spellCheck={false}
              className="input-ghost w-full font-mono text-[12.5px]"
            />
          </Field>
          <Field label="API Key" hint={draft.api_key.startsWith('******') ? '掩码显示 · 不改动则保持原值' : '留空保存 = 回落 .env 环境变量'}>
            <div className="relative">
              <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-[12px] text-ink-faint" aria-hidden>🔑</span>
              <input
                value={draft.api_key}
                onChange={(e) => onField('api_key', e.target.value)}
                placeholder="sk-…"
                spellCheck={false}
                autoComplete="off"
                className="input-ghost w-full pl-7 font-mono text-[12.5px]"
              />
            </div>
          </Field>
          <Field label="模型名称">
            <input
              value={draft.model}
              onChange={(e) => onField('model', e.target.value)}
              placeholder={kind === 'llm' ? 'gpt-4o / deepseek-chat / qwen-max' : kind === 'embedding' ? 'text-embedding-3-small / bge-m3' : kind === 'asr' ? 'whisper-1 / SenseVoiceSmall' : 'qwen-vl-max / glm-4v-plus'}
              spellCheck={false}
              className="input-ghost w-full font-mono text-[12.5px]"
            />
          </Field>
        </div>

        {/* 测试结果 */}
        {test.result && (
          <div
            className={`animate-rise mt-4 flex items-start gap-2 rounded-lg border px-3.5 py-2.5 text-[12px] leading-relaxed ${
              test.result.ok ? 'border-chalk/40 bg-chalk-soft/50 text-chalk' : 'border-cinnabar/40 bg-cinnabar-soft text-cinnabar'
            }`}
          >
            <span className="mt-0.5 font-bold">{test.result.ok ? '✔' : '✘'}</span>
            <span className="min-w-0 break-all">{test.result.message}</span>
          </div>
        )}

        {/* 操作条 */}
        <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-rule/60 pt-4">
          <button
            onClick={onSave}
            disabled={saving}
            className="rounded-lg bg-chalk px-5 py-2 text-[12.5px] font-bold text-white shadow-sm transition hover:bg-[#173f37] active:scale-[0.98] disabled:opacity-50"
          >
            {saving ? '保存中…' : dirty ? '保存并生效' : '保存'}
          </button>
          <button
            onClick={onTest}
            disabled={test.running}
            className="inline-flex items-center gap-1.5 rounded-lg border border-chalk/50 bg-chalk-soft/40 px-4 py-2 text-[12.5px] font-medium text-chalk transition hover:bg-chalk-soft disabled:opacity-50"
          >
            {test.running && <span className="h-3 w-3 animate-spin rounded-full border-2 border-chalk/30 border-t-chalk" />}
            {test.running ? '正在测试…' : '⚡ 测试连通性'}
          </button>
          <button
            onClick={onReset}
            disabled={saving}
            title="清空面板配置，回落 .env 环境变量默认"
            className="ml-auto rounded-lg px-3 py-2 text-[12px] text-ink-faint transition hover:text-cinnabar disabled:opacity-50"
          >
            回落 .env 默认
          </button>
        </div>
      </div>
    </section>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 flex items-baseline justify-between">
        <span className="text-[11.5px] font-semibold text-ink-soft">{label}</span>
        {hint && <span className="text-[10px] text-ink-faint">{hint}</span>}
      </span>
      {children}
    </label>
  );
}
