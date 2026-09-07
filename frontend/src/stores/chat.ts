// 对话状态：会话列表、消息流、进行中状态
import { create } from 'zustand'
import type { ChatMsg, Conversation } from '../types'

interface ChatState {
  conversations: Conversation[]
  currentConvId: number | null
  messages: ChatMsg[]
  streaming: boolean
  stage: string
  setConversations: (list: Conversation[]) => void
  setCurrentConvId: (id: number | null) => void
  setMessages: (list: ChatMsg[]) => void
  appendUser: (text: string) => void
  appendStreamMsg: (msg: ChatMsg) => void
  setStreaming: (v: boolean) => void
  setStage: (s: string) => void
  /** 公共方法：合并更新第 index 条消息（content 深度合并，供 SSE 事件分片写入） */
  patchMessage: (index: number, patch: Partial<ChatMsg>) => void
  /** 公共方法：向第 index 条消息的 content[key] 追加增量文本（打字机/思考流） */
  appendDelta: (index: number, key: string, delta: string) => void
  reset: () => void
}

export const useChatStore = create<ChatState>((set) => ({
  conversations: [],
  currentConvId: null,
  messages: [],
  streaming: false,
  stage: '',
  setConversations: (list) => set({ conversations: list }),
  setCurrentConvId: (id) => set({ currentConvId: id }),
  setMessages: (list) => set({ messages: list }),
  appendUser: (text) =>
    set((s) => ({
      messages: [...s.messages, { role: 'user', content_type: 'text', content: { text } }],
    })),
  appendStreamMsg: (msg) => set((s) => ({ messages: [...s.messages, msg] })),
  setStreaming: (v) => set({ streaming: v }),
  setStage: (stage) => set({ stage }),
  patchMessage: (index, patch) =>
    set((s) => {
      if (index < 0 || index >= s.messages.length) return s
      const list = [...s.messages]
      const prev = list[index]
      const prevContent = (prev?.content ?? {}) as Record<string, unknown>
      const newContent = { ...prevContent, ...((patch.content as Record<string, unknown>) ?? {}) }
      list[index] = { ...prev, ...patch, content: newContent }
      return { messages: list }
    }),
  appendDelta: (index, key, delta) =>
    set((s) => {
      if (index < 0 || index >= s.messages.length) return s
      const list = [...s.messages]
      const prev = list[index]
      const prevContent = (prev?.content ?? {}) as Record<string, unknown>
      list[index] = { ...prev, content: { ...prevContent, [key]: String(prevContent[key] ?? '') + delta } }
      return { messages: list }
    }),
  reset: () => set({ messages: [], currentConvId: null, stage: '' }),
}))
