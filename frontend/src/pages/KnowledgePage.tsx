// 知识库：FAQ + 文档（上传→切片→向量化）+ 检索测试台
import { useState } from 'react'
import {
  Button, Card, Drawer, Form, Input, Popconfirm, Space, Table, Tabs, Tag, Typography, Upload,
} from 'antd'
import { DeleteOutlined, PlusOutlined, SearchOutlined, UploadOutlined } from '@ant-design/icons'
import type { UploadProps } from 'antd'
import { client } from '../api/client'
import type { Faq, KnowledgeDoc } from '../types'
import PageHeader from '../components/PageHeader'
import { useCrudList } from '../hooks/useCrudList'

export default function KnowledgePage() {
  const faqsHook = useCrudList<Faq>('/faqs')
  const docsHook = useCrudList<KnowledgeDoc>('/documents')
  const { data: faqs, load: loadFaqs, msgApi, toastError } = faqsHook
  const { data: docs, load: loadDocs } = docsHook
  const [faqOpen, setFaqOpen] = useState(false)
  const [editing, setEditing] = useState<Faq | null>(null)
  const [form] = Form.useForm()
  const [retrieveQ, setRetrieveQ] = useState('')
  const [retrieveKind, setRetrieveKind] = useState<'doc' | 'faq'>('doc')
  const [retrieveHits, setRetrieveHits] = useState<Array<{ id: string; score: number; meta: Record<string, unknown>; content?: string }>>([])

  const openCreate = () => { setEditing(null); form.resetFields(); setFaqOpen(true) }
  const openEdit = (f: Faq) => { setEditing(f); form.setFieldsValue(f); setFaqOpen(true) }

  const submitFaq = async () => {
    const v = await form.validateFields()
    try {
      if (editing) await client.put(`/faqs/${editing.id}`, v)
      else await client.post('/faqs', v)
      msgApi.success('已保存（自动向量化）')
      setFaqOpen(false)
      loadFaqs()
    } catch (e) { toastError(e) }
  }

  const uploadProps: UploadProps = {
    accept: '.txt,.md,.pdf,.docx',
    showUploadList: false,
    customRequest: async ({ file, onSuccess, onError }) => {
      const fd = new FormData()
      fd.append('file', file as File)
      try {
        const r = await client.post('/documents', fd)
        msgApi.success(`上传成功，生成 ${r.data.chunks ?? 0} 个切片并向量化`)
        onSuccess?.(r.data)
        loadDocs()
      } catch (e) {
        toastError(e)
        onError?.(e as Error)
      }
    },
  }

  const retrieve = async () => {
    if (!retrieveQ.trim()) return
    try {
      const r = await client.post('/knowledge/retrieve-test', { query: retrieveQ, kind: retrieveKind, top_k: 5 })
      setRetrieveHits(r.data.hits)
    } catch (e) { toastError(e) }
  }

  return (
    <div className="glass-page">
      <PageHeader
        title="知识库（RAG）"
        description="FAQ 与文档切片向量化，问数时混合检索注入 few-shot 提示，越问越准"
      />
      <Card styles={{ header: { display: 'none' } }}>
      {faqsHook.ctx}{docsHook.ctx}
      <Tabs
        items={[
          {
            key: 'faq',
            label: `FAQ（${faqs.length}）`,
            children: (
              <div>
                <Button type="primary" icon={<PlusOutlined />} onClick={openCreate} style={{ marginBottom: 12 }}>
                  新建 FAQ
                </Button>
                <Table
                  rowKey="id"
                  dataSource={faqs}
                  pagination={{ pageSize: 10 }}
                  columns={[
                    { title: '问题', dataIndex: 'question' },
                    {
                      title: '答案',
                      dataIndex: 'answer',
                      render: (v: string) =>
                        v
                          ? <Typography.Text ellipsis={{ tooltip: v }} style={{ maxWidth: 280 }}>{v}</Typography.Text>
                          : <Typography.Text type="secondary">—</Typography.Text>,
                    },
                    { title: '分类', dataIndex: 'category', render: (v) => v ? <Tag>{v}</Tag> : '—' },
                    { title: '启用', dataIndex: 'enabled', render: (v) => (v ? <Tag color="green">是</Tag> : <Tag>否</Tag>) },
                    {
                      title: '操作',
                      render: (_, r) => (
                        <Space size={4}>
                          <Button size="small" onClick={() => openEdit(r)}>编辑</Button>
                          <Popconfirm title="删除该 FAQ？" onConfirm={async () => {
                            await client.delete(`/faqs/${r.id}`); loadFaqs()
                          }}>                            <Button size="small" danger icon={<DeleteOutlined />} />
                          </Popconfirm>
                        </Space>
                      ),
                    },
                  ]}
                />
                <Drawer title={editing ? '编辑 FAQ' : '新建 FAQ'} open={faqOpen} onClose={() => setFaqOpen(false)} width={460}>
                  <Form form={form} layout="vertical">
                    <Form.Item name="question" label="问题" rules={[{ required: true }]}>
                      <Input />
                    </Form.Item>
                    <Form.Item name="synonyms_json" label="同义问法（逗号分隔）">
                      <Input placeholder="如：上月销量, 上个月卖了多少" onChange={(e) =>
                        form.setFieldValue('synonyms_json', e.target.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean))} />
                    </Form.Item>
                    <Form.Item name="answer" label="答案（FAQ 问答对）" rules={[{ required: true, message: '请填写答案' }]}>
                      <Input.TextArea rows={3} placeholder="输入该问题的标准答案，问数命中后将直接返回" />
                    </Form.Item>
                    <Form.Item name="category" label="分类">
                      <Input />
                    </Form.Item>
                    <Form.Item name="table_ids" label="关联表（逗号分隔表名，可选）">
                      <Input />
                    </Form.Item>
                    <Button type="primary" onClick={submitFaq}>保存</Button>
                  </Form>
                </Drawer>
              </div>
            ),
          },
          {
            key: 'docs',
            label: `文档（${docs.length}）`,
            children: (
              <div>
                <Upload {...uploadProps}>
                  <Button type="primary" icon={<UploadOutlined />}>上传文档（txt/md/pdf/docx）</Button>
                </Upload>
                <Table
                  style={{ marginTop: 12 }}
                  rowKey="id"
                  dataSource={docs}
                  pagination={false}
                  columns={[
                    { title: '文档', dataIndex: 'name' },
                    { title: '类型', dataIndex: 'file_type', render: (v) => <Tag>{v}</Tag> },
                    { title: '大小', dataIndex: 'size', render: (v) => `${(v / 1024).toFixed(1)} KB` },
                    {
                      title: '状态',
                      dataIndex: 'status',
                      render: (v: string) => (
                        <Tag color={v === 'embedded' ? 'success' : v === 'failed' ? 'error' : 'processing'}>{v}</Tag>
                      ),
                    },
                    { title: '错误', dataIndex: 'error_msg', render: (v) => v ? <span style={{ color: '#dc2626', fontSize: 12 }}>{v}</span> : '—' },
                    {
                      title: '操作',
                      render: (_, r) => (
                        <Popconfirm title="删除文档及其切片？" onConfirm={async () => {
                          await client.delete(`/documents/${r.id}`); loadDocs()
                        }}>
                          <Button size="small" danger icon={<DeleteOutlined />}>删除</Button>
                        </Popconfirm>
                      ),
                    },
                  ]}
                />
              </div>
            ),
          },
          {
            key: 'retrieve',
            label: '检索测试台',
            children: (
              <div>
                <Space>
                  <Input
                    style={{ width: 320 }}
                    placeholder="输入测试问题"
                    value={retrieveQ}
                    onChange={(e) => setRetrieveQ(e.target.value)}
                    onPressEnter={retrieve}
                  />
                  <Button type="primary" icon={<SearchOutlined />} onClick={retrieve}>检索</Button>
                </Space>
                <div style={{ marginTop: 16 }}>
                  {retrieveHits.map((h) => (
                    <Card key={h.id} size="small" style={{ marginBottom: 8 }}>
                      <Space direction="vertical" style={{ width: '100%' }}>
                        <Tag color="blue">score: {h.score}</Tag>
                        <div style={{ fontSize: 13, whiteSpace: 'pre-wrap' }}>{String(h.content || (h.meta.kind ?? ''))}</div>
                      </Space>
                    </Card>
                  ))}
                  {!retrieveHits.length && retrieveQ ? <div style={{ color: '#94a3b8' }}>无命中</div> : null}
                </div>
              </div>
            ),
          },
        ]}
      />
      </Card>
    </div>
  )
}
