// 对话页（豆包式全屏聊天）：历史收起到抽屉、消息流居中、底部多行输入、发送后追加到底部
import { useEffect, useRef, useState } from 'react'
import {
 Button, Drawer, Dropdown, Input, List, Space, Spin, Tag, Tooltip, Typography,
} from 'antd'
import {
  HistoryOutlined, PlusOutlined, ArrowUpOutlined, DownOutlined,
} from '@ant-design/icons'
import { client, errMsg } from '../api/client'
import { postChatStream } from '../api/sse'
import { useChatStore } from '../stores/chat'
import MessageCard from '../components/MessageCard'
import type { ChatMsg, Conversation, Datasource } from '../types'
import { GlassSelect } from '../ui'
import { useMessageApi } from '../hooks/useMessageApi'

// 问数节点步骤：按后端 progress 动态展示（意图识别/知识检索/SQL 链路均逐节点显示）
interface StepState { label: string; done: boolean; active: boolean }

export default function ChatPage() {
  const { msgApi, ctx, toastError } = useMessageApi()
  const [steps, setSteps] = useState<StepState[]>([])
  const [question, setQuestion] = useState('')
  const [dsList, setDsList] = useState<Datasource[]>([])
  const [dsId, setDsId] = useState<number | null>(null)
  const [schemaList, setSchemaList] = useState<string[]>([])
  const [schemaName, setSchemaName] = useState<string | null>(null)
  const [modelList, setModelList] = useState<Array<{ id: number; name: string; model_name: string; is_default: boolean; scene: string }>>([])
  const [modelId, setModelId] = useState<number | null>(null)
  const [histOpen, setHistOpen] = useState(false)
  const {
    conversations, currentConvId, messages, streaming, stage,
    setConversations, setCurrentConvId, setMessages, appendUser, appendStreamMsg, setStreaming, setStage,
  } = useChatStore()
  const bottomRef = useRef<HTMLDivElement>(null)

  // ========== 文件问答 ==========
  interface DocFile {
    file_id: string
    name: string
    file_type: string
    file_size: number
    status: 'pending' | 'parsing' | 'ready' | 'failed'
    fail_reason?: string | null
    chunk_count?: number
  }
  const [docFiles, setDocFiles] = useState<DocFile[]>([])
  const docSessionIdRef = useRef<string>('')
  const fileInputRef = useRef<HTMLInputElement>(null)
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const ensureDocSessionId = () => {
    if (!docSessionIdRef.current) {
      docSessionIdRef.current = `doc_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`
    }
    return docSessionIdRef.current
  }

  const resetDocSession = () => {
    docSessionIdRef.current = ''
    setDocFiles([])
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current)
      pollTimerRef.current = null
    }
  }

  const uploadDocFiles = async (files: FileList) => {
    if (!files || files.length === 0) return
    const sid = ensureDocSessionId()
    const formData = new FormData()
    Array.from(files).forEach((f) => formData.append('files', f))
    formData.append('session_id', sid)
    try {
      const r = await client.post('/doc-chat/upload', formData)
      const data = r.data
      const uploaded: DocFile[] = (data.files || []).filter((f: DocFile) => f.file_id)
      if (uploaded.length > 0) {
        setDocFiles((prev) => [...prev, ...uploaded])
        startDocPolling(sid)
      }
      const failed = (data.files || []).filter((f: DocFile) => !f.file_id)
      if (failed.length > 0) {
        msgApi.warning(failed.map((f: DocFile) => `${f.name}: ${f.fail_reason}`).join('；'))
      }
    } catch (e) {
      msgApi.error('文件上传失败')
    }
  }

  const startDocPolling = (sid: string) => {
    if (pollTimerRef.current) clearInterval(pollTimerRef.current)
    pollTimerRef.current = setInterval(async () => {
      setDocFiles((prev) => {
        const parsing = prev.filter((f) => f.status === 'parsing' || f.status === 'pending')
        if (parsing.length === 0) {
          if (pollTimerRef.current) {
            clearInterval(pollTimerRef.current)
            pollTimerRef.current = null
          }
          return prev
        }
        return prev
      })
      try {
        const currentFiles = docFilesRef.current
        const ids = currentFiles.filter((f) => f.status === 'parsing' || f.status === 'pending').map((f) => f.file_id).join(',')
        if (!ids) return
        const r = await client.get('/doc-chat/status', { params: { session_id: sid, file_ids: ids } })
        const statusMap = new Map<string, DocFile>((r.data.files || []).map((f: DocFile) => [f.file_id, f]))
        setDocFiles((prev) => prev.map((f) => statusMap.get(f.file_id) || f))
      } catch { /* 忽略轮询错误 */ }
    }, 1500)
  }

  // 用 ref 存最新 docFiles，供轮询闭包读取
  const docFilesRef = useRef<DocFile[]>([])
  useEffect(() => { docFilesRef.current = docFiles }, [docFiles])

  const removeDocFile = async (fileId: string) => {
    const sid = docSessionIdRef.current
    setDocFiles((prev) => prev.filter((f) => f.file_id !== fileId))
    if (sid) {
      try {
        await client.delete(`/doc-chat/${fileId}`, { params: { session_id: sid } })
      } catch { /* 忽略 */ }
    }
  }

  const handleFileInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) uploadDocFiles(e.target.files)
    e.target.value = ''
  }

  const formatFileSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes}B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`
    return `${(bytes / 1024 / 1024).toFixed(1)}MB`
  }

  const fileIcon = (type: string) => {
    if (['pdf'].includes(type)) return '📄'
    if (['docx'].includes(type)) return '📝'
    if (['xlsx', 'csv'].includes(type)) return '📊'
    if (['png', 'jpg', 'jpeg'].includes(type)) return '🖼️'
    return '📎'
  }

  useEffect(() => {
    loadConversations()
    client.get<Datasource[]>('/datasources').then((r) => setDsList(r.data)).catch(() => {})
    // 加载可用大模型（sql 场景），默认选中 is_default
    client.get<Array<{ id: number; name: string; model_name: string; is_default: boolean; scene: string }>>('/models')
      .then((r) => {
        const sqlModels = (r.data || []).filter((m) => !m.scene || m.scene === 'sql')
        setModelList(sqlModels)
        const def = sqlModels.find((m) => m.is_default) || sqlModels[0]
        if (def) setModelId(def.id)
      })
      .catch(() => {})
    // 切换页面回来时：若有当前会话且不在流式输出中，重新加载最新消息（避免全局 store 残留错位状态）
    if (currentConvId && !streaming) {
      client.get<ChatMsg[]>(`/conversations/${currentConvId}/messages`)
        .then((r) => setMessages(r.data))
        .catch(() => {})
    }
  }, [])

  // 数据源切换 → 拉取该数据源的 schema（项目/库）清单，供查询范围选择（R3）
  useEffect(() => {
    setSchemaList([])
    setSchemaName(null)
    if (dsId == null) return
    client.get<string[]>(`/datasources/${dsId}/schemas`).then((r) => {
      setSchemaList(r.data || [])
      if ((r.data || []).length === 1) setSchemaName(r.data[0])
    }).catch(() => {})
  }, [dsId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages, streaming, stage])

  const loadConversations = async () => {
    try {
      const r = await client.get<Conversation[]>('/conversations')
      setConversations(r.data)
    } catch { /* 忽略 */ }
  }

  const enterConversation = async (id: number) => {
    setHistOpen(false)
    setCurrentConvId(id)
    try {
      const r = await client.get<ChatMsg[]>(`/conversations/${id}/messages`)
      setMessages(r.data)
    } catch (e) {
      toastError(e)
    }
  }

  const newConversation = () => {
    setMessages([])
    setCurrentConvId(null)
    setStage('')
    setSteps([])
    resetDocSession()
    loadConversations()
    setHistOpen(false)
  }

  const updateSteps = (m: string) => {
    const clean = m.replace(/…$/, '')
    if (!clean || clean.includes('理解问题')) return
    setSteps((prev) => {
      if (prev.some((p) => p.label === clean)) return prev
      const next = [...prev, { label: clean, done: false, active: true }]
      return next.map((st, i) => (i === next.length - 1 ? st : { ...st, done: true, active: false }))
    })
  }

  const send = async (text?: string) => {
    const q = (text ?? question).trim()
    if (!q || streaming) return
    // 文件问答：检查附件状态
    const readyFileIds = docFiles.filter((f) => f.status === 'ready').map((f) => f.file_id)
    const parsingFiles = docFiles.filter((f) => f.status === 'parsing' || f.status === 'pending')
    const hasDocFiles = docFiles.length > 0
    if (hasDocFiles && readyFileIds.length === 0) {
      if (parsingFiles.length > 0) msgApi.warning('文件解析中，请稍候再试')
      else msgApi.warning('没有可用的文件，请重新上传')
      return
    }
    appendUser(q)
    setQuestion('')
    setStreaming(true)
    setStage('准备中…')
    setSteps([])
    const partial: ChatMsg = { role: 'assistant', content_type: 'progress', content: { msg: '' } }
    appendStreamMsg(partial)
    // 公共方法：合并更新最后一条 assistant 消息（content 深度合并，供 SSE 各事件分片写入）
    const update = (patch: Partial<ChatMsg>) => {
      const idx = useChatStore.getState().messages.length - 1
      useChatStore.getState().patchMessage(idx, patch)
    }
    // 公共方法：向最后一条消息的 content[key] 追加增量文本（打字机/思考流/回答流）
    const appendDelta = (key: string, delta: string) => {
      const idx = useChatStore.getState().messages.length - 1
      useChatStore.getState().appendDelta(idx, key, delta)
    }
    try {
      await postChatStream(
        {
          conversation_id: currentConvId, question: q, datasource_id: dsId,
          schema_name: schemaName ?? undefined, model_id: modelId ?? undefined,
          file_ids: hasDocFiles ? readyFileIds : undefined,
          doc_session_id: hasDocFiles ? docSessionIdRef.current : undefined,
        },
        {
          onConvId: (id) => {
            // 新对话首个事件：绑定 conversation_id，确保澄清确认等多轮交互复用同一对话（连续性）
            if (!currentConvId) setCurrentConvId(id)
          },
          onStream: (delta) => {
            // 流式输出：逐块追加到当前 assistant 消息的 stream_text（打字机效果）
            appendDelta('stream_text', delta)
          },
          onThinking: (delta) => {
            // 思考过程：逐块追加到当前消息的 thinking_text（最终回答时可折叠展示）
            appendDelta('thinking_text', delta)
          },
          onAnswer: (delta) => {
            // 文件问答：流式累积回答文本到 content.text
            appendDelta('text', delta)
            update({ content_type: 'result', content: { mode: 'doc_chat' } as never })
          },
          onReferences: (refs) => {
            update({ content_type: 'result', content: { references: refs, mode: 'doc_chat' } as never })
          },
          onProgress: (_stage, m) => {
            setStage(m)
            updateSteps(m)
            update({ content_type: 'progress', content: { msg: m } })
          },
          onSql: (p) => {
            update({ content_type: 'result', content: { sql: p.sql, permission: p.permission } as never })
          },
          onExecute: (p) => update({ content_type: 'result', content: { executing: true, permission: p.permission } as never }),
          onTable: (p) => {
            update({
              content_type: 'result',
              content: { columns: p.columns, rows: p.rows, row_count: p.total, truncated: p.truncated } as never,
            })
          },
          onChart: (p) => {
            update({ content_type: 'result', content: { chart: p } as never })
          },
          onSpec: (spec) => {
            // AI 问数重构：后端下发 QuerySpec（"我理解的问题"），前端可展示可纠错
            update({ content_type: 'result', content: { query_spec: spec } as never })
          },
          onTrace: (trace) => {
            // 口径与溯源：数据源表/映射/SQL/耗时，前端折叠面板展示
            update({ content_type: 'result', content: { trace } as never })
          },
          onSummary: (p) => {
            const candidates = (p as { candidates?: Array<{ table: string; comment?: string }> }).candidates
            const kind = (p as { kind?: string }).kind
            update({
              content_type: candidates?.length ? 'clarify' : 'result',
              content: {
                text: p.text,
                sections: p.sections ?? undefined,
                anomalies: p.anomalies ?? undefined,
                ...(candidates ? { candidates, clarify_kind: kind } : {}),
              } as never,
            })
          },
          onError: (p) => {
            update({ content_type: 'error', content: { msg: p.msg } as never })
          },
          onDone: () => {
            setStreaming(false)
            setStage('')
            // 结束：标记完成后短暂展示，然后隐藏进度步骤（不留在输入框上方）
            setSteps((prev) => prev.map((st) => ({ ...st, done: true, active: false })))
            setTimeout(() => setSteps([]), 1200)
            loadConversations()
          },
        },
      )
    } catch (e) {
      update({ content_type: 'error', content: { msg: errMsg(e) } as never })
      setStreaming(false)
      setStage('')
    }
  }

  const saveQuery = async (msg: ChatMsg) => {
    const name = window.prompt('保存为查询名称：', `查询 ${new Date().toLocaleString()}`)
    if (!name) return
    const sql = String((msg.content as Record<string, unknown>).sql ?? '')
    if (!sql) {
      msgApi.warning('该消息无可用 SQL')
      return
    }
    try {
      await client.post('/saved-queries', {
        name,
        sql_text: sql,
        params: [],
        chart_config: {},
      })
      msgApi.success('已保存，可在「保存查询」中复用')
    } catch (e) {
      toastError(e)
    }
  }

  const feedback = async (_msgId: number | undefined, fb: string) => {
    msgApi.success(fb === 'good' ? '感谢反馈' : '已记录反馈')
  }

  const fmt = (s?: string) => {
    if (!s) return ''
    const d = new Date(s)
    const now = new Date()
    const same = d.toDateString() === now.toDateString()
    return same ? d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
      : `${d.getMonth() + 1}/${d.getDate()} ${d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}`
  }

  // 输入框（豆包风格：大圆角容器 + 附件chip + 模型按钮 + 圆形发送），空状态/有消息两种布局复用
  const inputBox = (
    <>
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept=".pdf,.docx,.xlsx,.csv,.md,.markdown,.txt,.png,.jpg,.jpeg"
        style={{ display: 'none' }}
        onChange={handleFileInputChange}
      />
      <div className="glass-chat-input" style={{
        maxWidth: 880, margin: '0 auto',
        display: 'flex', flexDirection: 'column',
        background: '#f5f5f7',
        border: 'none',
        borderRadius: 24,
        padding: '14px 16px 10px 20px',
        boxShadow: 'none',
      }}>
        {/* 附件 chip 列表 */}
        {docFiles.length > 0 && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 10 }}>
            {docFiles.map((f) => (
              <div key={f.file_id} style={{
                display: 'flex', alignItems: 'center', gap: 6,
                background: f.status === 'failed' ? '#fff1f0' : '#ffffff',
                border: `1px solid ${f.status === 'failed' ? '#ffccc7' : '#e8e8e8'}`,
                borderRadius: 10, padding: '4px 8px 4px 10px',
                maxWidth: 260,
              }}>
                <span style={{ fontSize: 15 }}>{fileIcon(f.file_type)}</span>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{
                    fontSize: 12, color: '#333', fontWeight: 500,
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }} title={f.name}>
                    {f.name}
                  </div>
                  <div style={{ fontSize: 10, color: f.status === 'failed' ? '#ff4d4f' : '#999' }}>
                    {f.status === 'ready' && `${formatFileSize(f.file_size)} · 就绪`}
                    {f.status === 'parsing' && `${formatFileSize(f.file_size)} · 解析中…`}
                    {f.status === 'pending' && `${formatFileSize(f.file_size)} · 等待中…`}
                    {f.status === 'failed' && `解析失败：${f.fail_reason || '未知错误'}`}
                  </div>
                </div>
                {(f.status === 'parsing' || f.status === 'pending') && (
                  <Spin size="small" style={{ marginLeft: 4 }} />
                )}
                <button
                  type="button"
                  onClick={() => removeDocFile(f.file_id)}
                  style={{
                    background: 'transparent', border: 'none', cursor: 'pointer',
                    color: '#999', fontSize: 14, padding: '2px 4px', borderRadius: 4,
                    lineHeight: 1,
                  }}
                  onMouseEnter={(e) => { e.currentTarget.style.color = '#ff4d4f' }}
                  onMouseLeave={(e) => { e.currentTarget.style.color = '#999' }}
                  title="移除文件"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}
        <Input.TextArea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder={docFiles.length > 0 ? '基于上传文件提问，Enter 发送，Shift+Enter 换行' : '输入问题，Enter 发送，Shift+Enter 换行'}
          autoSize={{ minRows: 2, maxRows: 8 }}
          style={{ fontSize: 14, padding: '4px 0', resize: 'none' }}
          variant="borderless"
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              if (e.shiftKey) {
                e.preventDefault()
                const ta = e.currentTarget
                const start = ta.selectionStart ?? question.length
                const end = ta.selectionEnd ?? question.length
                const next = question.slice(0, start) + '\n' + question.slice(end)
                setQuestion(next)
                requestAnimationFrame(() => {
                  ta.selectionStart = ta.selectionEnd = start + 1
                })
              } else {
                e.preventDefault()
                send()
              }
            }
          }}
        />
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            {/* 「+」号上传文件按钮 */}
            <Tooltip title="上传文件（PDF/Word/Excel/图片），基于文件内容问答">
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                style={{
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  width: 30, height: 30, borderRadius: 8,
                  background: 'transparent', border: 'none', cursor: 'pointer',
                  color: '#666666', fontSize: 18, fontWeight: 400,
                  transition: 'background 0.15s',
                }}
                onMouseEnter={(e) => { e.currentTarget.style.background = 'rgba(0,0,0,0.06)' }}
                onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
              >
                +
              </button>
            </Tooltip>
            {modelList.length > 0 && (
              <Dropdown
                menu={{
                  items: modelList.map((m) => ({ key: String(m.id), label: m.name })),
                  selectable: true,
                  selectedKeys: modelId ? [String(modelId)] : [],
                  onClick: ({ key }) => setModelId(Number(key)),
                }}
                placement="topLeft"
              >
                <button type="button" style={{
                  display: 'flex', alignItems: 'center', gap: 4,
                  background: 'transparent', border: 'none', cursor: 'pointer',
                  color: '#666666', fontSize: 13, padding: '5px 10px', borderRadius: 8,
                  transition: 'background 0.15s',
                }}
                onMouseEnter={(e) => { e.currentTarget.style.background = 'rgba(0,0,0,0.05)' }}
                onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
                >
                  {modelList.find((m) => m.id === modelId)?.name || '选择模型'}
                  <DownOutlined style={{ fontSize: 10, opacity: 0.6 }} />
                </button>
              </Dropdown>
            )}
          </div>
          <Button
            type="primary"
            shape="circle"
            icon={<ArrowUpOutlined />}
            onClick={() => send()}
            loading={streaming}
            disabled={!question.trim()}
            style={{ width: 36, height: 36, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          />
        </div>
      </div>
      <div style={{ textAlign: 'center', marginTop: 8 }}>
        <Typography.Text style={{ fontSize: 11, color: 'rgba(0,0,0,.28)' }}>
          内容由 AI 生成，请核对后使用 · 受字段级权限约束
        </Typography.Text>
      </div>
    </>
  )

  // 空会话居中标题
  const emptyHero = (
    <div style={{ textAlign: 'center', color: 'rgba(0,0,0,.45)' }}>
      <div style={{
        width: 60, height: 60, borderRadius: 18, margin: '0 auto 18px',
        background: 'linear-gradient(135deg,#1677ff,#69b1ff)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        color: '#fff', fontSize: 28, fontWeight: 700,
        boxShadow: '0 12px 32px rgba(22,119,255,.35)',
      }}>
        Q
      </div>
      <Typography.Paragraph style={{ fontSize: 22, fontWeight: 600, color: 'rgba(0,0,0,.8)', marginBottom: 10 }}>
        AI 问数
      </Typography.Paragraph>
      <Typography.Paragraph style={{ fontSize: 14, color: 'rgba(0,0,0,.45)', marginBottom: 0 }}>
        输入业务问题，例如「各区域销售额对比」「近 30 天订单量趋势」
      </Typography.Paragraph>
    </div>
  )

  return (
    <div className="glass-app" style={{ height: 'calc(100vh - 112px)', minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden', borderRadius: 16 }}>
      {ctx}
      {/* ---------- 顶部工具栏 ---------- */}
      <div className="glass-header" style={{
        height: 56, flexShrink: 0, padding: '0 16px',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        position: 'relative', zIndex: 5, borderRadius: '16px 16px 0 0',
      }}>
        <Space size={10}>
          <Tooltip title="历史会话">
            <Button type="text" shape="circle" icon={<HistoryOutlined style={{ fontSize: 18 }} />} onClick={() => { loadConversations(); setHistOpen(true) }} />
          </Tooltip>
          <Tooltip title="新对话">
            <Button type="text" shape="circle" icon={<PlusOutlined style={{ fontSize: 17 }} />} onClick={newConversation} />
          </Tooltip>
        </Space>
        <div style={{ position: 'absolute', left: '50%', transform: 'translateX(-50%)', display: 'flex', alignItems: 'center', gap: 8 }}>
          <Typography.Text strong style={{ fontSize: 15 }}>AI 问数</Typography.Text>
          {streaming ? <Tag color="processing" style={{ marginInlineEnd: 0 }}>{stage || '处理中'}</Tag> : null}
        </div>
        <GlassSelect
          size="small"
          style={{ width: 200 }}
          placeholder="选择数据源（默认第一个已同步）"
          value={dsId ?? undefined}
          onChange={setDsId}
          allowClear
          options={dsList.map((d) => ({ label: `${d.name}(${d.type})`, value: d.id }))}
        />
        {schemaList.length > 1 ? (
          <GlassSelect
            size="small"
            style={{ width: 160 }}
            placeholder="项目/库（可选）"
            value={schemaName ?? undefined}
            onChange={setSchemaName}
            allowClear
            options={schemaList.map((s) => ({ label: s, value: s }))}
          />
        ) : null}
      </div>

      {/* ---------- 历史会话抽屉 ---------- */}
      <Drawer
        title={
          <Space>
            <span>历史会话</span>
            <Tag style={{ marginInlineEnd: 0 }}>{conversations.length}</Tag>
          </Space>
        }
        placement="left"
        width={320}
        open={histOpen}
        onClose={() => setHistOpen(false)}
        styles={{ body: { padding: '8px 12px' } }}
      >
        <Button type="primary" block icon={<PlusOutlined />} onClick={newConversation} style={{ marginBottom: 12, borderRadius: 10 }}>
          新对话
        </Button>
        <List
          size="small"
          dataSource={conversations}
          renderItem={(c) => (
            <List.Item
              onClick={() => enterConversation(c.id)}
              style={{
                cursor: 'pointer',
                borderRadius: 10,
                padding: '10px 12px',
                marginBottom: 4,
                background: currentConvId === c.id ? 'rgba(22,119,255,.1)' : undefined,
                border: currentConvId === c.id ? '1px solid rgba(22,119,255,.3)' : '1px solid transparent',
              }}
            >
              <div style={{ width: '100%', overflow: 'hidden' }}>
                <Typography.Text ellipsis style={{ fontSize: 13, display: 'block' }}>{c.title}</Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 11 }}>{fmt(c.create_time)}</Typography.Text>
              </div>
            </List.Item>
          )}
        />
      </Drawer>

      {/* ---------- 主体：空会话整体居中 / 有消息时消息在上输入框贴底 ---------- */}
      {messages.length === 0 ? (
        <div style={{
          flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column',
          justifyContent: 'center', alignItems: 'center', padding: '0 16px', gap: 36,
        }}>
          {emptyHero}
          <div style={{ width: '100%', maxWidth: 880 }}>{inputBox}</div>
        </div>
      ) : (
        <>
          <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '24px 16px 8px' }}>
            <div style={{ maxWidth: 880, margin: '0 auto', width: '100%' }}>
              {messages.map((m, i) => (
                <MessageCard
                  key={m.id ?? `msg-${i}`}
                  msg={m}
                  onSaveQuery={saveQuery}
                  onFeedback={feedback}
                  onClarifyConfirm={(tables) => {
                    send(`已确认查询表：${tables.join('、')}`)
                  }}
                />
              ))}
              <div ref={bottomRef} style={{ height: 4 }} />
            </div>
          </div>
          <div style={{ flexShrink: 0, padding: '12px 16px 20px' }}>{inputBox}</div>
        </>
      )}
    </div>
  )
}
