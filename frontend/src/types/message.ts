// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
/** 多态消息定义（Solve / Socratic / Quiz / Compare / General）——与后端 domain.py 一一对应 */

export type MessageType =
  | 'solve_card'
  | 'socratic_card'
  | 'quiz_card'
  | 'parallel_compare'
  | 'general_text';

export type Intent = 'solve' | 'socratic' | 'quiz' | 'compare' | 'general';

export interface BaseMessage {
  id: string;
  sessionId: string;
  role: 'user' | 'assistant' | 'system';
  createdAt: number;
}

export interface EvidenceRef {
  audioId: string;
  timestampRange: [string, string];
  audioSnippetUrl: string;
  boardImageUrl: string;
  transcriptSnippet: string;
  boardCaption: string;
}

/** 单轮 Token 用量（后端估算口径，键名与后端一致） */
export interface UsageInfo {
  input: number;
  output: number;
  total: number;
  duration_ms: number;
  cache_hit_rate: number;
  context_used: number;
  context_limit: number;
  context_breakdown: Array<{ label: string; tokens: number }>;
}

/** 聚合消息协议：通过 type 驱动 UI 渲染具体卡片 */
export interface PolymorphicMessage extends BaseMessage {
  type: MessageType;
  content: string;
  intent?: Intent;

  /** 1. 拍照解题与教学洞察载荷 */
  solvePayload?: {
    examPoint: string;
    difficulty: 1 | 2 | 3 | 4 | 5;
    pitfalls: string[];
    steps: string[];
    evidenceList: EvidenceRef[];
  };

  /** 2. 苏格拉底伴学载荷 */
  socraticPayload?: {
    currentStepIndex: number;
    totalSteps: number;
    guidingQuestion: string;
    hints: string[];
    /** P2-A 显式状态机：阶段与提示阶梯。流转由服务端 FSM 决定，前端只做展示 */
    phase?: string;
    phaseLabel?: string;
    hintLevel?: number;
    maxHintLevel?: number;
    revealed?: boolean;
    guardBlocked?: boolean;
    convergeFailed?: boolean;
  };

  /** 3. 针对易错点的自测题载荷 */
  quizPayload?: {
    questionText: string;
    options?: string[];
    targetPitfall: string;
    explanation: string;
  };

  /** 4. 多模型分屏并行比对载荷 */
  comparePayload?: {
    tracks: {
      modelName: string;
      content: string;
      status: 'streaming' | 'done';
    }[];
  };

  /** 本轮 Token 用量（完成后由 usage 事件 / 最终卡片携带） */
  usage?: UsageInfo;

  /** 知识库证据列表（SSE `evidence` 事件）。
   * solve 卡同时挂在 solvePayload.evidenceList；普通问答（general_text）
   * 挂载在顶层——否则 `[1]` 角标在普通回答里无处可指，联动无从谈起。 */
  evidenceList?: EvidenceRef[];
}

/** SSE 流式事件（后端 chat_stream 协议） */
export type StreamEvent =
  | { event: 'meta'; data: { message_id: string; intent: Intent } }
  | { event: 'delta'; data: { text: string } }
  | { event: 'evidence'; data: { list: EvidenceRef[] } }
  | { event: 'track_delta'; data: { index: number; model_name: string; text: string } }
  | { event: 'track_done'; data: { index: number; model_name: string } }
  | { event: 'card'; data: PolymorphicMessage }
  | { event: 'error'; data: { message: string } }
  | { event: 'done'; data: Record<string, never> };
