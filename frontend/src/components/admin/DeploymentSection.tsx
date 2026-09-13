'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com

/** 部署档位分区：三档单选（节能/标准/性能）+ 当前生效参数只读展示 */
import { useState } from 'react';
import { Server } from 'lucide-react';
import { AdminConfigView, DeploymentProfile, saveDeploymentProfile } from '@/lib/api';

const PROFILES: Array<{ key: DeploymentProfile['profile']; label: string; spec: string; desc: string }> = [
  { key: 'eco', label: '节能模式', spec: '1C1G / 1C2G', desc: '浅召回 + 小上下文 + 向量全落盘 + 入库串行，小内存 VPS 保命档' },
  { key: 'standard', label: '标准模式', spec: '2C4G / 4C8G', desc: '均衡深度与资源占用，默认推荐档' },
  { key: 'performance', label: '性能模式', spec: '8C16G+ / 独立机', desc: '深召回 + 大上下文 + 全内存索引 + 多路入库并发' },
];

const PARAM_LABELS: Array<{ key: keyof Omit<DeploymentProfile, 'profile'>; label: string; fmt?: (v: unknown) => string }> = [
  { key: 'final_top_k', label: '注入切片数' },
  { key: 'coarse_top_k', label: '召回候选池' },
  { key: 'token_budget', label: '上下文装箱预算', fmt: (v) => `${v} tokens` },
  { key: 'rerank_mode', label: '精排策略', fmt: (v) => (v === 'rrf_only' ? 'RRF 直通（零外部延迟）' : '远程 API 精排') },
  { key: 'max_concurrent_ingest', label: '入库并发' },
  { key: 'qdrant_on_disk', label: '向量库存储', fmt: (v) => (v ? '落盘（低内存）' : '全内存') },
  { key: 'embed_batch_size', label: 'Embedding 批大小' },
];

export default function DeploymentSection({ config }: { config: AdminConfigView }) {
  const [current, setCurrent] = useState<DeploymentProfile | null>(config.deployment ?? null);
  const [saving, setSaving] = useState(false);
  const [hint, setHint] = useState('');

  const apply = async (key: DeploymentProfile['profile']) => {
    if (saving || current?.profile === key) return;
    setSaving(true);
    setHint('');
    try {
      const view = await saveDeploymentProfile(key);
      setCurrent(view.deployment ?? null);
      setHint('✅ 已切换并即时生效');
    } catch (e) {
      setHint(`切换失败：${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  if (!current) return null;

  return (
    <section className="paper-card animate-rise px-6 py-5">
      <h2 className="flex items-center gap-1.5 text-[14.5px] font-semibold text-ink">
        <Server size={15} strokeWidth={1.5} aria-hidden />
        部署档位
      </h2>
      <p className="mt-1 text-[12px] text-ink-faint">
        按服务器规格一键联动检索深度、内存分配与入库并发。切换即时生效，无需重启。
      </p>

      <div className="mt-4 grid gap-2.5 sm:grid-cols-3">
        {PROFILES.map((p) => (
          <button
            key={p.key}
            onClick={() => void apply(p.key)}
            disabled={saving}
            className={`rounded-xl border px-4 py-3 text-left transition disabled:opacity-50 ${
              current.profile === p.key
                ? 'border-chalk bg-chalk-soft/60 shadow-sm'
                : 'border-rule bg-white/70 hover:border-chalk/40'
            }`}
          >
            <div className={`text-[13px] font-semibold ${current.profile === p.key ? 'text-chalk' : 'text-ink'}`}>
              {p.label}
              <span className="ml-1.5 font-mono text-[10.5px] font-normal text-ink-faint">{p.spec}</span>
            </div>
            <div className="mt-0.5 text-[11px] leading-relaxed text-ink-faint">{p.desc}</div>
          </button>
        ))}
      </div>

      {/* 当前生效参数（后端解析后的真实口径） */}
      <div className="mt-4 grid grid-cols-2 gap-x-6 gap-y-2 rounded-xl border border-dashed border-rule bg-paper-deep/30 px-4 py-3.5 sm:grid-cols-3">
        {PARAM_LABELS.map((item) => (
          <div key={String(item.key)} className="flex items-baseline justify-between gap-2">
            <span className="text-[11px] text-ink-faint">{item.label}</span>
            <span className="font-mono text-[11.5px] font-medium text-ink">
              {item.fmt ? item.fmt(current[item.key]) : String(current[item.key])}
            </span>
          </div>
        ))}
      </div>

      {hint && (
        <div className={`mt-3 text-[12px] ${hint.includes('失败') ? 'text-cinnabar' : 'text-chalk'}`}>{hint}</div>
      )}
    </section>
  );
}
