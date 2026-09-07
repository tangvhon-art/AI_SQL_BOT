// 知识库：FAQ + 文档（上传→切片→向量化）+ 检索测试台
// 两个列表均支持查询条件 + 服务端分页（page/size）
import { useState } from 'react'
import {
  Button, Card, Drawer, Form, Input, Popconfirm, Space, Table, Tabs, Tag, Typography, Upload,
} from 'antd'
import { DeleteOutlined, PlusOutlined, SearchOutlined, UploadOutlined } from '@ant-design/icons'
import type { UploadProps } from 'antd'
import { client } from '../api/client'
import type { Faq, KnowledgeDoc } from '../types'
import PageHeader from '../components/PageHeader'
import QueryBar from '../components/QueryBar'
import { GlassSelect } from '../ui'
import { tablePagination, usePagedList } from '../hooks/useCrudList'

const DOC_TYPE_OPTIONS = [
  { label: 'txt', value: 'txt' },
  { label: 'md', value: 'md' },
  { label: 'markdown', value: 'markdown' },
  { label: 'pdf', value: 'pdf' },
  { label: 'docx', value: 'docx' },
]
const DOC_STATUS_OPTIONS = [
  { label: '待处理', value: 'pending' },
  { label: '解析中', value: 'parsing' },
  { label: '已向量化', value: 'embedded' },
  { label: '失败', value: 'failed' },
]

export default function KnowledgePage() {
  const faqsHook = usePagedList<Faq>('/faqs')
  const docsHook = usePagedList<KnowledgeDoc>('/documents')
  const {
    items: faqs, total: faqTotal, page: faqPage, size: faqSize, loading: faqLoading,
    search: searchFaqs, reload: loadFaqs, onPageChange: faqPageChange, msgApi, toastError,
  } = faqsHook
  const {
    items: docs, total: docTotal, page: docPage, size: docSize, loading: docLoading,
    search: searchDocs, reload: loadDocs, onPageChange: docPageChange,
  } = docsHook
  const [faqOpen, setFaqOpen] = useState(false)
  const [editing, setEditing] = useState<Faq | null>(null)
  const [form] = Form.useForm()
  const [retrieveQ, setRetrieveQ] = useState('')
  const [retrieveKind, setRetrieveKind] = useState<'doc' | 'faq'>('doc')
  const [retrieveHits, setRetrieveHits] = useState<Array<{ id: string; score: number; meta: Record<string, unknown>; content?: string }>>([])
  // 查询条件
  const [fFaqQ, setFaqQ] = useState('')
  const [fFaqCategory, setFaqCategory] = useState('')
  const [fFaqEnabled, setFaqEnabled] = useState<string>('')
  const [fDocQ, setDocQ] = useState('')
  const [fDocType, setDocType] = useState<string>('')
  const [fDocStatus, setDocStatus] = useState<string>('')

  const doSearchFaqs = () => searchFaqs({
    q: fFaqQ.trim() || undefined,
    category: fFaqCategory.trim() || undefined,
    enabled: fFaqEnabled || undefined,
  })
  const doResetFaqs = () => {
    setFaqQ(''); setFaqCategory(''); setFaqEnabled('')
    searchFaqs({})
  }
  const doSearchDocs = () => searchDocs({
    q: fDocQ.trim() || undefined,
    file_type: fDocType || undefined,
    status: fDocStatus || undefined,
  })
  const doResetDocs = () => {
    setDocQ(''); setDocType(''); setDocStatus('')
    searchDocs({})
  }

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
            label: `FAQ（${faqTotal}）`,
            children: (
              <div>
                <Space style={{ marginBottom: 12 }}>
                  <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
                    新建 FAQ
                  </Button>
                </Space>
                <QueryBar onSearch={doSearchFaqs} onReset={doResetFaqs} loading={faqLoading}>
                  <Input
                    allowClear
                    placeholder="问题关键字"
                    style={{ width: 220 }}
                    value={fFaqQ}
                    onChange={(e) => setFaqQ(e.target.value)}
                    onPressEnter={doSearchFaqs}
                  />
                  <Input
                    allowClear
                    placeholder="分类"
                    style={{ width: 140 }}
                    value={fFaqCategory}
                    onChange={(e) => setFaqCategory(e.target.value)}
                    onPressEnter={doSearchFaqs}
                  />
                  <GlassSelect
                    allowClear
                    placeholder="启用状态"
                    style={{ width: 130 }}
                    value={fFaqEnabled || undefined}
                    onChange={(v) => setFaqEnabled(v ?? '')}
                    options={[{ label: '启用', value: '1' }, { label: '停用', value: '0' }]}
                  />
                </QueryBar>
                <Table
                  rowKey="id"
                  dataSource={faqs}
                  pagination={tablePagination(faqPage, faqSize, faqTotal, faqPageChange)}
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
            label: `文档（${docTotal}）`,
            children: (
              <div>
                <Upload {...uploadProps}>
                  <Button type="primary" icon={<UploadOutlined />}>上传文档（txt/md/pdf/docx）</Button>
                </Upload>
                <div style={{ marginTop: 12 }}>
                  <QueryBar onSearch={doSearchDocs} onReset={doResetDocs} loading={docLoading}>
                    <Input
                      allowClear
                      placeholder="文档名关键字"
                      style={{ width: 220 }}
                      value={fDocQ}
                      onChange={(e) => setDocQ(e.target.value)}
                      onPressEnter={doSearchDocs}
                    />
                    <GlassSelect
                      allowClear
                      placeholder="文件类型"
                      style={{ width: 130 }}
                      value={fDocType || undefined}
                      onChange={(v) => setDocType(v ?? '')}
                      options={DOC_TYPE_OPTIONS}
                    />
                    <GlassSelect
                      allowClear
                      placeholder="状态"
                      style={{ width: 140 }}
                      value={fDocStatus || undefined}
                      onChange={(v) => setDocStatus(v ?? '')}
                      options={DOC_STATUS_OPTIONS}
                    />
                  </QueryBar>
                </div>
                <Table
                  rowKey="id"
                  dataSource={docs}
                  pagination={tablePagination(docPage, docSize, docTotal, docPageChange)}
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
