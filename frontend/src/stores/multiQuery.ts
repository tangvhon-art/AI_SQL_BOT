// 多查询 V2.0 卡片状态机：以 sub_id 为索引，SSE 事件正向累积（禁止回退）
// Phase A：拆解预览（可编辑）→ Phase B：confirm 后并行执行实时跟踪
import { create } from 'zustand'
import type { SubQuerySpec, SubCardState, SubCardStatus } from '../types/chart'

export type MultiPhase = 'idle' | 'preview' | 'executing' | 'done' | 'cancelled'

interface MultiQueryState {
  taskId: string
  preview: SubQuerySpec[]           // 拆解预览（用户确认前可编辑）
  layoutHint: string
  originQuestion: string
  phase: MultiPhase
  subCards: Record<string, SubCardState>   // 执行中按 sub_id 的卡片状态
  confirmLoading: boolean
  messageId: number | null          // dashboard 事件下发的消息 id（单卡重试定位用）
  initPreview: (payload: {
    task_id?: string
    original_question: string
    sub_queries: SubQuerySpec[]
    layout_hint?: string
  }) => void
  setPreview: (subs: SubQuerySpec[], layoutHint: string, taskId?: string) => void
  updatePreviewSub: (subId: string, patch: Partial<SubQuerySpec>) => void
  togglePreviewSub: (subId: string, enabled: boolean) => void
  removePreviewSub: (subId: string) => void
  addPreviewSub: () => void
  resetPreview: () => void
  startExecuting: (subs: SubQuerySpec[]) => void
  setTaskId: (taskId: string) => void
  progress: (subId: string, stage: string, msg: string) => void
  subSql: (subId: string, sql: string) => void
  subResult: (payload: {
    sub_id: string
    title: string
    chart_type: string
    data: { columns: string[]; rows: unknown[][] }
    facts?: Record<string, unknown>
    anomalies?: Array<{ type: string; desc: string }>
    trace?: Record<string, unknown>
    interpretation?: string
  }) => void
  subError: (subId: string, error: string, retryable?: boolean, stage?: string) => void
  subClarify: (subId: string, candidates: Array<{ table: string; comment?: string }>) => void
  retryCard: (subId: string) => void
  markDashboard: (messageId: number | null) => void
  markCancelled: () => void
  reset: () => void
}

/** 状态迁移守卫：只允许正向迁移，乱序/回退事件直接丢弃 */
const FORWARD: Record<SubCardStatus, SubCardStatus[]> = {
  pending: ['generating'],
  generating: ['executing', 'error', 'cancelled', 'needs_clarify'],
  executing: ['success', 'error', 'cancelled', 'needs_clarify'],
  error: ['generating'],            // 仅重试允许 error → generating
  needs_clarify: ['generating'],    // 勾选确认表后重跑
  success: ['error'],               // 兼容重试后异常回退
  cancelled: [],
}

function canTransition(from: SubCardStatus, to: SubCardStatus): boolean {
  if (from === to) return false
  if (!FORWARD[from]) return false
  if (FORWARD[from].includes(to)) return true
  // 兜底：pending/不存在 允许进入任意状态（首个事件驱动）
  return from === 'pending' || !from
}

