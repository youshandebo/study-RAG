/** 证据抽屉联动状态：当前激活的音画证据引用 + 抽屉开合 + 入库进度 */
import { create } from 'zustand';
import type { EvidenceBundle, IngestAsset } from '@/types/evidence';

export type DrawerTab = 'evidence' | 'outline' | 'ingest';

interface EvidenceState {
  open: boolean;
  tab: DrawerTab;
  activeEvidence: EvidenceBundle | null;
  evidenceHistory: EvidenceBundle[];
  assets: IngestAsset[];
  ingestProgress: { filename: string; percent: number; stage: string } | null;

  openDrawer: (tab?: DrawerTab) => void;
  closeDrawer: () => void;
  setTab: (tab: DrawerTab) => void;
  activateEvidence: (bundle: EvidenceBundle) => void;
  registerAsset: (asset: IngestAsset) => void;
  setIngestProgress: (p: { filename: string; percent: number; stage: string } | null) => void;
}

export const useEvidenceStore = create<EvidenceState>((set) => ({
  open: false,
  tab: 'evidence',
  activeEvidence: null,
  evidenceHistory: [],
  assets: [],
  ingestProgress: null,

  openDrawer(tab) {
    set((s) => ({ open: true, tab: tab ?? s.tab }));
  },
  closeDrawer() {
    set({ open: false });
  },
  setTab(tab) {
    set({ tab });
  },
  activateEvidence(bundle) {
    set((s) => ({
      open: true,
      tab: 'evidence',
      activeEvidence: bundle,
      evidenceHistory: [bundle, ...s.evidenceHistory.filter((e) => e.chunkId !== bundle.chunkId)].slice(0, 8),
    }));
  },
  registerAsset(asset) {
    set((s) => ({ assets: [asset, ...s.assets] }));
  },
  setIngestProgress(p) {
    set({ ingestProgress: p });
  },
}));
