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
  reset: () => set({ messages: [], currentConvId: null, stage: '' }),
}))
