// 洞察分析：六步向导
import { useState } from 'react'
import { Button, Card, Input, Select, Steps, Space, List, Tag, Switch, message, Modal, Empty, Spin } from 'antd'
import { ThunderboltOutlined, SaveOutlined, PlayCircleOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { client } from '../api/client'

const { TextArea } = Input

interface InsightItem {
  id: string
  title: string
  question: string
  chart_type: string
  enabled: boolean
}

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
  const [saveModalOpen, setSaveModalOpen] = useState(false)

  // 加载数据源
  const loadDatasources = async () => {
    try {
      const r = await client.get('/datasources')
      setDatasources(r.data.items || r.data || [])
    } catch {}
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
      const r = await client.post('/insight/generate-draft', { purpose, datasource_id: datasourceId })
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
      message.success('报告已生成')
      navigate(`/reports`)
    } finally {
      setLoading(false)
    }
  }

  const handleSaveTemplate = async () => {
    if (!templateName.trim()) { message.warning('请输入模板名称'); return }
    await client.post('/insight/save-template', {
      config: { purpose, datasource_id: datasourceId, items },
      name: templateName,
    })
    message.success('模板已保存')
    setSaveModalOpen(false)
  }

  const toggleItem = (id: string) => {
    setItems(prev => prev.map(i => i.id === id ? { ...i, enabled: !i.enabled } : i))
  }

  const updateItem = (id: string, field: string, value: any) => {
    setItems(prev => prev.map(i => i.id === id ? { ...i, [field]: value } : i))
  }

  return (
    <div style={{ padding: 16, maxWidth: 960, margin: '0 auto' }}>
      <Card title="洞察分析" extra={<Tag color="purple">六步向导</Tag>}>
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
              <Button type="primary" disabled={!datasourceId} onClick={() => setCurrent(2)}>下一步</Button>
            </div>
          </div>
        )}

        {/* Step 2: AI生成草案 */}
        {current === 2 && (
          <div style={{ textAlign: 'center', padding: 40 }}>
            <ThunderboltOutlined style={{ fontSize: 48, color: '#6C5CE7' }} />
            <h3>AI 正在生成分析草案</h3>
            <p style={{ color: '#8c8c8c' }}>根据「{purpose}」自动设计分析项</p>
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
              <Space>
                <Button onClick={() => setCurrent(4)}>预览报告</Button>
                <Button type="primary" onClick={handleExecute} icon={<PlayCircleOutlined />}>生成报告</Button>
              </Space>
            </div>
          </div>
        )}

        {/* Step 4: 预览 */}
        {current === 4 && (
          <div>
            <h3>报告预览</h3>
            <Empty description="预览功能开发中，点击「生成报告」直接生成完整报告" />
            <div style={{ marginTop: 16, display: 'flex', justifyContent: 'space-between' }}>
              <Button onClick={() => setCurrent(3)}>上一步</Button>
              <Space>
                <Button onClick={() => setSaveModalOpen(true)} icon={<SaveOutlined />}>保存为模板</Button>
                <Button type="primary" loading={loading} onClick={handleExecute} icon={<PlayCircleOutlined />}>生成报告</Button>
              </Space>
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
            <div><Button type="primary" onClick={handleSaveTemplate} icon={<SaveOutlined />}>保存模板</Button></div>
          </div>
        )}
      </Card>

      <Modal title="保存为模板" open={saveModalOpen} onCancel={() => setSaveModalOpen(false)}
        onOk={handleSaveTemplate} okText="保存">
        <Input value={templateName} onChange={e => setTemplateName(e.target.value)} placeholder="模板名称" />
      </Modal>
    </div>
  )
}
