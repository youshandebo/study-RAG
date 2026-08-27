'use client';

/** 右侧协作抽屉容器：证据 / 大纲 / 入库三页签，滑出式面板 */
import { useEvidenceStore, type DrawerTab } from '@/stores/useEvidenceStore';
import EvidenceViewer from './EvidenceViewer';
import CourseOutlineTree from './CourseOutlineTree';
import QuickIngestPanel from './QuickIngestPanel';

const TABS: { key: DrawerTab; label: string }[] = [
  { key: 'evidence', label: '🔍 证据' },
  { key: 'outline', label: '🌳 大纲' },
  { key: 'ingest', label: '⬆️ 入库' },
];

export default function EvidenceDrawer() {
  const { open, tab, setTab, closeDrawer } = useEvidenceStore();

  return (
    <aside
      className={`relative h-full w-[380px] shrink-0 border-l border-rule bg-paper transition-all duration-300 ${
        open ? 'ml-0' : '-mr-[380px] opacity-0'
      }`}
      aria-hidden={!open}
      aria-label="证据与协作抽屉"
    >
      {/* 顶部页签 */}
      <div className="flex items-center justify-between border-b border-rule px-4 py-3">
        <div className="flex gap-1" role="tablist">
          {TABS.map((t) => (
            <button
              key={t.key}
              role="tab"
              aria-selected={tab === t.key}
              onClick={() => setTab(t.key)}
              className={`rounded-md px-3 py-1.5 text-[12.5px] font-medium transition ${
                tab === t.key ? 'bg-ink text-paper' : 'text-ink-soft hover:bg-paper-deep'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        <button onClick={closeDrawer} className="px-2 text-ink-faint hover:text-ink" aria-label="关闭抽屉">
          ✕
        </button>
      </div>

      <div className="h-[calc(100%-53px)] overflow-y-auto">
        {tab === 'evidence' && <EvidenceViewer />}
        {tab === 'outline' && <CourseOutlineTree />}
        {tab === 'ingest' && <QuickIngestPanel />}
      </div>
    </aside>
  );
}
