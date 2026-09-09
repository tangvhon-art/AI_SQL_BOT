// 权限控制：字段级（列级）规则 CRUD（角色/用户/用户组 × allow/deny）+ 行级权限（C3）配置与生效预览
// - 字段级：可查并集/不可查并集（黑名单优先），命中受限字段注入 AND 1=2，全部可查注入 AND 1=1
// - 行级：row_enabled 开关 + 行过滤条件（sql / template 两种类型，支持 {user_id}/{group_ids}/{role_code} 占位）
// - 预览 Tab：字段级生效预览（命中规则筛选表）+ 行级生效预览（row-effective，按表列出生效条件）
import { useMemo, useState } from 'react'
import {
  Button, Card, Collapse, Drawer, Form, Input, InputNumber, Popconfirm, Radio, Select, Space, Switch, Table, Tabs, Tag, Tooltip, Typography,
} from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import { client } from '../api/client'
import type { ColumnMeta, Datasource, PermissionRule, TableMeta } from '../types'
import PageHeader from '../components/PageHeader'
import QueryBar from '../components/QueryBar'
import { GlassSelect } from '../ui'
import { tablePagination, usePagedList } from '../hooks/useCrudList'
import { useOptions } from '../hooks/useOptions'

const TypographyText = Typography.Text

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

interface RowEffectiveItem {
  datasource_id: number
  table_id: number
  table_name: string
  rules: Array<{ rule_id: number; rule_type: string; condition: string; condition_type: string; note: string }>
}

// 行级条件模板占位说明（tooltip 展示）
const ROW_FILTER_HINT =
  'WHERE 片段（不含 WHERE 关键字）。template 类型支持占位：{user_id} 当前用户ID、{group_ids} 所属组ID列表（无组为0）、{role_code} 角色编码（自动加引号转义）。示例：store_id IN (SELECT store_id FROM store_scope WHERE manager_user_id = {user_id})'

