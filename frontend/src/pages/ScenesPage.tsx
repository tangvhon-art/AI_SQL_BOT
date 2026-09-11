// C14 场景模板管理：系统预置（只读）+ 工作空间自定义 CRUD + 场景识别测试
// - 场景六编码：mgmt_ops 经营管理 / store_diag 门店诊断 / product_analysis 商品分析 /
//   cost_supply 成本供应链 / customer_marketing 客户营销 / auto_report 自动报告
// - 识别测试：输入问题 → 命中场景码 + 注入 LLM 的提示词上下文
import { useEffect, useState } from 'react'
import {
  Button, Card, Descriptions, Drawer, Form, Input, InputNumber, Popconfirm, Space, Switch, Table, Tag, Typography,
} from 'antd'
import { ExperimentOutlined, PlusOutlined, SearchOutlined } from '@ant-design/icons'
import { client } from '../api/client'
import type { SceneDef } from '../types'
import PageHeader from '../components/PageHeader'
import { useMessageApi } from '../hooks/useMessageApi'

const SCENE_META: Record<string, { name: string; color: string }> = {
  mgmt_ops: { name: '经营管理', color: 'blue' },
  store_diag: { name: '门店诊断', color: 'purple' },
  product_analysis: { name: '商品分析', color: 'cyan' },
  cost_supply: { name: '成本供应链', color: 'orange' },
  customer_marketing: { name: '客户营销', color: 'magenta' },
  auto_report: { name: '自动报告', color: 'geekblue' },
}

