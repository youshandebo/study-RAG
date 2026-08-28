// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
/** 考点、易错陷阱、难易度定义 */

export interface ExamPointInsight {
  id: string;
  name: string;
  difficulty: number; // 1~5
  mastery: number; // 0~100 掌握度
  sourceChunkIds: string[];
}

export interface PitfallRule {
  id: string;
  statement: string;
  examPoint: string;
  frequency: 'high' | 'mid' | 'low';
}

export interface CourseOutlineNode {
  id: string;
  title: string;
  children?: CourseOutlineNode[];
  examPoints?: string[];
}