export default function PermissionPage() {
  const rulesHook = usePagedList<PermissionRule>('/permission-rules')
  const { items: rules, total, page, size, loading, search, reload: loadRules, onPageChange, msgApi, ctx, toastError } = rulesHook
  const dsList = useOptions<Datasource>('datasources')
  const roles = useOptions<{ id: number; code: string; name: string }>('roles')
  const users = useOptions<{ id: number; username: string; display_name: string }>('users')
  const groups = useOptions<{ id: number; name: string }>('user-groups')
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<PermissionRule | null>(null)
  const [form] = Form.useForm()
  const scopeType = Form.useWatch('scope_type', form)
  const rowEnabled = Form.useWatch('row_enabled', form)
  const [tables, setTables] = useState<TableMeta[]>([])
  const [cols, setCols] = useState<Array<{ label: string; value: number }>>([])
  const [previewUser, setPreviewUser] = useState<number | null>(null)
  const [preview, setPreview] = useState<{ allow: unknown[]; deny: unknown[]; rules: PreviewRule[] } | null>(null)
  // 行级生效预览
  const [rowPreviewUser, setRowPreviewUser] = useState<number | null>(null)
  const [rowPreview, setRowPreview] = useState<{ user_id: number; items: RowEffectiveItem[] } | null>(null)
  const [rowPreviewLoading, setRowPreviewLoading] = useState(false)
  // 生效预览筛选条件（字段级）
  const [fScopeType, setFScopeType] = useState<string>('')
  const [fRuleType, setFRuleType] = useState<string>('')
  const [fDatasource, setFDatasource] = useState<number | undefined>()
  const [fTable, setFTable] = useState<string>('')
  const [fColumn, setFColumn] = useState<string>('')
  const [fEnabled, setFEnabled] = useState<string>('')
  // 规则列表查询条件
  const [rScopeType, setRScopeType] = useState<string>('')
  const [rRuleType, setRRuleType] = useState<string>('')
  const [rDatasourceId, setRDatasourceId] = useState<number | undefined>()
  const [rTable, setRTable] = useState('')
  const [rEnabled, setREnabled] = useState<string>('')

  const doSearchRules = () => search({
    scope_type: rScopeType || undefined,
    rule_type: rRuleType || undefined,
    datasource_id: rDatasourceId,
    table: rTable.trim() || undefined,
    enabled: rEnabled || undefined,
  })
  const doResetRules = () => {
    setRScopeType(''); setRRuleType(''); setRDatasourceId(undefined); setRTable(''); setREnabled('')
    search({})
  }

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
    form.setFieldsValue({ scope_type: 'role', rule_type: 'deny', enabled: true, column_ids: [], row_enabled: false, row_filter_type: 'sql' })
    setOpen(true)
  }
  const openEdit = (r: PermissionRule) => {
    setEditing(r)
    form.setFieldsValue({
      ...r, scope_ids: [r.scope_id], column_ids: r.column_ids || [],
      row_enabled: !!r.row_enabled, row_filter_type: r.row_filter_type || 'sql',
      row_filter: r.row_filter ?? '', row_filter_note: r.row_filter_note ?? '',
    })
    loadTables(r.datasource_id)
    if (r.table_id) loadCols(r.table_id)
    setOpen(true)
  }

  const submit = async () => {
    const v = await form.validateFields()
    const ids: number[] = Array.isArray(v.scope_ids) ? v.scope_ids.filter((x: number) => x != null) : [v.scope_id]
    if (!ids.length) { msgApi.error('请选择角色/用户/用户组'); return }
    const colIds: number[] = Array.isArray(v.column_ids) ? v.column_ids.filter((x: number) => x != null) : []
    const rowFilter = (v.row_filter ?? '').trim()
    const base = {
      rule_type: v.rule_type,
      datasource_id: v.datasource_id,
      table_id: v.table_id,
      column_ids: colIds,
      enabled: v.enabled ?? true,
      row_enabled: !!v.row_enabled,
      row_filter: rowFilter || null,
      row_filter_type: v.row_filter_type || 'sql',
      row_filter_note: v.row_filter_note ?? '',
    }
    try {
      if (editing) {
        // 编辑：单条规则更新，column_ids 为多字段数组
        await client.put(`/permission-rules/${editing.id}`, { ...base, scope_type: v.scope_type, scope_id: ids[0] })
        msgApi.success('已保存')
      } else {
        // 新建：角色/用户/用户组可多选批量创建
        for (const id of ids) {
          await client.post('/permission-rules', { ...base, scope_type: v.scope_type, scope_id: id })
        }
        msgApi.success(`已创建 ${ids.length} 条规则`)
      }
      setOpen(false)
      loadRules()
    } catch (e) { toastError(e) }
  }

  const previewEffective = async () => {
    if (!previewUser) return
    try {
      const r = await client.get('/permission-rules/effective', { params: { user_id: previewUser } })
      setPreview(r.data)
      setFScopeType(''); setFRuleType(''); setFDatasource(undefined); setFTable(''); setFColumn(''); setFEnabled('')
    } catch (e) { toastError(e) }
  }

  const previewRowEffective = async () => {
    if (!rowPreviewUser) return
    setRowPreviewLoading(true)
    try {
      const r = await client.get('/permission-rules/row-effective', { params: { user_id: rowPreviewUser } })
      setRowPreview(r.data)
    } catch (e) { toastError(e) } finally { setRowPreviewLoading(false) }
  }

  // 作用域名称展示（不显示 ID）
  const scopeLabel = (r: { scope_type: string; scope_id: number }) => {
    if (r.scope_type === 'role') {
      const role = roles.find((x) => x.id === r.scope_id)
      return `角色：${role?.name || r.scope_id}`
    }
    if (r.scope_type === 'group') {
      const g = groups.find((x) => x.id === r.scope_id)
      return `用户组：${g?.name || r.scope_id}`
    }
    const u = users.find((x) => x.id === r.scope_id)
    return `用户：${u?.display_name || u?.username || r.scope_id}`
  }

  // 预览筛选后的规则（字段级）
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

  // 作用域选项（按 scope_type）
  const scopeOptions = useMemo(() => {
    if (scopeType === 'user') return users.map((u) => ({ label: `${u.display_name || u.username}（${u.username}）`, value: u.id }))
    if (scopeType === 'group') return groups.map((g) => ({ label: `${g.name}（组）`, value: g.id }))
    return roles.map((r) => ({ label: `${r.name}（${r.code}）`, value: r.id }))
  }, [scopeType, users, groups, roles])

  const scopePlaceholder = scopeType === 'user' ? '选择用户' : scopeType === 'group' ? '选择用户组' : '选择角色'

  return (
    <div className="glass-page">
      <PageHeader
        title="权限控制"
        description="字段级：可查并集 / 不可查并集（黑名单优先），命中受限字段注入 AND 1=2；行级：行过滤条件注入 WHERE（deny 优先，支持 {user_id}/{group_ids}/{role_code} 模板）"
        extra={<Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建规则</Button>}
      />
      <Card styles={{ header: { display: 'none' } }}>
      {ctx}
      {/* 生效预览（字段级 + 行级 Tab） */}
      <div style={{ marginBottom: 16, padding: 12, background: 'rgba(0,0,0,0.02)', borderRadius: 8 }}>
        <Tabs
          size="small"
          items={[
            {
              key: 'col',
              label: '字段级生效预览',
              children: (
                <div>
                  <Space wrap>
                    <span style={{ fontWeight: 600 }}>用户 ID：</span>
                    <InputNumber min={1} value={previewUser ?? undefined} onChange={(v) => setPreviewUser(v ?? null)} />
                    <Button type="primary" onClick={previewEffective}>查询</Button>
                    {preview ? (
                      <>
                        <Tag color="green">可查并集 {preview.allow.length} 个字段</Tag>
                        <Tag color="red">不可查并集 {preview.deny.length} 个字段</Tag>
                        <Tag color="blue">命中规则 {preview.rules.length} 条</Tag>
                      </>
                    ) : null}
                  </Space>
                  {preview ? (
                    <div style={{ marginTop: 12 }}>
                      <Space wrap style={{ marginBottom: 8 }}>
                        <Select placeholder="作用域" allowClear style={{ width: 110 }} value={fScopeType || undefined} onChange={(v) => setFScopeType(v || '')}
                          options={[{ label: '角色', value: 'role' }, { label: '用户', value: 'user' }, { label: '用户组', value: 'group' }]} />
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
                            <Tag color={r.scope_type === 'role' ? 'purple' : r.scope_type === 'group' ? 'geekblue' : 'blue'}>{scopeLabel(r)}</Tag>) },
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
              ),
            },
            {
              key: 'row',
              label: '行级生效预览',
              children: (
                <div>
                  <Space wrap>
                    <span style={{ fontWeight: 600 }}>用户 ID：</span>
                    <InputNumber min={1} value={rowPreviewUser ?? undefined} onChange={(v) => setRowPreviewUser(v ?? null)} />
                    <Button type="primary" loading={rowPreviewLoading} onClick={previewRowEffective}>查询</Button>
                    {rowPreview ? (
                      <>
                        <Tag color="geekblue">生效行级表 {rowPreview.items.length} 张</Tag>
                        <Tag color="orange">deny 优先于 allow</Tag>
                      </>
                    ) : null}
                  </Space>
                  {rowPreview ? (
                    <div style={{ marginTop: 12 }}>
                      {rowPreview.items.length === 0 ? (
                        <TypographyText type="secondary">该用户暂无生效的行级规则</TypographyText>
                      ) : (
                        <Table
                          rowKey="table_id"
                          size="small"
                          dataSource={rowPreview.items}
                          pagination={false}
                          expandable={{
                            expandedRowRender: (r) => (
                              <div style={{ padding: '4px 8px' }}>
                                {r.rules.map((rule) => (
                                  <div key={rule.rule_id} style={{ marginBottom: 8, padding: 8, background: 'rgba(0,0,0,0.03)', borderRadius: 6 }}>
                                    <Space size={8} style={{ marginBottom: 4 }}>
                                      <Tag color={rule.rule_type === 'deny' ? 'red' : 'green'}>
                                        {rule.rule_type === 'deny' ? 'deny（黑名单）' : 'allow（白名单）'}
                                      </Tag>
                                      <Tag>{rule.condition_type === 'template' ? '模板' : 'SQL'}</Tag>
                                      {rule.note ? <TypographyText type="secondary" style={{ fontSize: 12 }}>{rule.note}</TypographyText> : null}
                                    </Space>
                                    <code style={{ fontSize: 12, wordBreak: 'break-all' }}>{rule.condition}</code>
                                  </div>
                                ))}
                              </div>
                            ),
                          }}
                          columns={[
                            { title: '数据源 ID', dataIndex: 'datasource_id', width: 100 },
                            { title: '表', dataIndex: 'table_name', render: (v, r) => <TypographyText strong>{v}</TypographyText> },
                            { title: '生效规则数', render: (_, r) => <Tag color="blue">{r.rules.length} 条</Tag>, width: 110 },
                          ]}
                        />
                      )}
                    </div>
                  ) : null}
                </div>
              ),
            },
          ]}
        />
      </div>

      {/* 全部规则列表 */}
      <QueryBar onSearch={doSearchRules} onReset={doResetRules} loading={loading}>
        <Select placeholder="作用域" allowClear style={{ width: 110 }} value={rScopeType || undefined} onChange={(v) => setRScopeType(v || '')}
          options={[{ label: '角色', value: 'role' }, { label: '用户', value: 'user' }, { label: '用户组', value: 'group' }]} />
        <Select placeholder="类型" allowClear style={{ width: 100 }} value={rRuleType || undefined} onChange={(v) => setRRuleType(v || '')}
          options={[{ label: '可查', value: 'allow' }, { label: '不可查', value: 'deny' }]} />
        <Select placeholder="数据源" allowClear style={{ width: 160 }} value={rDatasourceId} onChange={(v) => setRDatasourceId(v)}
          options={dsList.map((d) => ({ label: d.name, value: d.id }))} />
        <Input placeholder="表名搜索" allowClear style={{ width: 140 }} value={rTable} onChange={(e) => setRTable(e.target.value)} onPressEnter={doSearchRules} />
        <Select placeholder="是否启用" allowClear style={{ width: 100 }} value={rEnabled || undefined} onChange={(v) => setREnabled(v || '')}
          options={[{ label: '启用', value: '1' }, { label: '停用', value: '0' }]} />
      </QueryBar>
      <Table
        rowKey="id"
        dataSource={rules}
        pagination={tablePagination(page, size, total, onPageChange)}
        columns={[
          {
            title: '作用域', render: (_, r) => (
              <Tag color={r.scope_type === 'role' ? 'purple' : r.scope_type === 'group' ? 'geekblue' : 'blue'}>{scopeLabel(r)}</Tag>),
          },
          { title: '类型', dataIndex: 'rule_type', render: (v) => (
            <Tag color={v === 'allow' ? 'green' : 'red'}>{v === 'allow' ? '可查' : '不可查'}</Tag>) },
          { title: '数据源', dataIndex: 'datasource_name' },
          { title: '表', dataIndex: 'table_name' },
          { title: '字段', dataIndex: 'column_names', render: (names: string[]) => (
            (names || []).map((n, i) => <Tag key={i} color={n === '（整表）' ? 'orange' : 'default'} style={{ marginBottom: 2 }}>{n}</Tag>)) },
          {
            title: '行级', dataIndex: 'row_enabled', width: 160,
            render: (v: boolean, r) => v ? (
              <Space size={4} wrap>
                <Tag color="geekblue">行级</Tag>
                <Tag>{r.row_filter_type === 'template' ? '模板' : 'SQL'}</Tag>
                {r.row_filter ? (
                  <Tooltip title={r.row_filter}>
                    <span style={{ fontSize: 12, color: 'rgba(0,0,0,.45)', maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'inline-block', verticalAlign: 'middle' }}>{r.row_filter}</span>
                  </Tooltip>
                ) : null}
              </Space>
            ) : <Tag>仅字段级</Tag>,
          },
          { title: '启用', dataIndex: 'enabled', render: (v) => (v ? '是' : '否') },
          {
            title: '操作',
            render: (_, r) => (
              <Space size={4}>
                <Button size="small" onClick={() => openEdit(r)}>编辑</Button>
                <Popconfirm title="删除规则？" onConfirm={async () => { await client.delete(`/permission-rules/${r.id}`); loadRules() }}>
                  <Button size="small" danger>删除</Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Drawer title={editing ? '编辑权限规则' : '新建权限规则'} open={open} onClose={() => setOpen(false)} width={460}>
        <Form form={form} layout="vertical" initialValues={{ scope_type: 'role', rule_type: 'deny', enabled: true, row_enabled: false, row_filter_type: 'sql' }}>
          <Form.Item name="scope_type" label="作用域" rules={[{ required: true }]}>
            <Radio.Group
              options={[{ label: '角色全局', value: 'role' }, { label: '用户级', value: 'user' }, { label: '用户组', value: 'group' }]}
              optionType="button"
              onChange={() => form.setFieldValue('scope_ids', undefined)}
            />
          </Form.Item>
          <Form.Item
            name="scope_ids"
            label={editing ? '角色/用户/用户组' : '角色/用户/用户组（可多选，批量创建）'}
            rules={[{ required: true, message: '请选择作用域对象' }]}
          >
            <GlassSelect
              mode={editing ? undefined : 'multiple'}
              style={{ width: '100%' }}
              placeholder={scopePlaceholder}
              options={scopeOptions}
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

          {/* 行级权限（C3） */}
          <Form.Item name="row_enabled" label="启用行级权限" valuePropName="checked" extra="开启后按行过滤条件注入 WHERE（deny 优先；行条件涉及被列级 deny 的字段时整表拒绝）">
            <Switch />
          </Form.Item>
          {rowEnabled ? (
            <Collapse
              size="small"
              defaultActiveKey={['row']}
              style={{ marginBottom: 16 }}
              items={[{
                key: 'row',
                label: '行过滤条件配置',
                children: (
                  <Space direction="vertical" style={{ width: '100%' }}>
                    <Form.Item name="row_filter_type" label="条件类型" style={{ marginBottom: 8 }}>
                      <Radio.Group
                        options={[
                          { label: 'SQL（原样注入）', value: 'sql' },
                          { label: '模板（占位符替换）', value: 'template' },
                        ]}
                        optionType="button"
                      />
                    </Form.Item>
                    <Form.Item name="row_filter" label="行过滤条件" style={{ marginBottom: 8 }} rules={[{ required: true, message: '请输入行过滤条件' }]}>
                      <Input.TextArea
                        rows={3}
                        placeholder="示例：store_id IN (SELECT store_id FROM store_scope WHERE manager_user_id = {user_id})"
                        style={{ fontFamily: 'monospace', fontSize: 12 }}
                      />
                    </Form.Item>
                    <div style={{ fontSize: 12, color: 'rgba(0,0,0,.55)', marginBottom: 8 }}>
                      <span>占位符：</span>
                      <code>{'{user_id}'}</code> 当前用户 ID、<code>{'{group_ids}'}</code> 所属组 ID 列表（无组为 0）、<code>{'{role_code}'}</code> 角色编码（自动转义加引号）
                    </div>
                    <Form.Item name="row_filter_note" label="条件说明（审计展示）" style={{ marginBottom: 8 }}>
                      <Input placeholder="如：店长只能查看本店数据" />
                    </Form.Item>
                  </Space>
                ),
              }]}
            />
          ) : null}
          <Button type="primary" onClick={submit}>保存</Button>
        </Form>
      </Drawer>
      </Card>
    </div>
  )
}
