// 字段级权限：规则 CRUD（角色/用户 × allow/deny）+ 生效预览（可查并集/不可查并集/黑名单优先 + 命中规则筛选表格）
import { useEffect, useMemo, useState } from 'react'
import {
  Button, Card, Drawer, Form, Input, InputNumber, Popconfirm, Radio, Select, Space, Switch, Table, Tag, message,
} from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import { client, errMsg } from '../api/client'
import type { ColumnMeta, Datasource, PermissionRule, TableMeta } from '../types'
import PageHeader from '../components/PageHeader'
import { GlassSelect } from '../ui'

interface PreviewRule {
  id: number
  scope_type: string
  scope_id: number
  scope_name: string
  rule_type: string
  datasource_id: number
  datasource_name: string
  table_id: number
  table_name: string
  column_id: number | null
  column_name: string
  enabled: boolean
}

export default function PermissionPage() {
  const [msgApi, ctx] = message.useMessage()
  const [rules, setRules] = useState<PermissionRule[]>([])
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<PermissionRule | null>(null)
  const [form] = Form.useForm()
  const scopeType = Form.useWatch('scope_type', form)
  const [dsList, setDsList] = useState<Datasource[]>([])
  const [tables, setTables] = useState<TableMeta[]>([])
  const [cols, setCols] = useState<Array<{ label: string; value: number }>>([])
  const [roles, setRoles] = useState<Array<{ id: number; code: string; name: string }>>([])
  const [users, setUsers] = useState<Array<{ id: number; username: string; display_name: string }>>([])
  const [previewUser, setPreviewUser] = useState<number | null>(null)
  const [preview, setPreview] = useState<{ allow: unknown[]; deny: unknown[]; rules: PreviewRule[] } | null>(null)
  // 生效预览筛选条件
  const [fScopeType, setFScopeType] = useState<string>('')
  const [fRuleType, setFRuleType] = useState<string>('')
  const [fDatasource, setFDatasource] = useState<number | undefined>()
  const [fTable, setFTable] = useState<string>('')
  const [fColumn, setFColumn] = useState<string>('')
  const [fEnabled, setFEnabled] = useState<string>('')

  const load = async () => {
    try {
      const r = await client.get<PermissionRule[]>('/permission-rules')
      setRules(r.data)
    } catch (e) { msgApi.error(errMsg(e)) }
  }
  useEffect(() => {
    load()
    client.get<Datasource[]>('/datasources').then((r) => setDsList(r.data)).catch(() => {})
    client.get('/roles').then((r) => setRoles(r.data)).catch(() => {})
    client.get('/users').then((r) => setUsers(r.data)).catch(() => {})
  }, [])

  const loadTables = async (dsId: number) => {
    try {
      const r = await client.get<TableMeta[]>(`/datasources/${dsId}/tables`)
      setTables(r.data)
    } catch { setTables([]) }
  }
  const loadCols = async (tblId: number) => {
    try {
      const r = await client.get<ColumnMeta[]>(`/datasources/tables/${tblId}/columns`)
      setCols([{ label: '（整表）', value: 0 }, ...r.data.map((c) => ({ label: `${c.column_name}（${c.comment || '无注释'}）`, value: c.id }))])
    } catch { setCols([]) }
  }

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    setTables([])
    setCols([])
    form.setFieldsValue({ scope_type: 'role', rule_type: 'deny', enabled: true, column_ids: [] })
    setOpen(true)
  }
  const openEdit = (r: PermissionRule) => {
    setEditing(r)
    form.setFieldsValue({ ...r, scope_ids: [r.scope_id], column_ids: r.column_ids || [] })
    loadTables(r.datasource_id)
    if (r.table_id) loadCols(r.table_id)
    setOpen(true)
  }

  const submit = async () => {
    const v = await form.validateFields()
    const ids: number[] = Array.isArray(v.scope_ids) ? v.scope_ids.filter((x: number) => x != null) : [v.scope_id]
    if (!ids.length) { msgApi.error('请选择角色/用户'); return }
    const colIds: number[] = Array.isArray(v.column_ids) ? v.column_ids.filter((x: number) => x != null) : []
    const base = {
      rule_type: v.rule_type,
      datasource_id: v.datasource_id,
      table_id: v.table_id,
      column_ids: colIds,
      enabled: v.enabled ?? true,
    }
    try {
      if (editing) {
        // 编辑：单条规则更新，column_ids 为多字段数组
        await client.put(`/permission-rules/${editing.id}`, { ...base, scope_type: v.scope_type, scope_id: ids[0] })
        msgApi.success('已保存')
      } else {
        // 新建：角色/用户可多选批量创建，每条规则包含多字段数组
        for (const id of ids) {
          await client.post('/permission-rules', { ...base, scope_type: v.scope_type, scope_id: id })
        }
        msgApi.success(`已创建 ${ids.length} 条规则`)
      }
      setOpen(false)
      load()
    } catch (e) { msgApi.error(errMsg(e)) }
  }

  const previewEffective = async () => {
    if (!previewUser) return
    try {
      const r = await client.get('/permission-rules/effective', { params: { user_id: previewUser } })
      setPreview(r.data)
      // 重置筛选
      setFScopeType(''); setFRuleType(''); setFDatasource(undefined); setFTable(''); setFColumn(''); setFEnabled('')
    } catch (e) { msgApi.error(errMsg(e)) }
  }

  // 作用域名称展示（不显示 ID）
  const scopeLabel = (r: { scope_type: string; scope_id: number }) => {
    if (r.scope_type === 'role') {
      const role = roles.find((x) => x.id === r.scope_id)
      return `角色：${role?.name || r.scope_id}`
    }
    const u = users.find((x) => x.id === r.scope_id)
    return `用户：${u?.display_name || u?.username || r.scope_id}`
  }

  // 预览筛选后的规则
  const filteredPreviewRules = useMemo(() => {
    if (!preview?.rules) return []
    return preview.rules.filter((r) => {
      if (fScopeType && r.scope_type !== fScopeType) return false
      if (fRuleType && r.rule_type !== fRuleType) return false
      if (fDatasource && r.datasource_id !== fDatasource) return false
      if (fTable && !r.table_name.toLowerCase().includes(fTable.toLowerCase())) return false
      if (fColumn && !r.column_name.toLowerCase().includes(fColumn.toLowerCase())) return false
      if (fEnabled === '1' && !r.enabled) return false
      if (fEnabled === '0' && r.enabled) return false
      return true
    })
  }, [preview, fScopeType, fRuleType, fDatasource, fTable, fColumn, fEnabled])

  return (
    <div className="glass-page">
      <PageHeader
        title="字段级权限控制"
        description="可查并集（角色 ∪ 用户）/ 不可查并集（角色 ∪ 用户），黑名单优先；问数命中受限字段注入 AND 1=2，全部可查注入 AND 1=1"
        extra={<Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建规则</Button>}
      />
      <Card styles={{ header: { display: 'none' } }}>
      {ctx}
      {/* 生效预览 */}
      <div style={{ marginBottom: 16, padding: 12, background: 'rgba(0,0,0,0.02)', borderRadius: 8 }}>
        <Space wrap>
          <span style={{ fontWeight: 600 }}>权限生效预览（用户 ID）：</span>
          <InputNumber min={1} value={previewUser ?? undefined} onChange={(v) => setPreviewUser(v ?? null)} />
          <Button type="primary" onClick={previewEffective}>查询</Button>
          {preview ? (
            <>
              <Tag color="green">可查并集 {preview.allow.length} 个字段</Tag>
              <Tag color="red">不可查并集（黑名单优先）{preview.deny.length} 个字段</Tag>
              <Tag color="blue">命中规则 {preview.rules.length} 条</Tag>
            </>
          ) : null}
        </Space>
        {preview ? (
          <div style={{ marginTop: 12 }}>
            {/* 筛选栏 */}
            <Space wrap style={{ marginBottom: 8 }}>
              <Select placeholder="作用域" allowClear style={{ width: 110 }} value={fScopeType || undefined} onChange={(v) => setFScopeType(v || '')}
                options={[{ label: '角色', value: 'role' }, { label: '用户', value: 'user' }]} />
              <Select placeholder="类型" allowClear style={{ width: 100 }} value={fRuleType || undefined} onChange={(v) => setFRuleType(v || '')}
                options={[{ label: '可查', value: 'allow' }, { label: '不可查', value: 'deny' }]} />
              <Select placeholder="数据源" allowClear style={{ width: 160 }} value={fDatasource} onChange={(v) => setFDatasource(v)}
                options={dsList.map((d) => ({ label: d.name, value: d.id }))} />
              <Input placeholder="表名搜索" allowClear style={{ width: 140 }} value={fTable} onChange={(e) => setFTable(e.target.value)} />
              <Input placeholder="字段搜索" allowClear style={{ width: 140 }} value={fColumn} onChange={(e) => setFColumn(e.target.value)} />
              <Select placeholder="是否启用" allowClear style={{ width: 100 }} value={fEnabled || undefined} onChange={(v) => setFEnabled(v || '')}
                options={[{ label: '启用', value: '1' }, { label: '停用', value: '0' }]} />
            </Space>
            <Table
              rowKey="id"
              size="small"
              dataSource={filteredPreviewRules}
              pagination={{ pageSize: 8, showSizeChanger: false }}
              locale={{ emptyText: '无命中规则' }}
              columns={[
                { title: '作用域', dataIndex: 'scope_name', render: (_, r) => (
                  <Tag color={r.scope_type === 'role' ? 'purple' : 'blue'}>{scopeLabel(r)}</Tag>) },
                { title: '类型', dataIndex: 'rule_type', render: (v) => (
                  <Tag color={v === 'allow' ? 'green' : 'red'}>{v === 'allow' ? '可查' : '不可查'}</Tag>) },
                { title: '数据源', dataIndex: 'datasource_name' },
                { title: '表', dataIndex: 'table_name' },
                { title: '字段', dataIndex: 'column_names', render: (names: string[]) => (
                  (names || []).map((n, i) => <Tag key={i} color={n === '（整表）' ? 'orange' : 'default'} style={{ marginBottom: 2 }}>{n}</Tag>)) },
                { title: '启用', dataIndex: 'enabled', render: (v) => (v ? '是' : '否'), width: 60 },
              ]}
            />
          </div>
        ) : null}
      </div>

      {/* 全部规则列表 */}
      <Table
        rowKey="id"
        dataSource={rules}
        pagination={false}
        columns={[
          {
            title: '作用域', render: (_, r) => (
              <Tag color={r.scope_type === 'role' ? 'purple' : 'blue'}>{scopeLabel(r)}</Tag>),
          },
          { title: '类型', dataIndex: 'rule_type', render: (v) => (
            <Tag color={v === 'allow' ? 'green' : 'red'}>{v === 'allow' ? '可查' : '不可查'}</Tag>) },
          { title: '数据源', dataIndex: 'datasource_name' },
          { title: '表', dataIndex: 'table_name' },
          { title: '字段', dataIndex: 'column_names', render: (names: string[]) => (
            (names || []).map((n, i) => <Tag key={i} color={n === '（整表）' ? 'orange' : 'default'} style={{ marginBottom: 2 }}>{n}</Tag>)) },
          { title: '启用', dataIndex: 'enabled', render: (v) => (v ? '是' : '否') },
          {
            title: '操作',
            render: (_, r) => (
              <Space size={4}>
                <Button size="small" onClick={() => openEdit(r)}>编辑</Button>
                <Popconfirm title="删除规则？" onConfirm={async () => { await client.delete(`/permission-rules/${r.id}`); load() }}>
                  <Button size="small" danger>删除</Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Drawer title={editing ? '编辑权限规则' : '新建权限规则'} open={open} onClose={() => setOpen(false)} width={420}>
        <Form form={form} layout="vertical" initialValues={{ scope_type: 'role', rule_type: 'deny', enabled: true }}>
          <Form.Item name="scope_type" label="作用域" rules={[{ required: true }]}>
            <Radio.Group
              options={[{ label: '角色全局', value: 'role' }, { label: '用户级', value: 'user' }]}
              optionType="button"
              onChange={() => form.setFieldValue('scope_ids', undefined)}
            />
          </Form.Item>
          <Form.Item
            name="scope_ids"
            label={editing ? '角色/用户' : '角色/用户（可多选，批量创建）'}
            rules={[{ required: true, message: '请选择角色/用户' }]}
          >
            <GlassSelect
              mode={editing ? undefined : 'multiple'}
              style={{ width: '100%' }}
              placeholder={scopeType === 'user' ? '选择用户' : '选择角色'}
              options={scopeType === 'user'
                ? users.map((u) => ({ label: `${u.display_name || u.username}（${u.username}）`, value: u.id }))
                : roles.map((r) => ({ label: `${r.name}（${r.code}）`, value: r.id }))}
            />
          </Form.Item>
          <Form.Item name="rule_type" label="规则类型" rules={[{ required: true }]}>
            <Radio.Group options={[{ label: '可查 allow', value: 'allow' }, { label: '不可查 deny', value: 'deny' }]} optionType="button" />
          </Form.Item>
          <Form.Item name="datasource_id" label="数据源" rules={[{ required: true }]}>
            <GlassSelect
              showSearch
              optionFilterProp="label"
              placeholder="搜索数据源名称"
              options={dsList.map((d) => ({ label: d.name, value: d.id }))}
              onChange={(v: number) => { form.setFieldValue('table_id', undefined); form.setFieldValue('column_ids', []); loadTables(v) }} />
          </Form.Item>
          <Form.Item name="table_id" label="表" rules={[{ required: true }]}>
            <GlassSelect
              showSearch
              optionFilterProp="label"
              placeholder="搜索表名/注释"
              options={tables.map((t) => ({ label: `${t.table_name}（${t.comment || '无注释'}）`, value: t.id }))}
              onChange={(v: number) => { form.setFieldValue('column_ids', []); loadCols(v) }} />
          </Form.Item>
          <Form.Item name="column_ids" label={editing ? '字段（选「整表」表示整表规则）' : '字段（可多选，选「整表」表示整表规则，与单字段互斥）'} rules={[{ required: true, message: '请选择字段' }]}>
            <GlassSelect
              mode="multiple"
              showSearch
              optionFilterProp="label"
              style={{ width: '100%' }}
              placeholder="搜索字段名/注释（可多选）"
              options={cols}
              onChange={(vals: number[]) => {
                // 整表（value=0）与单字段互斥：选了整表就清空其他，选了单字段就清空整表
                if (vals.includes(0)) {
                  form.setFieldValue('column_ids', [0])
                }
              }}
            />
          </Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Button type="primary" onClick={submit}>保存</Button>
        </Form>
      </Drawer>
      </Card>
    </div>
  )
}
