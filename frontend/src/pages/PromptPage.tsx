// Prompt 管理页面（公共，多场景）
import { useEffect, useState } from 'react'
import { Button, Card, Drawer, Form, Input, Modal, Select, Space, Table, Tag, message, Popconfirm, Tabs } from 'antd'
import { PlusOutlined, EditOutlined, DeleteOutlined, ExperimentOutlined } from '@ant-design/icons'
import { client } from '../api/client'

interface PromptItem {
  id: number
  scene_type: string
  name: string
  description: string
  scene_tags: string
  prompt_template: string
  is_default: boolean
  is_builtin: boolean
  created_at: string
}

const SCENE_OPTIONS = [
  { value: 'ai_interpret', label: 'AI解读' },
  { value: 'insight_draft', label: '洞察草案' },
  { value: 'sql_generation', label: 'SQL生成' },
  { value: 'custom', label: '自定义' },
]

export default function PromptPage() {
  const [items, setItems] = useState<PromptItem[]>([])
  const [loading, setLoading] = useState(false)
  const [sceneType, setSceneType] = useState<string>('ai_interpret')
  const [keyword, setKeyword] = useState('')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [editing, setEditing] = useState<PromptItem | null>(null)
  const [form] = Form.useForm()
  const [testOpen, setTestOpen] = useState(false)
  const [testResult, setTestResult] = useState('')

  const load = async () => {
    setLoading(true)
    try {
      const r = await client.get('/prompts', { params: { scene_type: sceneType, keyword } })
      setItems(r.data.items || [])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [sceneType, keyword])

  const handleCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ scene_type: sceneType })
    setDrawerOpen(true)
  }

  const handleEdit = (item: PromptItem) => {
    setEditing(item)
    form.setFieldsValue(item)
    setDrawerOpen(true)
  }

  const handleSave = async () => {
    const values = await form.validateFields()
    if (editing) {
      await client.put(`/prompts/${editing.id}`, values)
      message.success('已更新')
    } else {
      await client.post('/prompts', values)
      message.success('已创建')
    }
    setDrawerOpen(false)
    load()
  }

  const handleDelete = async (id: number) => {
    await client.delete(`/prompts/${id}`)
    message.success('已删除')
    load()
  }

  const handleSetDefault = async (id: number) => {
    await client.post(`/prompts/${id}/set-default`)
    message.success('已设为默认')
    load()
  }

  const handleTest = async () => {
    const tpl = form.getFieldValue('prompt_template') || ''
    const r = await client.post('/prompts/test', { prompt_template: tpl, variables: {} })
    setTestResult(r.data.rendered)
    setTestOpen(true)
  }

  const columns = [
    { title: '名称', dataIndex: 'name', key: 'name', render: (t: string, r: PromptItem) => (
      <Space>
        <span>{t}</span>
        {r.is_default && <Tag color="blue">默认</Tag>}
        {r.is_builtin && <Tag color="default">内置</Tag>}
      </Space>
    )},
    { title: '场景', dataIndex: 'scene_type', key: 'scene_type', width: 100, render: (v: string) => SCENE_OPTIONS.find(s => s.value === v)?.label || v },
    { title: '描述', dataIndex: 'description', key: 'description', ellipsis: true },
    { title: '变量', key: 'vars', width: 120, render: (_: any, r: PromptItem) => {
      const vars = (r.prompt_template.match(/\{\{\s*(\w+)\s*\}\}/g) || []).map(v => v.replace(/[{}]/g, '').trim())
      return <Space size={4} wrap>{[...new Set(vars)].slice(0, 3).map(v => <Tag key={v}>{v}</Tag>)}{vars.length > 3 && <Tag>+{vars.length - 3}</Tag>}</Space>
    }},
    { title: '操作', key: 'actions', width: 200, render: (_: any, r: PromptItem) => (
      <Space size={4}>
        {!r.is_default && <Button size="small" type="link" onClick={() => handleSetDefault(r.id)}>设默认</Button>}
        <Button size="small" type="link" icon={<EditOutlined />} onClick={() => handleEdit(r)}>编辑</Button>
        <Popconfirm title="确认删除？删除后该场景将不再使用此提示词。" onConfirm={() => handleDelete(r.id)}><Button size="small" type="link" danger icon={<DeleteOutlined />}>删除</Button></Popconfirm>
      </Space>
    )},
  ]

  return (
    <div style={{ padding: 16 }}>
      <Card
        title="Prompt 管理"
        extra={
          <Space>
            <Input.Search placeholder="搜索名称" allowClear style={{ width: 200 }} onSearch={setKeyword} />
            <Button type="primary" icon={<PlusOutlined />} onClick={handleCreate}>新建模板</Button>
          </Space>
        }
      >
        <Tabs activeKey={sceneType} onChange={setSceneType} items={SCENE_OPTIONS.map(s => ({ key: s.value, label: s.label }))} />
        <Table rowKey="id" columns={columns} dataSource={items} loading={loading} pagination={{ pageSize: 10 }} />
      </Card>

      <Drawer title={editing ? '编辑模板' : '新建模板'} open={drawerOpen} width={640} onClose={() => setDrawerOpen(false)}
        extra={<Button type="primary" onClick={handleSave}>保存</Button>}>
        <Form form={form} layout="vertical">
          <Form.Item name="scene_type" label="使用场景" rules={[{ required: true }]}>
            <Select options={SCENE_OPTIONS} />
          </Form.Item>
          <Form.Item name="name" label="模板名称" rules={[{ required: true }]}>
            <Input placeholder="如：通用分析（默认）" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} placeholder="模板用途说明" />
          </Form.Item>
          <Form.Item name="scene_tags" label="业务标签">
            <Input placeholder="逗号分隔，如：经营,销售" />
          </Form.Item>
          <Form.Item name="prompt_template" label="Prompt 正文" rules={[{ required: true }]} extra="支持 {{变量名}} 占位符，如 {{question}}、{{data_summary}}、{{metrics}}">
            <Input.TextArea rows={12} style={{ fontFamily: 'monospace', fontSize: 12 }} />
          </Form.Item>
          <Form.Item name="is_default" valuePropName="checked" label="设为该场景默认模板">
            <input type="checkbox" />
          </Form.Item>
          <Button icon={<ExperimentOutlined />} onClick={handleTest}>测试渲染</Button>
        </Form>
      </Drawer>

      <Modal title="渲染预览" open={testOpen} onCancel={() => setTestOpen(false)} footer={null} width={600}>
        <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, background: '#F8F9FB', padding: 12, borderRadius: 6, maxHeight: 400, overflow: 'auto' }}>{testResult}</pre>
      </Modal>
    </div>
  )
}
