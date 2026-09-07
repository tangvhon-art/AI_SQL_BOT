// Schema 详情：表/字段（comment 补录）+ 表关系
import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { Button, Card, Drawer, Form, Input, Popconfirm, Space, Table, Tabs, Tag } from 'antd'
import { PlusOutlined, SearchOutlined } from '@ant-design/icons'
import { client } from '../api/client'
import type { ColumnMeta, Relationship, TableMeta } from '../types'
import PageHeader from '../components/PageHeader'
import { GlassSelect } from '../ui'
import { useMessageApi } from '../hooks/useMessageApi'

export default function SchemaPage() {
  const { id } = useParams()
  const dsId = Number(id)
  const { msgApi, ctx, toastError } = useMessageApi()
  const [tables, setTables] = useState<TableMeta[]>([])
  const [selected, setSelected] = useState<TableMeta | null>(null)
  const [columns, setColumns] = useState<ColumnMeta[]>([])
  const [rels, setRels] = useState<Relationship[]>([])
  const [relOpen, setRelOpen] = useState(false)
  const [kw, setKw] = useState('')
  const [form] = Form.useForm()

  const loadAll = async () => {
    try {
      const [t, r] = await Promise.all([
        client.get<TableMeta[]>(`/datasources/${dsId}/tables`),
        client.get<Relationship[]>(`/datasources/${dsId}/relationships`),
      ])
      setTables(t.data)
      setRels(r.data)
      if (!selected && t.data.length) {
        setSelected(t.data[0])
      }
    } catch (err) {
      toastError(err)
    }
  }
  useEffect(() => { loadAll() }, [dsId])

  useEffect(() => {
    if (selected) {
      client.get<ColumnMeta[]>(`/datasources/tables/${selected.id}/columns`).then((r) => setColumns(r.data)).catch(() => {})
    }
  }, [selected])

  const updateComment = async (colId: number, comment: string) => {
    try {
      await client.put(`/datasources/columns/${colId}/comment`, { comment })
      msgApi.success('注释已保存')
      setColumns((cs) => cs.map((c) => (c.id === colId ? { ...c, comment } : c)))
    } catch (e) {
      toastError(e)
    }
  }

  const updateTableComment = async (tableId: number, comment: string) => {
    try {
      await client.put(`/datasources/tables/${tableId}/comment`, { comment })
      msgApi.success('表注释已保存')
      setTables((ts) => ts.map((t) => (t.id === tableId ? { ...t, comment } : t)))
      if (selected?.id === tableId) {
        setSelected((s) => s ? { ...s, comment } : s)
      }
    } catch (e) {
      toastError(e)
    }
  }

  const addRel = async () => {
    const v = await form.validateFields()
    try {
      await client.post(`/datasources/${dsId}/relationships`, v)
      msgApi.success('关系已添加')
      setRelOpen(false)
      loadAll()
    } catch (e) {
      toastError(e)
    }
  }

  const delRel = async (rid: number) => {
    try {
      await client.delete(`/datasources/relationships/${rid}`)
      msgApi.success('已删除')
      loadAll()
    } catch (e) {
      toastError(e)
    }
  }

  const [relSrcCols, setRelSrcCols] = useState<Array<{ label: string; value: number }>>([])
  const [relDstCols, setRelDstCols] = useState<Array<{ label: string; value: number }>>([])

  const loadColsFor = async (tblId: number, target: 'src' | 'dst') => {
    if (!tblId) return
    try {
      const r = await client.get<ColumnMeta[]>(`/datasources/tables/${tblId}/columns`)
      const opts = r.data.map((c) => ({ label: c.column_name, value: c.id }))
      if (target === 'src') {
        setRelSrcCols(opts)
        if (opts[0]) form.setFieldValue('src_col_id', opts[0].value)
      } else {
        setRelDstCols(opts)
        if (opts[0]) form.setFieldValue('dst_col_id', opts[0].value)
      }
    } catch { /* 忽略 */ }
  }

  return (
    <div className="glass-page">
      <PageHeader
        title={`Schema 详情（数据源 #${dsId}）`}
        description="表/字段注释、外键自动关系与手动关联、ER 关系图；注释缺失可在「字段详情」补录"
        extra={<Button onClick={loadAll}>刷新</Button>}
      />
      <Card styles={{ header: { display: 'none' } }}>
      {ctx}
      <Tabs
        items={[
          {
            key: 'tables',
            label: '表与字段',
            children: (
              <div>
                <Input.Search
                  allowClear
                  placeholder="搜索表名 / 表注释"
                  value={kw}
                  onChange={(e) => setKw(e.target.value)}
                  prefix={<SearchOutlined style={{ color: 'rgba(0,0,0,.35)' }} />}
                  style={{ maxWidth: 320, marginBottom: 12 }}
                />
                <Table
                  rowKey="id"
                  dataSource={tables.filter((t) => {
                    if (!kw.trim()) return true
                    const k = kw.trim().toLowerCase()
                    return t.table_name.toLowerCase().includes(k) || (t.comment ?? '').toLowerCase().includes(k)
                  })}
                  pagination={{ pageSize: 15 }}
                  columns={[
                    { title: '表名', dataIndex: 'table_name', render: (v: string, r) => (
                      <Button type="link" onClick={() => setSelected(r)}>{v}</Button>) },
                    { title: '表注释（可编辑）', dataIndex: 'comment', render: (v: string, r) => (
                      <Input
                        size="small"
                        defaultValue={v}
                        placeholder="点击编辑表注释"
                        onBlur={(e) => { if (e.target.value !== v) updateTableComment(r.id, e.target.value) }}
                        style={{ minWidth: 200 }}
                      />
                    ) },
                    { title: '类型', dataIndex: 'table_type', render: (v) => <Tag>{v}</Tag> },
                    { title: '字段数', dataIndex: 'column_count' },
                  ]}
                />
              </div>
            ),
          },
          {
            key: 'columns',
            label: `字段详情${selected ? `（${selected.table_name}）` : ''}`,
            children: selected ? (
              <Table
                rowKey="id"
                dataSource={columns}
                pagination={false}
                size="small"
                columns={[
                  { title: '字段', dataIndex: 'column_name', render: (v, r) => (
                    <Space>{v}{r.is_pk ? <Tag color="gold">PK</Tag> : null}</Space>) },
                  { title: '类型', dataIndex: 'data_type', render: (v) => <code>{v}</code> },
                  {
                    title: '注释（可补录）',
                    dataIndex: 'comment',
                    render: (v: string, r) => (
                      <Input
                        size="small"
                        defaultValue={v}
                        onBlur={(e) => { if (e.target.value !== v) updateComment(r.id, e.target.value) }}
                        style={{ minWidth: 220 }}
                      />
                    ),
                  },
                  { title: '可空', dataIndex: 'is_nullable', render: (v) => (v ? 'YES' : 'NO') },
                ]}
              />
            ) : <div style={{ padding: 30, color: '#94a3b8' }}>请选择表</div>,
          },
          {
            key: 'rels',
            label: '表关系',
            children: (
              <div>
                <Button type="primary" icon={<PlusOutlined />} onClick={() => { form.resetFields(); setRelOpen(true) }} style={{ marginBottom: 12 }}>
                  手动添加关联
                </Button>
                <Table
                  rowKey="id"
                  dataSource={rels}
                  pagination={false}
                  columns={[
                    { title: '源表.字段', render: (_, r) => <code>{r.src_table}.{r.src_col}</code> },
                    { title: '目标表.字段', render: (_, r) => <code>{r.dst_table}.{r.dst_col}</code> },
                    { title: '类型', dataIndex: 'rel_type' },
                    { title: '来源', dataIndex: 'source', render: (v) => (
                      <Tag color={v === 'fk_auto' ? 'blue' : v === 'inferred' ? 'orange' : 'green'}>
                        {v === 'fk_auto' ? '外键自动' : v === 'inferred' ? '逻辑推断' : '手动'}
                      </Tag>) },
                    { title: '操作', render: (_, r) => (
                      <Popconfirm title="删除该关系？" onConfirm={() => delRel(r.id)}>
                        <Button size="small" danger>删除</Button>
                      </Popconfirm>) },
                  ]}
                />
                <Drawer title="添加表关联" open={relOpen} onClose={() => setRelOpen(false)} width={380}>
                  <Form form={form} layout="vertical">
                    <Form.Item name="src_table_id" label="源表" rules={[{ required: true }]}>
                      <GlassSelect options={tables.map((t) => ({ label: t.table_name, value: t.id }))}
                        onChange={(tid: number) => loadColsFor(tid, 'src')} />
                    </Form.Item>
                    <Form.Item name="src_col_id" label="源字段" rules={[{ required: true }]}>
                      <GlassSelect options={relSrcCols} showSearch
                        filterOption={(input, opt) => (opt?.label as string ?? '').toLowerCase().includes(input.toLowerCase())} />
                    </Form.Item>
                    <Form.Item name="dst_table_id" label="目标表" rules={[{ required: true }]}>
                      <GlassSelect options={tables.map((t) => ({ label: t.table_name, value: t.id }))}
                        onChange={(tid: number) => loadColsFor(tid, 'dst')} />
                    </Form.Item>
                    <Form.Item name="dst_col_id" label="目标字段" rules={[{ required: true }]}>
                      <GlassSelect options={relDstCols} showSearch
                        filterOption={(input, opt) => (opt?.label as string ?? '').toLowerCase().includes(input.toLowerCase())} />
                    </Form.Item>
                    <Form.Item name="rel_type" label="关系类型" initialValue="1:N">
                      <GlassSelect options={[{ label: '1:N', value: '1:N' }, { label: '1:1', value: '1:1' }, { label: 'N:M', value: 'N:M' }]} />
                    </Form.Item>
                    <Button type="primary" onClick={addRel}>保存</Button>
                  </Form>
                </Drawer>
              </div>
            ),
          },
        ]}
      />
      </Card>
    </div>
  )
}
