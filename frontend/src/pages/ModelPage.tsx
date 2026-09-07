// 模型配置：OpenAI 兼容 LLM/Embedding
import { useEffect, useState } from 'react'
import {
 Button, Card, Drawer, Form, Input, InputNumber, Popconfirm, Space, Switch, Table, Tag, message,
} from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import { client, errMsg } from '../api/client'
import type { ModelItem } from '../types'
import PageHeader from '../components/PageHeader'
import { GlassSelect } from '../ui'

export default function ModelPage() {
  const [msgApi, ctx] = message.useMessage()
  const [list, setList] = useState<ModelItem[]>([])
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<ModelItem | null>(null)
  const [form] = Form.useForm()

  const load = async () => {
    try {
      const r = await client.get<ModelItem[]>('/models')
      setList(r.data)
    } catch (e) { msgApi.error(errMsg(e)) }
  }
  useEffect(() => { load() }, [])

  const openCreate = () => { setEditing(null); form.resetFields(); setOpen(true) }
  const openEdit = (m: ModelItem) => { setEditing(m); form.setFieldsValue(m); setOpen(true) }

  const submit = async () => {
    const v = await form.validateFields()
    try {
      if (editing) await client.put(`/models/${editing.id}`, v)
      else await client.post('/models', v)
      msgApi.success('已保存')
      setOpen(false)
      load()
    } catch (e) { msgApi.error(errMsg(e)) }
  }

  const test = async (m: ModelItem) => {
    try {
      const r = await client.post(`/models/${m.id}/test`)
      msgApi[r.data.ok ? 'success' : 'error'](r.data.ok ? `连通正常：${r.data.reply}` : `失败：${r.data.message}`)
    } catch (e) { msgApi.error(errMsg(e)) }
  }

  return (
    <div className="glass-page">
      <PageHeader
        title="模型配置"
        description="OpenAI 兼容协议（DeepSeek / 通义 / Kimi / 本地 vLLM / Ollama）；未配置 API Key 时系统进入 mock 演示模式"
        extra={<Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建模型</Button>}
      />
      <Card styles={{ header: { display: 'none' } }}>
      {ctx}
      <Table
        rowKey="id"
        dataSource={list}
        pagination={false}
        columns={[
          { title: '名称', dataIndex: 'name' },
          { title: '模型', dataIndex: 'model_name', render: (v, r) => <code>{v}</code> },
          { title: 'Embedding', dataIndex: 'embedding_model', render: (v) => v || '—' },
          { title: '场景', dataIndex: 'scene', render: (v) => <Tag color="cyan">{v}</Tag> },
          { title: '默认', dataIndex: 'is_default', render: (v) => (v ? <Tag color="gold">默认</Tag> : '—') },
          { title: 'API Key', dataIndex: 'has_key', render: (v) => (v ? <Tag color="green">已配置</Tag> : <Tag color="orange">未配置</Tag>) },
          {
            title: '操作',
            render: (_, r) => (
              <Space size={4}>
                <Button size="small" onClick={() => test(r)}>测试</Button>
                <Button size="small" onClick={() => openEdit(r)}>编辑</Button>
                <Popconfirm title="删除该模型配置？" onConfirm={async () => {
                  await client.delete(`/models/${r.id}`); load()
                }}>
                  <Button size="small" danger>删除</Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Drawer title={editing ? '编辑模型' : '新建模型'} open={open} onClose={() => setOpen(false)} width={440}>
        <Form form={form} layout="vertical" initialValues={{ provider: 'openai_compatible', temperature: 0.1, top_p: 0.9, max_tokens: 2048, scene: 'sql' }}>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="如：DeepSeek 生产" />
          </Form.Item>
          <Form.Item name="base_url" label="Base URL" rules={[{ required: true }]}>
            <Input placeholder="https://api.deepseek.com/v1" />
          </Form.Item>
          <Form.Item name="api_key" label="API Key" rules={[{ required: !editing }]}>
            <Input.Password placeholder={editing ? '留空则不修改' : ''} />
          </Form.Item>
          <Form.Item name="model_name" label="模型名" rules={[{ required: true }]}>
            <Input placeholder="deepseek-chat" />
          </Form.Item>
          <Form.Item name="embedding_model" label="Embedding 模型">
            <Input placeholder="text-embedding-3-small" />
          </Form.Item>
          <Form.Item name="scene" label="用途场景">
            <GlassSelect options={[{ label: 'SQL 生成', value: 'sql' }, { label: '文字总结', value: 'summary' }]} />
          </Form.Item>
          <Space size={16}>
            <Form.Item name="temperature" label="温度"><InputNumber min={0} max={2} step={0.1} /></Form.Item>
            <Form.Item name="max_tokens" label="Max Tokens"><InputNumber min={128} max={32768} /></Form.Item>
          </Space>
          <Form.Item name="is_default" label="设为默认" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Button type="primary" onClick={submit}>保存</Button>
        </Form>
      </Drawer>
      </Card>
    </div>
  )
}
