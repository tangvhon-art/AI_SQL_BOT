// 数据源管理：CRUD + 连接测试 + Schema 采集 + 进入 Schema 详情
import { useEffect, useState } from 'react'
import {
 Button, Card, Drawer, Form, Input, Popconfirm, Space, Table, Tag, message,
} from 'antd'
import { ApiOutlined, DatabaseOutlined, PlusOutlined, ReloadOutlined, ShareAltOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { client, errMsg } from '../api/client'
import type { Datasource } from '../types'
import PageHeader from '../components/PageHeader'
import { GlassSelect } from '../ui'

const TYPE_OPTIONS = [
  { label: 'MySQL', value: 'mysql' },
  { label: 'PostgreSQL', value: 'postgresql' },
  { label: 'ClickHouse', value: 'clickhouse' },
  { label: 'DuckDB', value: 'duckdb' },
]

export default function DatasourcePage() {
  const [msgApi, ctx] = message.useMessage()
  const nav = useNavigate()
  const [list, setList] = useState<Datasource[]>([])
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<Datasource | null>(null)
  const [form] = Form.useForm()
  const [testing, setTesting] = useState<number | null>(null)
  const [syncing, setSyncing] = useState<number | null>(null)

  const load = async () => {
    try {
      const r = await client.get<Datasource[]>('/datasources')
      setList(r.data)
    } catch (e) {
      msgApi.error(errMsg(e))
    }
  }
  useEffect(() => { load() }, [])

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    setOpen(true)
  }
  const openEdit = (ds: Datasource) => {
    setEditing(ds)
    form.setFieldsValue({ ...ds, password: '' })
    setOpen(true)
  }

  const submit = async () => {
    const values = await form.validateFields()
    const body = {
      name: values.name,
      type: values.type,
      host: values.host,
      port: values.port,
      db_name: values.db_name,
      user: values.user,
      password: values.password ?? '',
    }
    try {
      if (editing) await client.put(`/datasources/${editing.id}`, body)
      else await client.post('/datasources', body)
      msgApi.success('已保存')
      setOpen(false)
      load()
    } catch (e) {
      msgApi.error(errMsg(e))
    }
  }

  const testConn = async (id: number) => {
    setTesting(id)
    try {
      const r = await client.post(`/datasources/${id}/test`)
      msgApi.success(r.data.ok ? '连接成功' : `连接失败：${r.data.message}`)
      load()
    } catch (e) {
      msgApi.error(errMsg(e))
    } finally {
      setTesting(null)
    }
  }

  const syncSchema = async (id: number) => {
    setSyncing(id)
    try {
      const r = await client.post(`/datasources/${id}/sync-schema`)
      const s = r.data.stats
      msgApi.success(`采集完成：表 ${s.tables}，字段 ${s.columns}，外键关系 ${s.relationships}`)
      load()
    } catch (e) {
      msgApi.error(errMsg(e))
    } finally {
      setSyncing(null)
    }
  }

  const remove = async (id: number) => {
    try {
      await client.delete(`/datasources/${id}`)
      msgApi.success('已删除')
      load()
    } catch (e) {
      msgApi.error(errMsg(e))
    }
  }

  return (
    <div className="glass-page">
      <PageHeader
        title="数据源管理"
        description="配置业务数据库连接；采集 Schema 时自动获取表名/字段注释与外键关系图信息"
        extra={
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            新建数据源
          </Button>
        }
      />
      <Card styles={{ body: { padding: 0 }, header: { display: 'none' } }}>
      {ctx}
      <Table
        rowKey="id"
        dataSource={list}
        pagination={false}
        columns={[
          { title: '名称', dataIndex: 'name' },
          { title: '类型', dataIndex: 'type', render: (v) => <Tag>{v}</Tag> },
          { title: '地址', render: (_, r) => `${r.host}:${r.port}/${r.db_name}` },
          { title: '账号', dataIndex: 'user' },
          {
            title: '状态',
            dataIndex: 'status',
            render: (v: string) => (
              <Tag color={v === 'ok' ? 'success' : v === 'failed' ? 'error' : v === 'syncing' ? 'processing' : 'default'}>
                {v}
              </Tag>
            ),
          },
          { title: '最近同步', dataIndex: 'last_sync_at', render: (v) => v ? new Date(v).toLocaleString() : '—' },
          {
            title: '操作',
            render: (_, r) => (
              <Space size={4}>
                <Button size="small" icon={<ApiOutlined />} loading={testing === r.id} onClick={() => testConn(r.id)}>
                  测试
                </Button>
                <Button size="small" icon={<ReloadOutlined />} loading={syncing === r.id} onClick={() => syncSchema(r.id)}>
                  采集 Schema
                </Button>
                <Button size="small" icon={<ShareAltOutlined />} onClick={() => nav(`/datasources/${r.id}/schema`)}>
                  Schema
                </Button>
                <Button size="small" onClick={() => openEdit(r)}>编辑</Button>
                <Popconfirm title="确认删除该数据源及其元数据？" onConfirm={() => remove(r.id)}>
                  <Button size="small" danger>删除</Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Drawer title={editing ? '编辑数据源' : '新建数据源'} open={open} onClose={() => setOpen(false)} width={420}>
        <Form form={form} layout="vertical" initialValues={{ type: 'mysql', port: 3306, host: 'localhost' }}>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="如：生产 MySQL" />
          </Form.Item>
          <Form.Item name="type" label="类型" rules={[{ required: true }]}>
            <GlassSelect options={TYPE_OPTIONS} />
          </Form.Item>
          <Form.Item name="host" label="主机" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="port" label="端口" rules={[{ required: true }]}>
            <Input type="number" />
          </Form.Item>
          <Form.Item name="db_name" label="数据库" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="user" label="用户" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: !editing }]}>
            <Input.Password placeholder={editing ? '留空则不修改' : ''} />
          </Form.Item>
          <Space>
            <Button type="primary" icon={<DatabaseOutlined />} onClick={submit}>
              保存
            </Button>
            <Button onClick={() => setOpen(false)}>取消</Button>
          </Space>
        </Form>
      </Drawer>
      </Card>
    </div>
  )
}
