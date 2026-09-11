// 洞察分析：六步向导
import { useEffect, useRef, useState } from 'react'
import { Button, Card, Input, Select, Steps, Space, List, Tag, Switch, message, Modal, Empty, Spin, Collapse, Alert, Tabs, Popconfirm } from 'antd'
import { ThunderboltOutlined, SaveOutlined, PlayCircleOutlined, ReloadOutlined, ClockCircleOutlined, PlusOutlined, PauseOutlined, DeleteOutlined, EditOutlined, EyeOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import * as echarts from 'echarts'
import { client } from '../api/client'
import { buildChartOption } from '../utils/chart-factory'
import type { ChartDataset, ChartType } from '../types/chart'

const { TextArea } = Input

interface InsightItem {
  id: string
  title: string
  question: string
  chart_type: string
  enabled: boolean
}

interface PreviewCard {
  id: string
  title: string
  question: string
  chart_type: string
  sql: string
  columns: string[]
  rows: any[][]
  status: 'pending' | 'success' | 'error'
  error: string
}

interface InsightTemplateItem {
  id: number
  name: string
  description: string
  purpose: string
  datasource_id: number
  config: { purpose: string; datasource_id: number; model_id?: number | null; items: InsightItem[] }
  created_at: string
}

const CRON_PRESETS = [
  { label: '每天 09:00', value: '0 9 * * *' },
  { label: '每天 18:00', value: '0 18 * * *' },
  { label: '每周一 09:00', value: '0 9 * * 1' },
  { label: '每小时整点', value: '0 * * * *' },
  { label: '自定义', value: 'custom' },
]

const CHART_OPTIONS = [
  { value: 'bar', label: '柱状图' }, { value: 'line', label: '折线图' },
  { value: 'pie', label: '饼图' }, { value: 'radar', label: '雷达图' },
  { value: 'combo', label: '组合图' }, { value: 'stack_bar', label: '堆叠柱状图' },
  { value: 'rank', label: '排行榜' }, { value: 'kpi', label: 'KPI卡片' },
  { value: 'table', label: '表格' },
]

export default function InsightPage() {
  const navigate = useNavigate()
  const [current, setCurrent] = useState(0)
  const [purpose, setPurpose] = useState('')
  const [datasourceId, setDatasourceId] = useState<number | null>(null)
  const [datasources, setDatasources] = useState<any[]>([])
  const [items, setItems] = useState<InsightItem[]>([])
  const [loading, setLoading] = useState(false)
  const [templateName, setTemplateName] = useState('')
  const [previewCards, setPreviewCards] = useState<PreviewCard[]>([])
  const [previewLoading, setPreviewLoading] = useState(false)
  // 模型选择
  const [modelId, setModelId] = useState<number | null>(null)
  const [models, setModels] = useState<any[]>([])
  // 列表视图
  const [viewMode, setViewMode] = useState<'list' | 'wizard'>('list')
  const [templates, setTemplates] = useState<InsightTemplateItem[]>([])
  const [templateLoading, setTemplateLoading] = useState(false)
  const [editingTemplate, setEditingTemplate] = useState<InsightTemplateItem | null>(null)
  const [templateSearch, setTemplateSearch] = useState('')
  // 定时任务
  const [activeTab, setActiveTab] = useState<'templates' | 'schedules'>('templates')
  const [schedules, setSchedules] = useState<any[]>([])
  const [scheduleLoading, setScheduleLoading] = useState(false)
  const [scheduleModalOpen, setScheduleModalOpen] = useState(false)
  const [scheduleName, setScheduleName] = useState('')
  const [scheduleCron, setScheduleCron] = useState('0 9 * * *')
  const [scheduleTemplateId, setScheduleTemplateId] = useState<number | null>(null)
  const [scheduleSubmitting, setScheduleSubmitting] = useState(false)
  const [scheduleSearch, setScheduleSearch] = useState('')
  const scheduleInputRef = useRef<any>(null)

  // 加载数据源
  const loadDatasources = async () => {
    try {
      const r = await client.get('/datasources')
      setDatasources(r.data.items || r.data || [])
    } catch {}
  }

  // 加载可用模型
  const loadModels = async () => {
    try {
      const r = await client.get('/models', { params: { scene: 'sql', size: 50 } })
      const list = r.data.items || r.data || []
      setModels(list)
      // 默认选第一个启用的模型
      const defaultModel = list.find((m: any) => m.enabled !== false) || list[0]
      if (defaultModel && !modelId) setModelId(defaultModel.id)
    } catch {}
  }

  // 加载模板列表
  const loadTemplates = async () => {
    setTemplateLoading(true)
    try {
      const r = await client.get('/insight/templates')
      setTemplates(r.data.items || [])
    } catch {
      setTemplates([])
    } finally {
      setTemplateLoading(false)
    }
  }

  const loadSchedules = async () => {
    setScheduleLoading(true)
    try {
      const r = await client.get('/insight/schedules', { params: { page_size: 50 } })
      setSchedules(r.data.items || [])
    } catch {
      setSchedules([])
    } finally {
      setScheduleLoading(false)
    }
  }

  const openScheduleModal = (tpl?: any) => {
    setScheduleTemplateId(tpl?.id || null)
    setScheduleName(tpl ? `${tpl.name} 定时报告` : '')
    setScheduleCron('0 9 * * *')
    setScheduleModalOpen(true)
  }

  const handleCreateSchedule = async () => {
    if (!scheduleName.trim() || !scheduleCron.trim()) return
    if (!scheduleTemplateId) {
      message.warning('请先选择一个洞察模板')
      return
    }
    const tpl = templates.find(t => t.id === scheduleTemplateId)
    if (!tpl) return
    setScheduleSubmitting(true)
    try {
      await client.post('/insight/schedules', {
        name: scheduleName.trim(),
        template_id: scheduleTemplateId,
        config: tpl.config || {},
        cron_expr: scheduleCron.trim(),
        model_id: modelId,
      })
      message.success('定时任务创建成功')
      setScheduleModalOpen(false)
      setActiveTab('schedules')
      loadSchedules()
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '创建失败')
    } finally {
      setScheduleSubmitting(false)
    }
  }

  const handleToggleSchedule = async (sched: any) => {
    const newStatus = sched.status === 'active' ? 'paused' : 'active'
    try {
      await client.put(`/insight/schedules/${sched.id}`, { status: newStatus })
      message.success(newStatus === 'active' ? '已启用' : '已暂停')
      loadSchedules()
    } catch {
      message.error('操作失败')
    }
  }

  const handleDeleteSchedule = async (sched: any) => {
    try {
      await client.delete(`/insight/schedules/${sched.id}`)
      message.success('已删除')
      loadSchedules()
    } catch {
      message.error('删除失败')
    }
  }

  const filteredSchedules = schedules.filter(s => {
    const kw = scheduleSearch.trim().toLowerCase()
    if (!kw) return true
    return (s.name || '').toLowerCase().includes(kw) ||
           (s.cron_expr || '').toLowerCase().includes(kw)
  })

  const handleRunScheduleNow = async (sched: any) => {    try {
      message.loading({ content: '正在执行...', key: 'run-sched', duration: 0 })
      const r = await client.post(`/insight/schedules/${sched.id}/run`)
      message.destroy('run-sched')
      if (r.data.ok) {
        message.success(`执行成功，报告ID: ${r.data.report_id}`)
      } else {
        message.error(r.data.error || '执行失败')
      }
      loadSchedules()
    } catch (e: any) {
      message.destroy('run-sched')
      message.error(e?.response?.data?.detail || '执行失败')
    }
  }

  // 进入列表视图时加载模板和数据源
  useEffect(() => {
    if (viewMode === 'list') { loadTemplates(); loadDatasources(); loadSchedules() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewMode])

  // 数据源ID → 名称
  const dsName = (id: number) => {
    const ds = datasources.find(d => d.id === id)
    return ds ? (ds.name || ds.database_name || `数据源 ${id}`) : `数据源 ${id}`
  }

  // 搜索过滤
  const filteredTemplates = templates.filter(tpl => {
    const kw = templateSearch.trim().toLowerCase()
    if (!kw) return true
    return (tpl.name || '').toLowerCase().includes(kw) ||
           (tpl.purpose || '').toLowerCase().includes(kw) ||
           (tpl.description || '').toLowerCase().includes(kw)
  })

  // 新建向导
  const startNewWizard = () => {
    setEditingTemplate(null)
    setCurrent(0)
    setPurpose('')
    setDatasourceId(null)
    setModelId(null)
    setItems([])
    setPreviewCards([])
    setTemplateName('')
    setViewMode('wizard')
    loadDatasources()
    loadModels()
  }

  // 编辑模板：加载配置到向导
  const handleEditTemplate = (tpl: InsightTemplateItem) => {
    setEditingTemplate(tpl)
    const cfg = tpl.config || { purpose: '', datasource_id: 0, items: [] }
    setPurpose(cfg.purpose || tpl.purpose || '')
    setDatasourceId(cfg.datasource_id || tpl.datasource_id)
    setModelId(cfg.model_id || null)
    setItems((cfg.items || []).map((i: any) => ({ ...i, enabled: i.enabled !== false })))
    setTemplateName(tpl.name)
    setPreviewCards([])
    setCurrent(3)
    setViewMode('wizard')
    loadDatasources()
    loadModels()
  }

  // 从模板直接生成报告
  const handleGenerateFromTemplate = async (tpl: InsightTemplateItem) => {
    const cfg = tpl.config || { purpose: '', datasource_id: 0, items: [] }
    const enabled = (cfg.items || []).filter((i: any) => i.enabled !== false)
    if (!enabled.length) { message.warning('该模板没有启用的分析项'); return }
    setLoading(true)
    try {
      const r = await client.post('/insight/execute', {
        config: { ...cfg, items: enabled },
        template_id: tpl.id,
      })
      navigate(`/reports`)
    } catch (e: any) {
      // 静默失败，不弹 toast
    } finally {
      setLoading(false)
    }
  }

  // 删除模板
  const handleDeleteTemplate = (tpl: InsightTemplateItem) => {
    Modal.confirm({
      title: '删除模板',
      content: `确定删除模板「${tpl.name}」吗？此操作不可恢复。`,
      okText: '删除', okType: 'danger',
      onOk: async () => {
        await client.delete(`/insight/templates/${tpl.id}`)
        message.success('模板已删除')
        loadTemplates()
      },
    })
  }

  const steps = [
    { title: '描述目的' },
    { title: '选择数据源' },
    { title: 'AI生成草案' },
    { title: '确认配置' },
    { title: '预览报告' },
    { title: '保存模板' },
  ]

  const handleGenerateDraft = async () => {
    if (!purpose.trim()) { message.warning('请输入分析目的'); return }
    if (!datasourceId) { message.warning('请选择数据源'); return }
    setLoading(true)
    try {
      const r = await client.post('/insight/generate-draft', { purpose, datasource_id: datasourceId, model_id: modelId })
      setItems(r.data.config.items || [])
      setCurrent(3)
      message.success('草案已生成')
    } finally {
      setLoading(false)
    }
  }

  const handleExecute = async () => {
    const enabled = items.filter(i => i.enabled)
    if (!enabled.length) { message.warning('至少启用一个分析项'); return }
    setLoading(true)
    try {
      const r = await client.post('/insight/execute', {
        config: { purpose, datasource_id: datasourceId, items: enabled.map(i => ({ ...i })) },
      })
      navigate(`/reports`)
    } finally {
      setLoading(false)
    }
  }

  const handleSaveTemplate = async () => {
    if (!templateName.trim()) { message.warning('请输入模板名称'); return }
    const config = { purpose, datasource_id: datasourceId, model_id: modelId, items }
    if (editingTemplate) {
      await client.put(`/insight/templates/${editingTemplate.id}`, { config, name: templateName })
      message.success('模板已更新')
    } else {
      await client.post('/insight/save-template', { config, name: templateName })
      message.success('模板已保存')
    }
    setViewMode('list')
  }

  const toggleItem = (id: string) => {
    setItems(prev => prev.map(i => i.id === id ? { ...i, enabled: !i.enabled } : i))
  }

  const updateItem = (id: string, field: string, value: any) => {
    setItems(prev => prev.map(i => i.id === id ? { ...i, [field]: value } : i))
  }

  // columns(字符串数组) + rows(二维数组) → ChartDataset
  const toChartDataset = (columns: string[], rows: any[][]): ChartDataset => {
    const objRows = rows.map(r => {
      const o: Record<string, any> = {}
      columns.forEach((c, i) => { o[c] = r[i] })
      return o
    })
    // 第一列作维度，其余数值列作指标
    const dimensions = columns.length ? [columns[0]] : []
    const metrics = columns.slice(1).filter(c => {
      const vals = objRows.map(r => r[c]).filter(v => v !== null && v !== undefined)
      return vals.length > 0 && vals.every(v => typeof v === 'number' || !isNaN(Number(v)))
    })
    return { dimensions, metrics: metrics.length ? metrics : columns.slice(1), rows: objRows }
  }

  const handlePreview = async () => {
    const enabled = items.filter(i => i.enabled)
    if (!enabled.length) { message.warning('至少启用一个分析项'); return }
    setPreviewLoading(true)
    try {
      const r = await client.post('/insight/preview', {
        config: { purpose, datasource_id: datasourceId, model_id: modelId, items: enabled.map(i => ({ ...i })) },
      })
      setPreviewCards(r.data.cards || [])
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '预览生成失败')
    } finally {
      setPreviewLoading(false)
    }
  }

  // 进入预览步骤时自动触发
  useEffect(() => {
    if (current === 4 && previewCards.length === 0 && !previewLoading) {
      handlePreview()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current])

  // ============ 列表视图 ============
  if (viewMode === 'list') {
    return (
      <div style={{ padding: 16, maxWidth: 1100, margin: '0 auto' }}>
        <Card
          title={<Space><span style={{ fontSize: 16, fontWeight: 600 }}>洞察分析</span></Space>}
          extra={<Button type="primary" icon={<ThunderboltOutlined />} onClick={startNewWizard}>新建洞察</Button>}
        >
          <Tabs
            activeKey={activeTab}
            onChange={(k) => setActiveTab(k as any)}
            items={[
              {
                key: 'templates',
                label: <span><SaveOutlined style={{ marginRight: 6 }} />模板列表</span>,
                children: (
                  <>
          {templateLoading ? (
            <div style={{ textAlign: 'center', padding: 60 }}><Spin /></div>
          ) : (
            <>
              <div style={{ marginBottom: 16 }}>
                <Input.Search
                  placeholder="搜索模板名称 / 分析目的"
                  value={templateSearch}
                  onChange={e => setTemplateSearch(e.target.value)}
                  onClear={() => setTemplateSearch('')}
                  allowClear
                  style={{ maxWidth: 360 }}
                />
              </div>
              {filteredTemplates.length === 0 ? (
                <Empty description={templateSearch ? `未找到匹配「${templateSearch}」的模板` : '暂无保存的模板，点击「新建洞察」创建第一个分析模板'} style={{ padding: '40px 0' }} />
              ) : (
                <List
                  dataSource={filteredTemplates}
                  renderItem={tpl => (
                    <List.Item
                      actions={[
                        <Button key="gen" type="primary" size="small" icon={<PlayCircleOutlined />}
                          loading={loading} onClick={() => handleGenerateFromTemplate(tpl)}>生成报告</Button>,
                        <Button key="edit" size="small" onClick={() => handleEditTemplate(tpl)}>编辑</Button>,
                        <Button key="del" size="small" danger onClick={() => handleDeleteTemplate(tpl)}>删除</Button>,
                      ]}
                    >
                      <List.Item.Meta
                        title={<Space>{tpl.name}<Tag color="blue">{(tpl.config?.items || []).length} 项</Tag></Space>}
                        description={
                          <div>
                            <div style={{ color: '#595959', marginBottom: 4 }}>{tpl.purpose || tpl.description || '—'}</div>
                            <div style={{ fontSize: 12, color: '#8c8c8c' }}>
                              数据源: {dsName(tpl.datasource_id)} · 创建: {tpl.created_at ? new Date(tpl.created_at).toLocaleString('zh-CN') : '—'}
                            </div>
                          </div>
                        }
                      />
                    </List.Item>
                  )}
                />
              )}
            </>
          )}
                  </>
                )
              },
              {
                key: 'schedules',
                label: <span><ClockCircleOutlined style={{ marginRight: 6 }} />定时任务</span>,
                children: (
                  <div>
                    <div style={{ marginBottom: 16, display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                      <Input.Search
                        placeholder="搜索任务名称 / CRON"
                        value={scheduleSearch}
                        onChange={e => setScheduleSearch(e.target.value)}
                        onClear={() => setScheduleSearch('')}
                        allowClear
                        style={{ maxWidth: 300 }}
                      />
                      <Button type="primary" icon={<PlusOutlined />} onClick={() => openScheduleModal()} disabled={templates.length === 0}>
                        新建定时任务
                      </Button>
                    </div>
                    {scheduleLoading ? (
                      <div style={{ textAlign: 'center', padding: 40 }}><Spin /></div>
                    ) : filteredSchedules.length === 0 ? (
                      <Empty description={scheduleSearch ? `未找到匹配「${scheduleSearch}」的定时任务` : '暂无定时任务，点击「新建定时任务」创建'} style={{ padding: '40px 0' }} />
                    ) : (
                      <List
                        dataSource={filteredSchedules}
                        renderItem={sched => (
                          <List.Item
                            actions={[
                              <Button key="run" size="small" icon={<ReloadOutlined />} onClick={() => handleRunScheduleNow(sched)}>立即执行</Button>,
                              <Button key="toggle" size="small" icon={sched.status === 'active' ? <PauseOutlined /> : <PlayCircleOutlined />} onClick={() => handleToggleSchedule(sched)}>
                                {sched.status === 'active' ? '暂停' : '启用'}
                              </Button>,
                              <Popconfirm key="del" title="确认删除该定时任务？" onConfirm={() => handleDeleteSchedule(sched)}>
                                <Button size="small" danger icon={<DeleteOutlined />}>删除</Button>
                              </Popconfirm>,
                            ]}
                          >
                            <List.Item.Meta
                              avatar={<ClockCircleOutlined style={{ fontSize: 20, color: sched.status === 'active' ? '#6C5CE7' : '#bfbfbf' }} />}
                              title={<Space>{sched.name}<Tag color={sched.status === 'active' ? 'green' : 'default'}>{sched.status === 'active' ? '运行中' : '已暂停'}</Tag></Space>}
                              description={
                                <div style={{ fontSize: 12, color: '#8c8c8c' }}>
                                  <div>CRON: <code style={{ background: '#f5f5f5', padding: '1px 6px', borderRadius: 4 }}>{sched.cron_expr}</code> · 下次执行: {sched.next_run_at ? new Date(sched.next_run_at).toLocaleString('zh-CN') : '—'}</div>
                                  <div>已执行 {sched.run_count || 0} 次 · 上次: {sched.last_run_at ? new Date(sched.last_run_at).toLocaleString('zh-CN') : '—'} · 上次报告ID: {sched.last_report_id || '—'}</div>
                                </div>
                              }
                            />
                          </List.Item>
                        )}
                      />
                    )}
                  </div>
                ),
              },
            ]}
          />
        </Card>

        {/* 新建定时任务弹窗 */}
        <Modal
          title="新建定时任务"
          open={scheduleModalOpen}
          onOk={handleCreateSchedule}
          onCancel={() => setScheduleModalOpen(false)}
          okText="创建"
          cancelText="取消"
          confirmLoading={scheduleSubmitting}
          okButtonProps={{ disabled: !scheduleName.trim() || !scheduleTemplateId }}
          width={480}
        >
          <div style={{ marginBottom: 16 }}>
            <div style={{ marginBottom: 6, fontWeight: 500 }}>任务名称</div>
            <Input
              value={scheduleName}
              onChange={e => setScheduleName(e.target.value)}
              placeholder="请输入任务名称"
              maxLength={100}
            />
          </div>
          <div style={{ marginBottom: 16 }}>
            <div style={{ marginBottom: 6, fontWeight: 500 }}>选择洞察模板</div>
            <Select
              style={{ width: '100%' }}
              value={scheduleTemplateId}
              onChange={setScheduleTemplateId}
              placeholder="选择要定时执行的洞察模板"
              options={templates.map((t: any) => ({ value: t.id, label: t.name }))}
            />
          </div>
          <div style={{ marginBottom: 16 }}>
            <div style={{ marginBottom: 6, fontWeight: 500 }}>执行时间（CRON）</div>
            <Space direction="vertical" style={{ width: '100%' }}>
              <Select
                style={{ width: '100%' }}
                value={CRON_PRESETS.find(p => p.value === scheduleCron) ? scheduleCron : 'custom'}
                onChange={(v) => {
                  if (v === 'custom') {
                    setScheduleCron('')
                    setTimeout(() => scheduleInputRef.current?.focus(), 50)
                  } else {
                    setScheduleCron(v)
                  }
                }}
                options={CRON_PRESETS}
              />
              <Input
                ref={scheduleInputRef}
                value={scheduleCron}
                onChange={e => setScheduleCron(e.target.value)}
                placeholder="分 时 日 月 周，例如：0 9 * * *"
              />
              <div style={{ fontSize: 12, color: '#8c8c8c' }}>
                格式：分(0-59) 时(0-23) 日(1-31) 月(1-12) 周(0-6, 0=周日)，* 表示任意
              </div>
            </Space>
          </div>
        </Modal>
      </div>
    )
  }

  // ============ 向导视图 ============
  return (
    <div style={{ padding: 16, maxWidth: 960, margin: '0 auto' }}>
      <Card
        title={
          <Space>
            <span style={{ fontSize: 16, fontWeight: 600 }}>{editingTemplate ? '编辑洞察模板' : '新建洞察分析'}</span>
            <Tag color="purple">六步向导</Tag>
          </Space>
        }
        extra={<Button onClick={() => setViewMode('list')}>返回列表</Button>}
      >
        <Steps current={current} items={steps} style={{ marginBottom: 24 }} />

        {/* Step 0: 描述目的 */}
        {current === 0 && (
          <div>
            <h3>请描述你的分析目的</h3>
            <p style={{ color: '#8c8c8c' }}>例如：分析各门店本月的经营状况，找出表现最好和最差的门店</p>
            <TextArea rows={4} value={purpose} onChange={e => setPurpose(e.target.value)}
              placeholder="描述你想分析什么问题…" />
            <div style={{ marginTop: 16, textAlign: 'right' }}>
              <Button type="primary" disabled={!purpose.trim()} onClick={() => { loadDatasources(); setCurrent(1) }}>下一步</Button>
            </div>
          </div>
        )}

        {/* Step 1: 选择数据源 */}
        {current === 1 && (
          <div>
            <h3>选择数据源</h3>
            <Select style={{ width: '100%' }} value={datasourceId} onChange={setDatasourceId}
              placeholder="选择要分析的数据源"
              options={datasources.map(d => ({ value: d.id, label: d.name || d.database_name }))} />
            <div style={{ marginTop: 16, display: 'flex', justifyContent: 'space-between' }}>
              <Button onClick={() => setCurrent(0)}>上一步</Button>
              <Button type="primary" disabled={!datasourceId} onClick={() => { loadModels(); setCurrent(2) }}>下一步</Button>
            </div>
          </div>
        )}

        {/* Step 2: AI生成草案 */}
        {current === 2 && (
          <div style={{ textAlign: 'center', padding: '40px 20px' }}>
            <ThunderboltOutlined style={{ fontSize: 48, color: '#6C5CE7' }} />
            <h3>AI 生成分析草案</h3>
            <p style={{ color: '#8c8c8c' }}>根据「{purpose}」自动设计分析项</p>
            <div style={{ maxWidth: 400, margin: '0 auto 24px', textAlign: 'left' }}>
              <div style={{ fontSize: 13, color: '#595959', marginBottom: 6 }}>选择生成模型</div>
              <Select style={{ width: '100%' }} value={modelId} onChange={setModelId}
                placeholder="选择大模型"
                options={models.map((m: any) => ({ value: m.id, label: m.name || m.model_name }))} />
            </div>
            <Button type="primary" size="large" loading={loading} icon={<ThunderboltOutlined />} onClick={handleGenerateDraft}>
              生成分析草案
            </Button>
            <div style={{ marginTop: 16 }}><Button onClick={() => setCurrent(1)}>上一步</Button></div>
          </div>
        )}

        {/* Step 3: 确认配置 */}
        {current === 3 && (
          <div>
            <h3>确认分析项配置</h3>
            <p style={{ color: '#8c8c8c' }}>可编辑分析项标题、查询问题、图表类型，或禁用不需要的项</p>
            <List
              dataSource={items}
              renderItem={item => (
                <List.Item>
                  <div style={{ width: '100%' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                      <Switch checked={item.enabled} onChange={() => toggleItem(item.id)} size="small" />
                      <Input value={item.title} onChange={e => updateItem(item.id, 'title', e.target.value)}
                        style={{ width: 200 }} size="small" />
                      <Select value={item.chart_type} onChange={v => updateItem(item.id, 'chart_type', v)}
                        options={CHART_OPTIONS} style={{ width: 120 }} size="small" />
                    </div>
                    <TextArea value={item.question} onChange={e => updateItem(item.id, 'question', e.target.value)}
                      rows={1} size="small" placeholder="查询问题" />
                  </div>
                </List.Item>
              )}
            />
            <div style={{ marginTop: 16, display: 'flex', justifyContent: 'space-between' }}>
              <Button onClick={() => setCurrent(2)}>上一步</Button>
              <Button type="primary" onClick={() => setCurrent(4)}>预览报告</Button>
            </div>
          </div>
        )}

        {/* Step 4: 预览 */}
        {current === 4 && (
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
              <h3 style={{ margin: 0 }}>报告预览</h3>
              <Button icon={<ReloadOutlined />} loading={previewLoading} onClick={handlePreview}>重新预览</Button>
            </div>
            {previewLoading ? (
              <div style={{ textAlign: 'center', padding: 60 }}><Spin size="large" tip="正在执行查询并生成预览…" /></div>
            ) : previewCards.length === 0 ? (
              <Empty description="暂无预览数据" />
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
                {previewCards.map((card, idx) => (
                  <PreviewChartCard key={card.id} card={card} index={idx} toChartDataset={toChartDataset} />
                ))}
              </div>
            )}
            <div style={{ marginTop: 16, display: 'flex', justifyContent: 'space-between' }}>
              <Button onClick={() => { setPreviewCards([]); setCurrent(3) }}>上一步</Button>
              <Button type="primary" onClick={() => setCurrent(5)} icon={<SaveOutlined />}>保存为模板</Button>
            </div>
          </div>
        )}

        {/* Step 5: 保存模板 */}
        {current === 5 && (
          <div style={{ textAlign: 'center', padding: 40 }}>
            <h3>保存为模板</h3>
            <p style={{ color: '#8c8c8c' }}>保存后可在模板列表中重复使用此配置生成报告</p>
            <Input value={templateName} onChange={e => setTemplateName(e.target.value)}
              placeholder="模板名称" style={{ width: 300, marginBottom: 16 }} />
            <div style={{ display: 'flex', gap: 12, justifyContent: 'center' }}>
              <Button onClick={() => setCurrent(4)}>上一步</Button>
              <Button type="primary" onClick={handleSaveTemplate} icon={<SaveOutlined />}>保存模板</Button>
            </div>
          </div>
        )}
      </Card>
    </div>
  )
}

/** 预览图表卡片：ECharts 渲染 + 折叠 SQL + 错误展示 */
function PreviewChartCard({ card, index, toChartDataset }: {
  card: PreviewCard
  index: number
  toChartDataset: (cols: string[], rows: any[][]) => ChartDataset
}) {
  const chartRef = useRef<HTMLDivElement>(null)
  const instRef = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    if (card.status !== 'success' || !card.columns.length || !chartRef.current) return
    if (!instRef.current) {
      instRef.current = echarts.init(chartRef.current)
    }
    const dataset = toChartDataset(card.columns, card.rows)
    try {
      const option = buildChartOption(card.chart_type as ChartType, dataset)
      instRef.current.setOption(option, true)
    } catch {
      instRef.current.clear()
    }
    const onResize = () => instRef.current?.resize()
    window.addEventListener('resize', onResize)
    return () => { window.removeEventListener('resize', onResize) }
  }, [card, toChartDataset])

  useEffect(() => {
    return () => { instRef.current?.dispose(); instRef.current = null }
  }, [])

  const isTable = card.chart_type === 'table'
  const isKpi = card.chart_type === 'kpi'

  return (
    <Card
      size="small"
      title={
        <Space>
          <span style={{ fontWeight: 600 }}>{index + 1}. {card.title}</span>
          <Tag color={card.status === 'success' ? 'green' : 'red'}>{card.status === 'success' ? '成功' : '失败'}</Tag>
          <Tag>{card.chart_type}</Tag>
        </Space>
      }
      style={{ height: '100%' }}
    >
      {card.status === 'error' ? (
        <Alert type="error" message={card.error || '执行失败'} showIcon style={{ marginBottom: 8 }} />
      ) : isKpi ? (
        (() => {
          const valIdx = card.columns.length > 1 ? 1 : 0
          const dimIdx = 0
          return (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: '24px 16px', minHeight: 160 }}>
              <div style={{ fontSize: 36, fontWeight: 700, color: '#6C5CE7' }}>
                {card.rows[0]?.[valIdx] ?? '—'}
              </div>
              <div style={{ fontSize: 14, color: '#8c8c8c', marginTop: 8 }}>{card.columns[valIdx] || '数值'}</div>
              {card.columns[dimIdx] && card.rows[0]?.[dimIdx] != null && (
                <div style={{ fontSize: 12, color: '#595959', marginTop: 4 }}>{String(card.rows[0][dimIdx])}</div>
              )}
            </div>
          )
        })()
      ) : isTable ? (
        <div style={{ overflowX: 'auto', maxHeight: 360, width: '100%' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13, tableLayout: 'fixed' }}>
            <thead>
              <tr>{card.columns.map(c => <th key={c} style={{ border: '1px solid #f0f0f0', padding: '6px 10px', background: '#fafafa', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{c}</th>)}</tr>
            </thead>
            <tbody>
              {card.rows.slice(0, 50).map((r, i) => (
                <tr key={i}>{r.map((v, j) => <td key={j} style={{ border: '1px solid #f0f0f0', padding: '6px 10px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: 200 }}>{String(v ?? '')}</td>)}</tr>
              ))}
            </tbody>
          </table>
          {card.rows.length > 50 && <div style={{ fontSize: 11, color: '#999', marginTop: 4 }}>仅显示前 50 行，共 {card.rows.length} 行</div>}
        </div>
      ) : (
        <div ref={chartRef} style={{ width: '100%', height: 320 }} />
      )}
      {card.sql && (
        <Collapse ghost size="small" style={{ marginTop: 8 }}
          items={[{ key: 'sql', label: <span style={{ fontSize: 12, color: '#8c8c8c' }}>查看 SQL</span>,
            children: <pre style={{ fontSize: 11, background: '#f6f8fa', padding: 8, borderRadius: 4, overflowX: 'auto', margin: 0, maxHeight: 160 }}>{card.sql}</pre> }]} />
      )}
    </Card>
  )
}