export default function ScenesPage() {
  const { msgApi, ctx, toastError } = useMessageApi()
  const [items, setItems] = useState<SceneDef[]>([])
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<SceneDef | null>(null)
  const [form] = Form.useForm()
  // 识别测试
  const [testQ, setTestQ] = useState('')
  const [testLoading, setTestLoading] = useState(false)
  const [testResult, setTestResult] = useState<{ scene_code: string; context: string } | null>(null)

  const load = async () => {
    setLoading(true)
    try {
      const r = await client.get<{ items: SceneDef[] }>('/scenes')
      setItems(r.data.items)
    } catch (e) { toastError(e) } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ enabled: true, sort_order: 0, metric_pack: [], examples: [] })
    setOpen(true)
  }
  const openEdit = (s: SceneDef) => {
    setEditing(s)
    form.setFieldsValue({
      scene_code: s.scene_code, scene_name: s.scene_name, description: s.description,
      metric_pack: (s.metric_pack || []).length ? s.metric_pack : [{ name: '', metric: '', desc: '' }],
      gen_prompt_template: s.gen_prompt_template, explain_template: s.explain_template,
      examples: (s.examples || []).length ? s.examples : [{ question: '', sql: '', note: '' }],
      report_template: s.report_template, enabled: s.enabled, sort_order: s.sort_order,
    })
    setOpen(true)
  }

  const submit = async () => {
    const v = await form.validateFields().catch(() => null)
    if (!v) return
    const body = {
      scene_code: v.scene_code?.trim(),
      scene_name: v.scene_name?.trim(),
      description: v.description ?? '',
      metric_pack: (v.metric_pack || []).filter((m: Record<string, string>) => m.name || m.metric),
      gen_prompt_template: v.gen_prompt_template ?? '',
      explain_template: v.explain_template ?? '',
      examples: (v.examples || []).filter((e: Record<string, string>) => e.question),
      report_template: v.report_template ?? '',
      enabled: v.enabled ?? true,
      sort_order: v.sort_order ?? 0,
    }
    try {
      if (editing) {
        await client.put(`/scenes/${editing.id}`, body)
        msgApi.success('已保存')
      } else {
        await client.post('/scenes', body)
        msgApi.success('已创建')
      }
      setOpen(false)
      load()
    } catch (e) { toastError(e) }
  }

  const remove = async (s: SceneDef) => {
    try {
      await client.delete(`/scenes/${s.id}`)
      msgApi.success('已删除')
      load()
    } catch (e) { toastError(e) }
  }

  const testDetect = async () => {
    if (!testQ.trim()) { msgApi.warning('请输入测试问题'); return }
    setTestLoading(true)
    try {
      const r = await client.get('/scenes/detect', { params: { question: testQ.trim() } })
      setTestResult(r.data)
    } catch (e) { toastError(e) } finally { setTestLoading(false) }
  }

  return (
    <div className="glass-page">
      <PageHeader
        title="场景模板管理"
        description="六大业务场景的指标包 / 提示词模板 / 示例，命中场景时注入 LLM 上下文提升生成准确率；系统预置模板只读，可新建工作空间自定义模板覆盖"
        extra={<Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建场景</Button>}
      />
      {ctx}
      {/* 场景识别测试 */}
      <Card title={<Space><ExperimentOutlined />场景识别测试</Space>} size="small" style={{ marginBottom: 16 }}>
        <Space wrap>
          <Input
            placeholder="输入业务问题，如：本月各门店销售额与毛利率"
            style={{ width: 360 }}
            value={testQ}
            onChange={(e) => setTestQ(e.target.value)}
            onPressEnter={testDetect}
          />
          <Button type="primary" icon={<SearchOutlined />} loading={testLoading} onClick={testDetect}>识别</Button>
        </Space>
        {testResult ? (
          <div style={{ marginTop: 12 }}>
            <Space style={{ marginBottom: 8 }}>
              <span style={{ fontWeight: 600 }}>命中场景：</span>
              {testResult.scene_code ? (
                <Tag color={SCENE_META[testResult.scene_code]?.color || 'default'}>
                  {SCENE_META[testResult.scene_code]?.name || testResult.scene_code}（{testResult.scene_code}）
                </Tag>
              ) : <Tag>未命中（走通用链路）</Tag>}
            </Space>
            {testResult.context ? (
              <div style={{ padding: 10, background: 'rgba(0,0,0,0.03)', borderRadius: 6, maxHeight: 220, overflow: 'auto' }}>
                <Typography.Text style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>{testResult.context}</Typography.Text>
              </div>
            ) : null}
          </div>
        ) : null}
      </Card>

      {/* 场景列表 */}
      <Card styles={{ header: { display: 'none' } }}>
        <Table
          rowKey="id"
          loading={loading}
          dataSource={items}
          pagination={false}
          expandable={{
            expandedRowRender: (s) => (
              <Descriptions size="small" column={2} bordered style={{ marginTop: 8 }}>
                <Descriptions.Item label="指标包" span={2}>
                  {(s.metric_pack || []).map((m, i) => (
                    <Tag key={i} style={{ marginBottom: 4 }}>
                      {[m.name, m.metric, m.desc].filter(Boolean).join(' · ') || '（空）'}
                    </Tag>
                  ))}
                </Descriptions.Item>
                <Descriptions.Item label="生成提示词模板" span={2}>
                  <code style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>{s.gen_prompt_template || '（空）'}</code>
                </Descriptions.Item>
                <Descriptions.Item label="解释提示词模板" span={2}>
                  <code style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>{s.explain_template || '（空）'}</code>
                </Descriptions.Item>
                <Descriptions.Item label="few-shot 示例" span={2}>
                  {(s.examples || []).map((e, i) => (
                    <div key={i} style={{ marginBottom: 6 }}>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>Q: {e.question}</Typography.Text>
                      <div><code style={{ fontSize: 12 }}>{e.sql || '（无 SQL）'}</code></div>
                    </div>
                  ))}
                </Descriptions.Item>
                <Descriptions.Item label="报告模板" span={2}>
                  <code style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>{s.report_template || '（空）'}</code>
                </Descriptions.Item>
              </Descriptions>
            ),
          }}
          columns={[
            { title: '场景', dataIndex: 'scene_code', width: 130, render: (v: string) => (
              <Tag color={SCENE_META[v]?.color || 'default'}>{SCENE_META[v]?.name || v}</Tag>
            ) },
            { title: '名称', dataIndex: 'scene_name' },
            { title: '说明', dataIndex: 'description', ellipsis: true },
            { title: '类型', width: 100, render: (_, s) => s.system ? <Tag>系统预置</Tag> : <Tag color="geekblue">自定义</Tag> },
            { title: '启用', dataIndex: 'enabled', width: 70, render: (v: boolean) => (v ? '是' : '否') },
            { title: '排序', dataIndex: 'sort_order', width: 70 },
            {
              title: '操作', width: 140,
              render: (_, s) => (
                <Space size={4}>
                  {s.system ? <Button size="small" disabled>系统只读</Button> : (
                    <>
                      <Button size="small" onClick={() => openEdit(s)}>编辑</Button>
                      <Popconfirm title="删除该场景模板？" onConfirm={() => remove(s)}>
                        <Button size="small" danger>删除</Button>
                      </Popconfirm>
                    </>
                  )}
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Drawer title={editing ? '编辑场景模板' : '新建场景模板'} open={open} onClose={() => setOpen(false)} width={640}>
        <Form form={form} layout="vertical">
          <Form.Item name="scene_code" label="场景编码" rules={[{ required: true, message: '请输入场景编码' }]}
            extra={editing ? '系统预置编码：mgmt_ops / store_diag / product_analysis / cost_supply / customer_marketing / auto_report' : undefined}>
            <Input disabled={editing?.system} placeholder="如：store_diag" />
          </Form.Item>
          <Form.Item name="scene_name" label="场景名称" rules={[{ required: true }]}>
            <Input placeholder="如：门店诊断" />
          </Form.Item>
          <Form.Item name="description" label="场景说明">
            <Input.TextArea rows={2} placeholder="该场景面向的对象与核心分析目标" />
          </Form.Item>
          <Form.Item name="metric_pack" label="指标包（指标/维度候选优先引用）">
            <Form.List name="metric_pack">
              {(fields, { add, remove: rm }) => (
                <Space direction="vertical" style={{ width: '100%' }}>
                  {fields.map((f) => (
                    <Space key={f.key} align="baseline" style={{ display: 'flex' }}>
                      <Form.Item name={[f.name, 'name']} style={{ marginBottom: 4 }}><Input placeholder="指标名" style={{ width: 130 }} /></Form.Item>
                      <Form.Item name={[f.name, 'metric']} style={{ marginBottom: 4 }}><Input placeholder="指标/字段" style={{ width: 160 }} /></Form.Item>
                      <Form.Item name={[f.name, 'desc']} style={{ marginBottom: 4 }}><Input placeholder="说明" style={{ width: 180 }} /></Form.Item>
                      <Button size="small" type="text" danger onClick={() => rm(f.name)}>删</Button>
                    </Space>
                  ))}
                  <Button size="small" type="dashed" onClick={() => add({ name: '', metric: '', desc: '' })}>+ 添加指标</Button>
                </Space>
              )}
            </Form.List>
          </Form.Item>
          <Form.Item name="gen_prompt_template" label="生成提示词模板" extra="追加到 SQL 生成提示词，可用 {scene_name} 等占位">
            <Input.TextArea rows={3} placeholder="面向{scene_name}场景：优先使用以下指标口径……" />
          </Form.Item>
          <Form.Item name="explain_template" label="解释提示词模板">
            <Input.TextArea rows={2} placeholder="结果解释结构（如经营日报分段）" />
          </Form.Item>
          <Form.Item name="examples" label="few-shot 示例（场景命中时追加）">
            <Form.List name="examples">
              {(fields, { add, remove: rm }) => (
                <Space direction="vertical" style={{ width: '100%' }}>
                  {fields.map((f) => (
                    <Space key={f.key} align="baseline" style={{ display: 'flex' }}>
                      <Form.Item name={[f.name, 'question']} style={{ marginBottom: 4 }}><Input placeholder="问题" style={{ width: 220 }} /></Form.Item>
                      <Form.Item name={[f.name, 'sql']} style={{ marginBottom: 4 }}><Input placeholder="对应 SQL" style={{ width: 260 }} /></Form.Item>
                      <Button size="small" type="text" danger onClick={() => rm(f.name)}>删</Button>
                    </Space>
                  ))}
                  <Button size="small" type="dashed" onClick={() => add({ question: '', sql: '' })}>+ 添加示例</Button>
                </Space>
              )}
            </Form.List>
          </Form.Item>
          <Form.Item name="report_template" label="报告模板（auto_report 场景）">
            <Input.TextArea rows={2} placeholder="报告结构模板" />
          </Form.Item>
          <Space size={16}>
            <Form.Item name="enabled" label="启用" valuePropName="checked" style={{ marginBottom: 4 }}>
              <Switch />
            </Form.Item>
            <Form.Item name="sort_order" label="排序" style={{ marginBottom: 4 }}>
              <InputNumber min={0} />
            </Form.Item>
          </Space>
          <Button type="primary" onClick={submit}>保存</Button>
        </Form>
      </Drawer>
    </div>
  )
}
