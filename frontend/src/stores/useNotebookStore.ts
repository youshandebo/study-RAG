// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
/** 错题本抽屉状态：到期角标 / 列表过滤 / 抽屉开合。
 *
 * 数据全部来自服务端（`/notebook/*`）——租户与用户作用域由后端解析，
 * 前端不做任何身份参数拼接（拼接出来的是可伪造的越权口子）。
 * 请求失败（未登录 / 网络）一律静默降级为空列表，不打断主界面。
 */
import { create } from 'zustand';
import { fetchDueMistakes, fetchNotebook } from '@/lib/api';
import type { MistakeItem } from '@/types/notebook';

export type NotebookTab = 'review' | 'struggles';
export type NotebookFilter = '' | 'active' | 'mastered' | 'archived';

interface NotebookState {
  open: boolean;
  tab: NotebookTab;
  /** 待复习数量：驱动入口红点角标 */
  dueCount: number;
  items: MistakeItem[];
  loading: boolean;
  statusFilter: NotebookFilter;

  openDrawer: (tab?: NotebookTab) => void;
  closeDrawer: () => void;
  setTab: (tab: NotebookTab) => void;
  setStatusFilter: (filter: NotebookFilter) => void;
  /** 拉取列表（按当前过滤条件）并顺带刷新角标 */
  refresh: () => Promise<void>;
  /** 只刷新待复习角标（轻量，登录后 / 复习后调用） */
  refreshDue: () => Promise<void>;
}

export const useNotebookStore = create<NotebookState>((set, get) => ({
  open: false,
  tab: 'review',
  dueCount: 0,
  items: [],
  loading: false,
  statusFilter: '',

  openDrawer(tab) {
    set({ open: true, tab: tab ?? get().tab });
    void get().refresh();
  },

  closeDrawer() {
    set({ open: false });
  },

  setTab(tab) {
    set({ tab });
  },

  setStatusFilter(filter) {
    set({ statusFilter: filter });
    void get().refresh();
  },

  async refresh() {
    set({ loading: true });
    try {
      const items = await fetchNotebook(get().statusFilter);
      // 角标口径是"到期未复习"，与列表过滤条件无关，单独取
      const due = await fetchDueMistakes(200);
      set({ items, dueCount: due.length });
    } catch {
      set({ items: [], dueCount: 0 });
    } finally {
      set({ loading: false });
    }
  },

  async refreshDue() {
    try {
      const due = await fetchDueMistakes(200);
      set({ dueCount: due.length });
    } catch {
      set({ dueCount: 0 });
    }
  },
}));
