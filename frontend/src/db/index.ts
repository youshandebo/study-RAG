// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
/** Dexie.js 本地离线数据库：缓存会话、消息、图片与 API Keys */
import Dexie, { type Table } from 'dexie';
import type { PolymorphicMessage } from '@/types/message';
import type { SessionMeta } from '@/stores/useSessionStore';

export interface CachedImage {
  id: string;
  sessionId: string;
  dataUrl: string;
  createdAt: number;
}

export interface AppSettings {
  key: string;
  value: string;
}

class TutorDB extends Dexie {
  sessions!: Table<SessionMeta, string>;
  messages!: Table<PolymorphicMessage, string>;
  images!: Table<CachedImage, string>;
  settings!: Table<AppSettings, string>;

  constructor() {
    super('ai-classroom-tutor');
    this.version(1).stores({
      sessions: 'id, createdAt',
      messages: 'id, sessionId, createdAt',
      images: 'id, sessionId',
      settings: 'key',
    });
    // v2（2026-09，P0 隐私修复）：sessions 加 owner 索引，本地库按账号分区。
    // 此前整个 IndexedDB 不区分登录者：init() 全量加载、登出不清数据——
    // 学校机房这类共享设备上，A 退出登录后 B 能看到 A 的全部会话、聊天记录
    // 与题目照片。服务端 TenantScope 那层管不到这里：泄漏根本不经过后端。
    this.version(2)
      .stores({
        sessions: 'id, createdAt, owner',
      })
      .upgrade(async (tx) => {
        // 存量无 owner 行一次性回填到 'anonymous' 桶（非破坏性迁移）：
        // 升级时无法归因数据属于哪个账号，宁可归匿名桶也不能默认归到
        // 下一个登录者名下。残留面：预修复数据在该浏览器匿名可见一次；
        // 修复后新建的会话全部按 owner 分区，不再产生无主行。
        await (tx.table('sessions') as unknown as Table<SessionMeta, string>)
          .toCollection()
          .modify((s: { owner?: string }) => {
            if (!s.owner) s.owner = 'anonymous';
          });
      });
  }
}

export const db = new TutorDB();

export async function hydrateMessages(sessionId: string): Promise<PolymorphicMessage[]> {
  const rows = await db.messages.where('sessionId').equals(sessionId).toArray();
  return rows
    .sort((a, b) => a.createdAt - b.createdAt)
    // 兜底清理历史遗留的空占位行（流式中断/旧版 bug 产生的无内容助教消息）
    .filter(
      (m) =>
        m.role === 'user' ||
        m.content.trim() !== '' ||
        !!m.solvePayload ||
        !!m.socraticPayload ||
        !!m.quizPayload ||
        !!m.comparePayload ||
        !!m.examReportPayload,
    );
}
