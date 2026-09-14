// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
/** 错题本 / 复习 / 租户卡点 前端契约（与后端 services/notebook 一一对应） */

export type MistakeSource = 'passive_converge' | 'active_manual' | 'exam_failed';
export type MistakeStatus = 'active' | 'mastered' | 'archived';

/** 错题条目（含 Leitner 复习调度元数据） */
export interface MistakeItem {
  id: string;
  tenantId: string;
  userId: string;
  courseId: string;
  conceptTag: string;
  sourceType: MistakeSource;
  sessionId: string;
  questionContext: string;
  referenceAnswer: string;
  options: string[] | null;
  misconception: string;
  /** 1..5，越高复习间隔越长（1→1天 2→3天 3→7天 4→14天 5→30天） */
  leitnerBox: number;
  /** 0..100 */
  masteryScore: number;
  reviewCount: number;
  lastReviewedAt: number;
  nextReviewAt: number;
  status: MistakeStatus;
  createdAt: number;
  updatedAt: number;
}

/** 复习结算结果。`correct=null` 表示无法确定性判定（既不放行也不惩罚） */
export interface ReviewResult {
  correct: boolean | null;
  reason: string;
  item: MistakeItem | null;
}

/** 变式题（结构同 QuizPayload） */
export interface VariantQuestion {
  questionText: string;
  options?: string[] | null;
  targetPitfall: string;
  explanation: string;
  answer?: string | null;
  difficulty?: number;
}

export interface VariantResult {
  ok: boolean;
  sourceId: string;
  question: VariantQuestion;
}

/** 租户级高频卡点（教研视角，仅本租户） */
export interface ConceptStruggle {
  conceptTag: string;
  mistakes: number;
  passiveConverge: number;
  /** 被动挂科率 = 引导自测失败数 / 该知识点错题总数 */
  convergeFailRate: number;
  avgMastery: number;
  mastered: number;
  active: number;
  lastAt: number;
}