export const useMultiQueryStore = create<MultiQueryState>((set, get) => ({
  taskId: '',
  preview: [],
  layoutHint: 'auto',
  originQuestion: '',
  phase: 'idle',
  subCards: {},
  confirmLoading: false,
  messageId: null,

  initPreview: (payload) =>
    set({
      taskId: payload.task_id ?? '',
      preview: payload.sub_queries ?? [],
      layoutHint: payload.layout_hint ?? 'auto',
      originQuestion: payload.original_question ?? '',
      phase: 'preview',
      subCards: {},
    }),

  setPreview: (subs, layoutHint, taskId) =>
    set({ preview: subs, layoutHint, taskId: taskId ?? get().taskId, phase: 'preview' }),

  updatePreviewSub: (subId, patch) =>
    set((s) => ({
      preview: s.preview.map((q) => (q.sub_id === subId ? { ...q, ...patch } : q)),
    })),

  togglePreviewSub: (subId, enabled) =>
    set((s) => ({
      preview: s.preview.map((q) => (q.sub_id === subId ? { ...q, enabled } : q)),
    })),

  removePreviewSub: (subId) =>
    set((s) => ({ preview: s.preview.filter((q) => q.sub_id !== subId) })),

  addPreviewSub: () =>
    set((s) => {
      const n = s.preview.length + 1
      return {
        preview: [...s.preview, {
          sub_id: `q${n}`,
          question: '',
          intent: 'value',
          metrics: [],
          dimensions: [],
          filters: [],
          time: {},
          confidence: 0.3,
          enabled: true,
        }],
      }
    }),

  resetPreview: () => set({ preview: [], taskId: '', phase: 'idle' }),

  startExecuting: (subs) => {
    const cards: Record<string, SubCardState> = {}
    for (const q of subs) {
      cards[q.sub_id] = {
        sub_id: q.sub_id,
        status: 'pending',
        question: q.question,
        title: q.title,
        intent: q.intent,
        confidence: q.confidence,
        chartType: q.chart_hint,
      }
    }
    set({ subCards: cards, phase: 'executing', confirmLoading: false })
  },

  setTaskId: (taskId) => set({ taskId }),

  progress: (subId, stage, msg) =>
    set((s) => {
      const card = s.subCards[subId]
      if (!card) return s
      const to: SubCardStatus = stage === 'execute' ? 'executing' : 'generating'
      if (!canTransition(card.status, to)) return s
      return { subCards: { ...s.subCards, [subId]: { ...card, status: to } } }
    }),

  subSql: (subId, sql) =>
    set((s) => {
      const card = s.subCards[subId]
      if (!card) return s
      return { subCards: { ...s.subCards, [subId]: { ...card, sql } } }
    }),

  subResult: (payload) =>
    set((s) => {
      const card = s.subCards[payload.sub_id]
      if (!card) return s
      if (!canTransition(card.status, 'success')) return s
      return {
        subCards: {
          ...s.subCards,
          [payload.sub_id]: {
            ...card,
            status: 'success',
            title: payload.title ?? card.title,
            chartType: payload.chart_type ?? card.chartType,
            data: payload.data ?? null,
            facts: payload.facts,
            anomalies: payload.anomalies,
            trace: payload.trace,
            interpretation: payload.interpretation,
            error: undefined,
            retryable: undefined,
          },
        },
      }
    }),

  subError: (subId, error, retryable, stage) =>
    set((s) => {
      const card = s.subCards[subId]
      if (!card) return s
      // 已成功/已取消的卡片不接受 error（防止重试流的旧事件回写）
      if (card.status === 'success' || card.status === 'cancelled') return s
      if (!canTransition(card.status, 'error')) return s
      return {
        subCards: {
          ...s.subCards,
          [subId]: { ...card, status: 'error', error, retryable: retryable ?? true, stage },
        },
      }
    }),

  subClarify: (subId, candidates) =>
    set((s) => {
      const card = s.subCards[subId]
      if (!card) return s
      if (!canTransition(card.status, 'needs_clarify')) return s
      return {
        subCards: {
          ...s.subCards,
          [subId]: {
            ...card, status: 'needs_clarify', clarifyCandidates: candidates,
            error: '需要选择查询表', retryable: true,
          },
        },
      }
    }),

  retryCard: (subId) =>
    set((s) => {
      const card = s.subCards[subId]
      if (!card) return s
      if (!canTransition(card.status, 'generating')) return s
      return {
        subCards: {
          ...s.subCards,
          [subId]: { ...card, status: 'generating', error: undefined, sql: undefined },
        },
      }
    }),

  markDashboard: (messageId) => set({ messageId, phase: 'done' }),

  markCancelled: () => {
    const cards: Record<string, SubCardState> = {}
    for (const [subId, card] of Object.entries(get().subCards)) {
      if (card.status !== 'success' && card.status !== 'error') {
        cards[subId] = { ...card, status: 'cancelled' }
      } else {
        cards[subId] = card
      }
    }
    set({ subCards: cards, phase: 'cancelled' })
  },

  reset: () =>
    set({
      taskId: '',
      preview: [],
      layoutHint: 'auto',
      originQuestion: '',
      phase: 'idle',
      subCards: {},
      confirmLoading: false,
      messageId: null,
    }),
}))
