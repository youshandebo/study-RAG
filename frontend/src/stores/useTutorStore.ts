/** 伴学状态机：记录当前教学步数、引导轮次与掌握度 */
import { create } from 'zustand';

interface TutorState {
  /** sessionId -> 状态 */
  bySession: Record<
    string,
    {
      stepIndex: number;
      totalSteps: number;
      finished: boolean;
      mastery: number;
      rounds: number;
    }
  >;

  sync: (sessionId: string, payload: { currentStepIndex: number; totalSteps: number }) => void;
  addRound: (sessionId: string) => void;
  grantMastery: (sessionId: string, delta: number) => void;
  reset: (sessionId: string) => void;
}

export const useTutorStore = create<TutorState>((set) => ({
  bySession: {},

  sync(sessionId, payload) {
    set((s) => {
      const prev = s.bySession[sessionId] ?? { stepIndex: -1, totalSteps: payload.totalSteps, finished: false, mastery: 0, rounds: 0 };
      return {
        bySession: {
          ...s.bySession,
          [sessionId]: {
            ...prev,
            stepIndex: payload.currentStepIndex,
            totalSteps: payload.totalSteps,
            finished: payload.currentStepIndex >= payload.totalSteps,
            mastery: Math.min(100, prev.mastery + (payload.currentStepIndex > prev.stepIndex ? Math.round(100 / payload.totalSteps) : 0)),
          },
        },
      };
    });
  },

  addRound(sessionId) {
    set((s) => {
      const prev = s.bySession[sessionId] ?? { stepIndex: -1, totalSteps: 5, finished: false, mastery: 0, rounds: 0 };
      return { bySession: { ...s.bySession, [sessionId]: { ...prev, rounds: prev.rounds + 1 } } };
    });
  },

  grantMastery(sessionId, delta) {
    set((s) => {
      const prev = s.bySession[sessionId] ?? { stepIndex: -1, totalSteps: 5, finished: false, mastery: 0, rounds: 0 };
      return { bySession: { ...s.bySession, [sessionId]: { ...prev, mastery: Math.min(100, prev.mastery + delta) } } };
    });
  },

  reset(sessionId) {
    set((s) => ({
      bySession: { ...s.bySession, [sessionId]: { stepIndex: -1, totalSteps: 5, finished: false, mastery: 0, rounds: 0 } },
    }));
  },
}));
