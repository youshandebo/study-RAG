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
  }
}

export const db = new TutorDB();

export async function hydrateMessages(sessionId: string): Promise<PolymorphicMessage[]> {
  const rows = await db.messages.where('sessionId').equals(sessionId).toArray();
  return rows.sort((a, b) => a.createdAt - b.createdAt);
}
