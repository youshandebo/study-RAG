'use client';

/** 右侧协作抽屉容器：证据 / 大纲 / 入库三页签，滑出式面板 */
import { ListTree, Search, Upload } from 'lucide-react';
import { useEvidenceStore, type DrawerTab } from '@/stores/useEvidenceStore';
import EvidenceViewer from './EvidenceViewer';
import CourseOutlineTree from './CourseOutlineTree';
import QuickIngestPanel from './QuickIngestPanel';

const TABS: { key: DrawerTab; label: string; icon: typeof Search }[] = [
  { key: 'evidence', label: '证据', icon: Search },
  { key: 'outline', label: '大纲', icon: ListTree },
  { key: 'ingest', label: '入库', icon: Upload },
];

export default function EvidenceDrawer() {
  const { open, tab, setTab, closeDrawer } = useEvidenceStore();

  return (
    <>
      {/* <1280px：抽屉转浮层，背后遮罩点击关闭，避免挤压中间分屏 */}
      {open && (
        <div
          className="fixed inset-0 z-40 bg-zinc-950/30 xl:hidden"
          onClick={closeDrawer}
          aria-hidden
        />
      )}
      <aside
        className={`relative h-full w-[380px] shrink-0 border-l border-rule bg-paper transition-all duration-300 max-xl:fixed max-xl:inset-y-0 max-xl:right-0 max-xl:z-50 max-xl:shadow-2xl ${
          open ? 'ml-0' : '-mr-[380px] opacity-0 max-xl:pointer-events-none'
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
              className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[12.5px] font-medium transition ${
                tab === t.key ? 'bg-ink text-paper' : 'text-ink-soft hover:bg-paper-deep'
              }`}
            >
              <t.icon size={14} strokeWidth={1.5} />
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
    </>
  );
}
