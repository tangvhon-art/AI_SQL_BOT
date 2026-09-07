// 数据源管理：CRUD + 连接测试 + Schema 采集 + 进入 Schema 详情
import { useState } from 'react'
import {
  Button, Card, Drawer, Dropdown, Form, Input, Popconfirm, Space, Table, Tag,
} from 'antd'
import type { MenuProps } from 'antd'
import {
  ApiOutlined, DatabaseOutlined, DeleteOutlined, EditOutlined,
  MoreOutlined, PlusOutlined, ReloadOutlined, ShareAltOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { client } from '../api/client'
import type { Datasource } from '../types'
import PageHeader from '../components/PageHeader'
import { GlassSelect } from '../ui'
import { useCrudList } from '../hooks/useCrudList'

const TYPE_OPTIONS = [
  { label: 'MySQL', value: 'mysql' },
  { label: 'PostgreSQL', value: 'postgresql' },
  { label: 'ClickHouse', value: 'clickhouse' },
  { label: 'DuckDB', value: 'duckdb' },
]

const STATUS_MAP: Record<string, { text: string; color: string }> = {
  ok: { text: '正常', color: 'success' },
  failed: { text: '失败', color: 'error' },
  syncing: { text: '采集中', color: 'processing' },
  pending: { text: '待测试', color: 'default' },
}

export default function DatasourcePage() {
  const nav = useNavigate()
  const { data: list, load, msgApi, ctx, toastError } = useCrudList<Datasource>('/datasources')
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<Datasource | null>(null)
  const [form] = Form.useForm()
  const [testing, setTesting] = useState<number | null>(null)
  const [syncing, setSyncing] = useState<number | null>(null)

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
      toastError(e)
    }
  }

  const testConn = async (id: number) => {
    setTesting(id)
    try {
      const r = await client.post(`/datasources/${id}/test`)
      msgApi.success(r.data.ok ? '连接成功' : `连接失败：${r.data.message}`)
      load()
    } catch (e) {
      toastError(e)
    } finally {
      setTesting(null)
    }
  }

  const syncSchema = async (id: number) => {
    setSyncing(id)
    try {
      const r = await client.post(`/datasources/${id}/sync-schema`)
      const s = r.data.stats
      const inferred = s.inferred_relationships ? `，逻辑推断 ${s.inferred_relationships}` : ''
      msgApi.success(`采集完成：表 ${s.tables}，字段 ${s.columns}，外键关系 ${s.relationships}${inferred}`)
      load()
    } catch (e) {
      toastError(e)
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
      toastError(e)
    }
  }

  const moreMenu = (r: Datasource): MenuProps['items'] => [
    { key: 'edit', icon: <EditOutlined />, label: '编辑', onClick: () => openEdit(r) },
    { type: 'divider' },
    {
      key: 'delete',
      icon: <DeleteOutlined />,
      label: (
        <Popconfirm title="确认删除该数据源及其元数据？" onConfirm={() => remove(r.id)}>
          <span style={{ color: '#ff4d4f' }}>删除</span>
        </Popconfirm>
      ),
    },
  ]

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
      <Card styles={{ body: { padding: '12px 16px 16px' } }}>
        {ctx}
        <div style={{ marginBottom: 12, color: '#64748b', fontSize: 13 }}>
          共 <b style={{ color: '#1e293b' }}>{list.length}</b> 个数据源
        </div>
        <Table
          rowKey="id"
          dataSource={list}
          pagination={false}
          size="middle"
          scroll={{ x: 960 }}
          columns={[
            { title: '名称', dataIndex: 'name', width: 160, fixed: 'left',
              render: (v: string, r) => (
                <Space>
                  <DatabaseOutlined style={{ color: '#3b82f6' }} />
                  <span style={{ fontWeight: 500 }}>{v}</span>
                  <Tag style={{ margin: 0 }}>{r.type}</Tag>
                </Space>
              ) },
            { title: '连接地址', width: 220,
              render: (_, r) => <code style={{ fontSize: 12, color: '#475569' }}>{r.host}:{r.port}/{r.db_name}</code> },
            { title: '账号', dataIndex: 'user', width: 120 },
            {
              title: '状态',
              dataIndex: 'status',
              width: 100,
              render: (v: string) => {
                const s = STATUS_MAP[v] || { text: v || '未知', color: 'default' }
                return <Tag color={s.color}>{s.text}</Tag>
              },
            },
            { title: '最近同步', dataIndex: 'last_sync_at', width: 170,
              render: (v) => v ? new Date(v).toLocaleString('zh-CN', { hour12: false }) : '—' },
            {
              title: '操作',
              width: 260,
              fixed: 'right',
              render: (_, r) => (
                <Space size={4} wrap={false}>
                  <Button size="small" icon={<ApiOutlined />} loading={testing === r.id}
                    onClick={() => testConn(r.id)}>测试</Button>
                  <Button size="small" icon={<ReloadOutlined />} loading={syncing === r.id}
                    onClick={() => syncSchema(r.id)}>采集</Button>
                  <Button size="small" type="link" icon={<ShareAltOutlined />}
                    onClick={() => nav(`/datasources/${r.id}/schema`)}>Schema</Button>
                  <Dropdown menu={{ items: moreMenu(r) }} trigger={['click']} placement="bottomRight">
                    <Button size="small" icon={<MoreOutlined />} />
                  </Dropdown>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Drawer title={editing ? '编辑数据源' : '新建数据源'} open={open} onClose={() => setOpen(false)} width={440}>
        <Form form={form} layout="vertical" initialValues={{ type: 'mysql', port: 3306, host: 'localhost' }}>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="如：生产 MySQL" />
          </Form.Item>
          <Form.Item name="type" label="类型" rules={[{ required: true }]}>
            <GlassSelect options={TYPE_OPTIONS} />
          </Form.Item>
          <Space.Compact style={{ width: '100%' }}>
            <Form.Item name="host" label="主机" rules={[{ required: true }]} style={{ flex: 1 }}>
              <Input placeholder="localhost" />
            </Form.Item>
            <Form.Item name="port" label="端口" rules={[{ required: true }]} style={{ width: 110 }}>
              <Input type="number" />
            </Form.Item>
          </Space.Compact>
          <Form.Item name="db_name" label="数据库名" rules={[{ required: true }]}>
            <Input placeholder="如：AITS_hub" />
          </Form.Item>
          <Form.Item name="user" label="用户名" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: !editing }]}>
            <Input.Password placeholder={editing ? '留空则不修改' : ''} />
          </Form.Item>
          <Space>
            <Button type="primary" icon={<DatabaseOutlined />} onClick={submit}>保存</Button>
            <Button onClick={() => setOpen(false)}>取消</Button>
          </Space>
        </Form>
      </Drawer>
    </div>
  )
}
