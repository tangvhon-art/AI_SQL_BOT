// 报告中心：列表 + 详情（AI 解读可按需生成，流式展示并回写报告）
import { useEffect, useState } from 'react'
import { Alert, Button, Card, Descriptions, Empty, Input, Modal, Popconfirm, Select, Space, Table, Tag, message } from 'antd'
import { DeleteOutlined, EyeOutlined, FileTextOutlined, ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons'
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
  const [regeneratingId, setRegeneratingId] = useState<number | null>(null)
  // 单个图表重新生成：记录正在重试的卡片 subId
  const [retryingCardId, setRetryingCardId] = useState<string | null>(null)
  // 数据源选择（旧报告无 datasource_id 时使用）
  const [dsList, setDsList] = useState<Array<{ id: number; name: string }>>([])
  const [dsModalOpen, setDsModalOpen] = useState(false)
  const [pendingRetrySubId, setPendingRetrySubId] = useState<string | null>(null)
  const [selectedDsId, setSelectedDsId] = useState<number | null>(null)
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

  // 获取数据源列表（用于旧报告重新生成时选择）
  useEffect(() => {
    client.get('/datasources', { params: { page: 1, page_size: 100 } })
      .then((r) => setDsList(r.data.items || r.data || []))
      .catch(() => {})
  }, [])

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

  // 重新生成报告：优先用洞察配置快照，其次从多查询快照构建配置
  const handleRegenerate = async (id: number) => {
    if (regeneratingId) return
    setRegeneratingId(id)
    try {
      const r = await client.get(`/reports/${id}`)
      const item = r.data.item || {}
      let cfg = item.insight_config
      const templateId = item.template_id
      // 无洞察配置时，从多查询快照构建
      if (!cfg || !cfg.items?.length) {
        const mq = item.multi_query_spec || {}
        const subQueries = mq.sub_queries || []
        const dsId = mq.datasource_id || item.datasource_id
        const cards = item.dashboard_data?.cards || []
        if (subQueries.length && dsId) {
          cfg = {
            purpose: item.original_question || '',
            datasource_id: dsId,
            items: subQueries.map((sq: any, i: number) => ({
              id: sq.sub_id || `item${i + 1}`,
              question: sq.question || item.original_question || '',
              chart_type: cards[i]?.chart_type || sq.chart_hint || 'bar',
              enabled: true,
              title: cards[i]?.title || sq.question?.slice(0, 20) || '',
            })),
          }
        }
      }
      if (!cfg || !cfg.items?.length) {
        message.warning('该报告无可重新生成的配置')
        return
      }
      const enabled = cfg.items.filter((i: any) => i.enabled !== false)
      if (!enabled.length) {
        message.warning('该报告没有启用的分析项')
        return
      }
      await client.post('/insight/execute', {
        config: { ...cfg, items: enabled },
        template_id: templateId ?? undefined,
      })
      load()
    } catch (e: any) {
      console.error('[regenerate] 重新生成失败:', e?.response?.data || e.message)
    } finally {
      setRegeneratingId(null)
    }
  }

  // 单个图表重新生成：先获取最新报告快照，再调用预览接口重新执行该图表
  const handleRetryCard = async (subId: string) => {
    console.log('[retry] 入口被调用，subId=', subId)
    if (retryingCardId || !detail) return
    setRetryingCardId(subId)
    // 立即把卡片状态改为 loading，给用户明确反馈
    const origCards = detail.dashboard_data?.cards || []
    const origIdx = origCards.findIndex((c: any) => (c.sub_id || c.id) === subId)
    if (origIdx >= 0) {
      const loadingCards = [...origCards]
      loadingCards[origIdx] = { ...loadingCards[origIdx], status: 'loading', error: undefined }
      setDetail((d: any) => ({ ...d, dashboard_data: { ...d.dashboard_data, cards: loadingCards } }))
    }
    try {
      // 1. 获取最新报告详情（快照），确保配置完整
      console.log('[retry] 获取报告详情快照，id=', detail.id)
      const r0 = await client.get(`/reports/${detail.id}`)
      const fresh = r0.data.item || {}
      console.log('[retry] 快照获取成功，insight_config=', !!fresh.insight_config, 'multi_query_spec=', !!fresh.multi_query_spec, 'mq.datasource_id=', fresh.multi_query_spec?.datasource_id)
      const cards = fresh.dashboard_data?.cards || []

      // 2. 多种方式匹配卡片索引
      let idx = cards.findIndex((c: any) => (c.sub_id || c.id) === subId)
      if (idx < 0) idx = cards.findIndex((c: any) => c.status === 'error')
      if (idx < 0) {
        const m = /card-(\d+)/.exec(subId || '')
        if (m) idx = parseInt(m[1], 10)
      }
      if (idx < 0 || idx >= cards.length) {
        console.warn('[retry] 未找到卡片索引，subId=', subId)
        throw new Error('未找到对应卡片')
      }

      // 3. 优先用洞察配置快照，其次从多查询快照构建配置
      let cfg = fresh.insight_config
      let item = cfg?.items?.[idx]
      if (!item) {
        const mq = fresh.multi_query_spec || {}
        const subQueries = mq.sub_queries || []
        const dsId = mq.datasource_id || fresh.datasource_id
        const sq = subQueries[idx] || subQueries[0]
        if (sq && dsId) {
          const card = cards[idx] || {}
          cfg = {
            purpose: fresh.original_question || sq.question || '',
            datasource_id: dsId,
            items: [{
              id: sq.sub_id || `item${idx + 1}`,
              question: sq.question || fresh.original_question || '',
              chart_type: card.chart_type || sq.chart_hint || 'bar',
              enabled: true,
              title: card.title || sq.question?.slice(0, 20) || '',
            }],
          }
          item = cfg.items[0]
          console.log('[retry] 从多查询快照构建配置，question=', item.question)
        }
      }
      if (!cfg || !item) {
        // 无配置快照时，尝试让用户选择数据源后重新生成
        const mq = fresh.multi_query_spec || {}
        const subQueries = mq.sub_queries || []
        const sq = subQueries[idx] || subQueries[0]
        if (sq && subQueries.length) {
          console.log('[retry] 无 datasource_id，打开数据源选择，subId=', subId, 'idx=', idx)
          setPendingRetrySubId(subId)
          setSelectedDsId(null)
          setDsModalOpen(true)
          return
        }
        console.warn('[retry] 无可用配置，idx=', idx, 'insight_config=', fresh.insight_config, 'multi_query_spec=', fresh.multi_query_spec)
        throw new Error('该报告缺少重新生成所需的配置快照')
      }

      // 4. 调用预览接口重新生成该图表
      console.log('[retry] 开始重新生成，idx=', idx, 'question=', item.question)
      const r = await client.post('/insight/preview', {
        config: { ...cfg, items: [{ ...item, enabled: true }] },
      })
      const newCards = r.data.cards || []
      console.log('[retry] 接口返回，cards=', newCards.length)
      if (newCards.length) {
        const updated = [...cards]
        updated[idx] = { ...updated[idx], ...newCards[0], status: newCards[0].status || 'success' }
        setDetail((d: any) => ({ ...d, dashboard_data: { ...d.dashboard_data, cards: updated } }))
      } else {
        throw new Error('接口返回空数据')
      }
    } catch (e: any) {
      const errMsg = e?.response?.data?.detail || e?.message || '重新生成失败'
      console.error('[retry] 重新生成失败:', errMsg)
      // 失败时恢复卡片为 error 状态并显示错误信息
      setDetail((d: any) => {
        const cs = d.dashboard_data?.cards || []
        const ei = cs.findIndex((c: any) => (c.sub_id || c.id) === subId)
        if (ei >= 0) {
          const updated = [...cs]
          updated[ei] = { ...updated[ei], status: 'error', error: errMsg }
          return { ...d, dashboard_data: { ...d.dashboard_data, cards: updated } }
        }
        return d
      })
    } finally {
      setRetryingCardId(null)
    }
  }

  // 用户选择数据源后，用选择的数据源重新生成当前图表
  const handleConfirmDs = async () => {
    if (!selectedDsId || !pendingRetrySubId || !detail) return
    setDsModalOpen(false)
    const subId = pendingRetrySubId
    setRetryingCardId(subId)
    const origCards = detail.dashboard_data?.cards || []
    const origIdx = origCards.findIndex((c: any) => (c.sub_id || c.id) === subId)
    if (origIdx >= 0) {
      const loadingCards = [...origCards]
      loadingCards[origIdx] = { ...loadingCards[origIdx], status: 'loading', error: undefined }
      setDetail((d: any) => ({ ...d, dashboard_data: { ...d.dashboard_data, cards: loadingCards } }))
    }
    try {
      const r0 = await client.get(`/reports/${detail.id}`)
      const fresh = r0.data.item || {}
      const cards = fresh.dashboard_data?.cards || []
      let idx = cards.findIndex((c: any) => (c.sub_id || c.id) === subId)
      if (idx < 0) idx = cards.findIndex((c: any) => c.status === 'error')
      if (idx < 0) {
        const m = /card-(\d+)/.exec(subId || '')
        if (m) idx = parseInt(m[1], 10)
      }
      if (idx < 0 || idx >= cards.length) throw new Error('未找到对应卡片')
      const mq = fresh.multi_query_spec || {}
      const subQueries = mq.sub_queries || []
      const sq = subQueries[idx] || subQueries[0]
      const card = cards[idx] || {}
      const cfg = {
        purpose: fresh.original_question || sq.question || '',
        datasource_id: selectedDsId,
        items: [{
          id: sq.sub_id || `item${idx + 1}`,
          question: sq.question || fresh.original_question || '',
          chart_type: card.chart_type || sq.chart_hint || 'bar',
          enabled: true,
          title: card.title || sq.question?.slice(0, 20) || '',
        }],
      }
      console.log('[retry][ds] 用选择的数据源重新生成，dsId=', selectedDsId, 'question=', cfg.items[0].question)
      const r = await client.post('/insight/preview', { config: cfg })
      const newCards = r.data.cards || []
      console.log('[retry][ds] 返回卡片:', JSON.stringify({columns: newCards[0]?.columns, rowsCount: newCards[0]?.rows?.length, chartType: newCards[0]?.chart_type, status: newCards[0]?.status}))
      if (newCards.length) {
        const updated = [...cards]
        updated[idx] = { ...updated[idx], ...newCards[0], status: newCards[0].status || 'success' }
        setDetail((d: any) => ({ ...d, dashboard_data: { ...d.dashboard_data, cards: updated } }))
      }
    } catch (e: any) {
      const errMsg = e?.response?.data?.detail || e?.message || '重新生成失败'
      console.error('[retry][ds] 失败:', errMsg)
      setDetail((d: any) => {
        const cs = d.dashboard_data?.cards || []
        const ei = cs.findIndex((c: any) => (c.sub_id || c.id) === subId)
        if (ei >= 0) {
          const updated = [...cs]
          updated[ei] = { ...updated[ei], status: 'error', error: errMsg }
          return { ...d, dashboard_data: { ...d.dashboard_data, cards: updated } }
        }
        return d
      })
    } finally {
      setRetryingCardId(null)
      setPendingRetrySubId(null)
    }
  }

  const columns = [
    { title: '标题', dataIndex: 'title', key: 'title', render: (t: string, r: ReportItem) => (
      <a onClick={() => handleView(r.id)}><FileTextOutlined style={{ marginRight: 6 }} />{t}</a>
    )},
    { title: '原始问题', dataIndex: 'original_question', key: 'q', ellipsis: true },
    { title: '创建时间', dataIndex: 'created_at', key: 'created_at', width: 180, render: (v: string) => v?.slice(0, 19).replace('T', ' ') },
    { title: '操作', key: 'actions', width: 240, align: 'left' as const, render: (_: any, r: ReportItem) => (
      <Space>
        <Button size="small" type="link" icon={<EyeOutlined />} onClick={() => handleView(r.id)}>查看</Button>
        <Button size="small" type="link" icon={<ReloadOutlined />} loading={regeneratingId === r.id} onClick={() => handleRegenerate(r.id)}>重新生成</Button>
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
            <Dashboard data={dashboardData} showToolbar={false} onRetryCard={handleRetryCard} retryDisabled={(id) => retryingCardId === id} />
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

        {/* 数据源选择 Modal（旧报告无 datasource_id 时，重新生成图表前选择数据源） */}
        <Modal
          title="选择数据源"
          open={dsModalOpen}
          onOk={handleConfirmDs}
          onCancel={() => { setDsModalOpen(false); setPendingRetrySubId(null) }}
          okText="确定"
          cancelText="取消"
          okButtonProps={{ disabled: !selectedDsId }}
        >
          <p style={{ marginBottom: 12, color: '#8c8c8c' }}>该报告未保存数据源信息，请选择数据源后重新生成图表：</p>
          <Select
            style={{ width: '100%' }}
            placeholder="请选择数据源"
            value={selectedDsId ?? undefined}
            onChange={(v) => setSelectedDsId(v)}
            options={dsList.map((d) => ({ label: d.name, value: d.id }))}
          />
        </Modal>
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
