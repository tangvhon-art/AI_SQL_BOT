// SSE 问数客户端：解析事件流并回调
import { TOKEN_KEY } from './client'

export interface SSEHandlers {
  onProgress?: (stage: string, msg: string) => void
  onSql?: (payload: { sql: string; dialect: string; permission: string }) => void
  onExecute?: (payload: Record<string, unknown>) => void
  onTable?: (payload: { columns: string[]; rows: unknown[][]; total: number; truncated?: boolean }) => void
  onChart?: (payload: Record<string, unknown>) => void
  // AI 问数重构：spec（我理解的问题）/ trace（口径溯源）/ summary（四层结论+异常+澄清）
  onSpec?: (spec: Record<string, unknown>) => void
  onTrace?: (trace: Record<string, unknown>) => void
  onSummary?: (payload: {
    text: string
    sections?: Record<string, string>
    anomalies?: Array<{ type: string; desc: string }>
    candidates?: Array<{ table: string; comment?: string; name?: string; column?: string }>
    kind?: string
    original_question?: string
    missing?: string[]
  }) => void
  onError?: (payload: { code: string; msg: string }) => void
  onDone?: () => void
  onConvId?: (convId: number) => void
  onStream?: (delta: string) => void
  onThinking?: (delta: string) => void
  // 文件问答
  onAnswer?: (delta: string) => void
  onReferences?: (references: Array<Record<string, unknown>>) => void
  // 多查询（C11）
  onMultiSpec?: (payload: Record<string, unknown>) => void
  onSubSql?: (payload: Record<string, unknown>) => void
  onSubResult?: (payload: Record<string, unknown>) => void
  onSubError?: (payload: Record<string, unknown>) => void
  onDashboard?: (payload: Record<string, unknown>) => void
  // AI 解读
  onAiInterpretationStart?: (payload: Record<string, unknown>) => void
  onAiInterpretation?: (payload: Record<string, unknown>) => void
  onAiInterpretationDone?: (payload: Record<string, unknown>) => void
  onAiInterpretationError?: (payload: Record<string, unknown>) => void
}

export async function postChatStream(
  body: {
    conversation_id?: number | null
    question: string
    datasource_id?: number | null
    schema_name?: string | null
    model_id?: number | null
    file_ids?: string[] | null
    doc_session_id?: string | null
  },
  handlers: SSEHandlers,
): Promise<void> {
  const token = localStorage.getItem(TOKEN_KEY)
  const resp = await fetch('/api/v1/chat', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token ?? ''}`,
    },
    body: JSON.stringify(body),
  })
  if (!resp.ok || !resp.body) {
    handlers.onError?.({ code: 'HTTP', msg: `请求失败: ${resp.status}` })
    return
  }
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  // 解析 SSE：按空行分隔 event/data
  const dispatch = (block: string) => {
    let event = 'message'
    let data = ''
    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) data += line.slice(5).trim()
    }
    if (!data) return
    let payload: Record<string, unknown>
    try {
      payload = JSON.parse(data)
    } catch {
      return
    }
    switch (event) {
      case 'progress': handlers.onProgress?.(String(payload.stage ?? ''), String(payload.msg ?? '')); break
      case 'sql': handlers.onSql?.(payload as never); break
      case 'execute': handlers.onExecute?.(payload); break
      case 'table': handlers.onTable?.(payload as never); break
      case 'chart': handlers.onChart?.(payload); break
      case 'spec': handlers.onSpec?.(payload); break
      case 'trace': handlers.onTrace?.(payload); break
      case 'summary': handlers.onSummary?.(payload as never); break
      case 'error': handlers.onError?.(payload as never); break
      case 'done': handlers.onDone?.(); break
      case 'conv_id': handlers.onConvId?.(Number(payload.conversation_id)); break
      case 'stream': handlers.onStream?.(String(payload.delta ?? '')); break
      case 'thinking': handlers.onThinking?.(String(payload.delta ?? '')); break
      case 'answer': handlers.onAnswer?.(String(payload.delta ?? '')); break
      case 'references': handlers.onReferences?.(payload.references as never); break
      // 多查询
      case 'multi_spec': handlers.onMultiSpec?.(payload); break
      case 'sub_sql': handlers.onSubSql?.(payload); break
      case 'sub_result': handlers.onSubResult?.(payload); break
      case 'sub_error': handlers.onSubError?.(payload); break
      case 'dashboard': handlers.onDashboard?.(payload); break
      // AI 解读
      case 'ai_interpretation_start': handlers.onAiInterpretationStart?.(payload); break
      case 'ai_interpretation': handlers.onAiInterpretation?.(payload); break
      case 'ai_interpretation_done': handlers.onAiInterpretationDone?.(payload); break
      case 'ai_interpretation_error': handlers.onAiInterpretationError?.(payload); break
    }
  }
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let idx: number
    while ((idx = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, idx)
      buffer = buffer.slice(idx + 2)
      dispatch(block)
    }
  }
  if (buffer.trim()) dispatch(buffer)
}
