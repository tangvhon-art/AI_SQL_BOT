// 报告中心：列表 + 详情（AI 解读可按需生成，流式展示并回写报告）
import { useEffect, useState } from 'react'
import { Alert, Button, Card, Descriptions, Empty, Input, Popconfirm, Select, Space, Table, Tag, message } from 'antd'
import { DeleteOutlined, EyeOutlined, FileTextOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { client } from '../api/client'
import { postSseStream } from '../api/sse'
import { Dashboard, convertDashboardEvent } from '../components/dashboard/Dashboard'
import { AiInterpretation } from '../components/dashboard/AiInterpretation'
import type { InterpretationResult } from '../types/chart'

interface ReportItem {
  id: number
  title: string
  original_question: string
  remark: string
  created_at: string
}

export default function ReportCenterPage() {
  const navigate = useNavigate()
  const [items, setItems] = useState<ReportItem[]>([])
  const [loading, setLoading] = useState(false)
  const [keyword, setKeyword] = useState('')
  const [detail, setDetail] = useState<any>(null)
  // AI 解读状态：流式生成 + 结果（生成完成后服务端已回写报告）
  const [ai, setAi] = useState<{ loading: boolean; result?: InterpretationResult | null; rawText?: string; error?: string; templateName?: string }>({ loading: false })
  // AI解读提示词选择（Prompt 管理中 scene_type=ai_interpret）
  const [promptOptions, setPromptOptions] = useState<Array<{ id: number; name: string; is_default: boolean }>>([])
  const [selPromptId, setSelPromptId] = useState<number | null>(null)
  const effectivePromptId = selPromptId ?? promptOptions.find((p) => p.is_default)?.id ?? null

  useEffect(() => {
    client.get<{ items: Array<{ id: number; name: string; is_default: boolean }> }>('/prompts', { params: { scene_type: 'ai_interpret' } })
      .then((r) => setPromptOptions(r.data.items || []))
      .catch(() => {})
  }, [])

  const runInterpret = async () => {
    if (!detail || ai.loading) return
    const data = (detail.dashboard_data ?? {}) as Record<string, unknown>
    if (!Object.keys(data).length) return
    setAi({ loading: true })
    try {
      await postSseStream('/api/v1/ai/interpret', {
        question: String(detail.original_question ?? ''),
        data,
        report_id: detail.id,
        prompt_template_id: effectivePromptId ?? undefined,
      }, {
        onAiInterpretationStart: (p) => setAi((s) => ({ ...s, templateName: String(p.template_name ?? '') })),
        onAiInterpretation: (p) => setAi((s) => ({ ...s, loading: true, rawText: String(p.raw_text ?? '') })),
        onAiInterpretationDone: (p) => {
          const res = (p as { result?: InterpretationResult }).result
          setAi({ loading: false, result: res ?? null, rawText: String(p.raw_text ?? '') })
          if (res) {
            setDetail((d: any) => ({ ...d, ai_interpretation: res, interpretation_text: String((p as { raw_text?: string }).raw_text ?? '') }))
          }
        },
        onAiInterpretationError: (p) => setAi({ loading: false, error: String(p.error ?? '解读失败') }),
        onError: (p) => setAi({ loading: false, error: String((p as { msg?: string }).msg ?? '解读失败') }),
      })
    } catch (e) {
      setAi({ loading: false, error: e instanceof Error ? e.message : '解读失败' })
    }
  }

  const load = async () => {
    setLoading(true)
    try {
      const r = await client.get('/reports', { params: { keyword, page: 1, page_size: 50 } })
      setItems(r.data.items || [])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [keyword])

  const handleView = async (id: number) => {
    const r = await client.get(`/reports/${id}`)
    setDetail(r.data.item)
  }

  const handleDelete = async (id: number) => {
    await client.delete(`/reports/${id}`)
    message.success('已删除')
    if (detail?.id === id) setDetail(null)
    load()
  }

  const columns = [
    { title: '标题', dataIndex: 'title', key: 'title', render: (t: string, r: ReportItem) => (
      <a onClick={() => handleView(r.id)}><FileTextOutlined style={{ marginRight: 6 }} />{t}</a>
    )},
    { title: '原始问题', dataIndex: 'original_question', key: 'q', ellipsis: true },
    { title: '创建时间', dataIndex: 'created_at', key: 'created_at', width: 180, render: (v: string) => v?.slice(0, 19).replace('T', ' ') },
    { title: '操作', key: 'actions', width: 150, render: (_: any, r: ReportItem) => (
      <Space>
        <Button size="small" type="link" icon={<EyeOutlined />} onClick={() => handleView(r.id)}>查看</Button>
        <Popconfirm title="确认删除？" onConfirm={() => handleDelete(r.id)}><Button size="small" type="link" danger icon={<DeleteOutlined />}>删除</Button></Popconfirm>
      </Space>
    )},
  ]

  // 详情视图
  if (detail) {
    const dashboardData = detail.dashboard_data ? convertDashboardEvent(detail.dashboard_data) : null
    const interpretation = detail.ai_interpretation as InterpretationResult | undefined
    return (
      <div style={{ padding: 16 }}>
        <Card
          title={<Space><FileTextOutlined />{detail.title}</Space>}
          extra={<Button onClick={() => setDetail(null)}>返回列表</Button>}
        >
          <Descriptions size="small" column={3} style={{ marginBottom: 16 }}>
            <Descriptions.Item label="原始问题">{detail.original_question}</Descriptions.Item>
            <Descriptions.Item label="创建时间">{detail.created_at?.slice(0, 19).replace('T', ' ')}</Descriptions.Item>
            <Descriptions.Item label="备注">{detail.remark || '-'}</Descriptions.Item>
          </Descriptions>
          {dashboardData ? (
            <Dashboard data={dashboardData} showToolbar={false} />
          ) : <Empty description="无Dashboard数据" />}
          {ai.error ? (
            <Alert type="error" showIcon message={ai.error} style={{ marginTop: 16 }} />
          ) : null}
          {(() => {
            const hasSaved = !!(interpretation || detail.interpretation_text)
            const showInterp = ai.loading || ai.result || ai.rawText || hasSaved
            if (!showInterp) {
              // 尚无解读：提供「AI解读」按钮 + 提示词选择（需有可解读数据）
              return dashboardData ? (
                <div style={{ marginTop: 16 }}>
                  <Space size={8}>
                    <Select
                      style={{ minWidth: 150 }}
                      placeholder="提示词（默认）"
                      value={effectivePromptId ?? undefined}
                      onChange={setSelPromptId}
                      options={promptOptions.map((p) => ({ label: p.name, value: p.id }))}
                      allowClear
                    />
                    <Button type="primary" icon={<ThunderboltOutlined />} loading={ai.loading} onClick={runInterpret}>
                      AI解读
                    </Button>
                  </Space>
                </div>
              ) : null
            }
            return (
              <div style={{ marginTop: 16 }}>
                {dashboardData && (
                  <div style={{ marginBottom: 8 }}>
                    <Select
                      size="small"
                      style={{ minWidth: 150 }}
                      placeholder="提示词（默认）"
                      value={effectivePromptId ?? undefined}
                      onChange={setSelPromptId}
                      options={promptOptions.map((p) => ({ label: p.name, value: p.id }))}
                      allowClear
                    />
                  </div>
                )}
                <AiInterpretation
                  result={ai.result ?? interpretation}
                  loading={ai.loading}
                  rawText={ai.rawText ?? detail.interpretation_text}
                  templateName={ai.templateName}
                  onRegenerate={dashboardData ? runInterpret : undefined}
                />
              </div>
            )
          })()}
        </Card>
      </div>
    )
  }

  return (
    <div style={{ padding: 16 }}>
      <Card
        title="报告中心"
        extra={<Input.Search placeholder="搜索标题" allowClear style={{ width: 240 }} onSearch={setKeyword} />}
      >
        <Table rowKey="id" columns={columns} dataSource={items} loading={loading} pagination={{ pageSize: 10 }} />
      </Card>
    </div>
  )
}
