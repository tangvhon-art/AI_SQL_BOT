// 用户管理：用户 CRUD + 分配角色 / 分配用户组（用户被分配的用户组或角色，两者取并集）
import { useEffect, useState } from 'react'
import {
  Card, Drawer, Form, Input, Popconfirm, Space, Switch, Table, Tag, message,
} from 'antd'
import { DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons'
import { client, errMsg } from '../api/client'
import PageHeader from '../components/PageHeader'
import { DefaultButton, GlassSelect, PrimaryButton, TabSwitch } from '../ui'

interface UserRow {
  id: number
  username: string
  display_name: string
  status: number
  role_id: number
  role_code: string
  role_name: string
  role_ids: number[]
  group_ids: number[]
  group_names: string[]
}
interface RoleRow { id: number; name: string }
interface GroupRow { id: number; name: string }

export default function UserPage() {
  const [msgApi, ctx] = message.useMessage()
  const [users, setUsers] = useState<UserRow[]>([])
  const [roles, setRoles] = useState<RoleRow[]>([])
  const [groups, setGroups] = useState<GroupRow[]>([])
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<UserRow | null>(null)
  const [assigning, setAssigning] = useState<UserRow | null>(null)
  const [assignTab, setAssignTab] = useState<'roles' | 'groups'>('roles')
  const [assignIds, setAssignIds] = useState<number[]>([])
  const [form] = Form.useForm()

  const load = async () => {
    try {
      const r = await client.get<UserRow[]>('/users')
      setUsers(r.data)
    } catch (e) { msgApi.error(errMsg(e)) }
  }
  useEffect(() => {
    load()
    client.get<RoleRow[]>('/roles').then((r) => setRoles(r.data)).catch(() => {})
    client.get<GroupRow[]>('/user-groups').then((r) => setGroups(r.data)).catch(() => {})
  }, [])

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ status: 1, role_id: roles[0]?.id })
    setOpen(true)
  }
  const openEdit = (u: UserRow) => {
    setEditing(u)
    form.setFieldsValue({ ...u, password: '' })
    setOpen(true)
  }
  const save = async () => {
    try {
      const v = await form.validateFields()
      if (editing) await client.put(`/users/${editing.id}`, v)
      else await client.post('/users', v)
      msgApi.success('已保存')
      setOpen(false)
      load()
    } catch (e) { if (typeof e === 'object' && e && 'errorFields' in e) return; msgApi.error(errMsg(e)) }
  }
  const remove = async (u: UserRow) => {
    try {
      await client.delete(`/users/${u.id}`)
      msgApi.success('已删除')
      load()
    } catch (e) { msgApi.error(errMsg(e)) }
  }
  const openAssign = (u: UserRow, tab: 'roles' | 'groups') => {
    setAssigning(u)
    setAssignTab(tab)
    setAssignIds(tab === 'roles' ? u.role_ids : u.group_ids)
  }
  const saveAssign = async () => {
    if (!assigning) return
    try {
      await client.put(`/users/${assigning.id}/${assignTab}`, { ids: assignIds })
      msgApi.success('分配已保存（角色与用户组取并集）')
      setAssigning(null)
      load()
    } catch (e) { msgApi.error(errMsg(e)) }
  }

  return (
    <div className="glass-page">
      {ctx}
      <PageHeader title="用户管理" description="用户可被直接分配角色，也可加入用户组继承组角色；两者权限取并集" />
      <Card className="glass-card" variant="borderless">
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
          <PrimaryButton icon={<PlusOutlined />} onClick={openCreate}>新建用户</PrimaryButton>
        </div>
        <Table<UserRow>
          rowKey="id"
          dataSource={users}
          pagination={false}
          columns={[
            { title: 'ID', dataIndex: 'id', width: 60 },
            { title: '用户名', dataIndex: 'username' },
            { title: '显示名', dataIndex: 'display_name' },
            {
              title: '状态', dataIndex: 'status', width: 90,
              render: (v: number) => <Tag color={v === 1 ? 'green' : 'red'}>{v === 1 ? '启用' : '停用'}</Tag>,
            },
            {
              title: '角色', dataIndex: 'role_ids', width: 200,
              render: (v: number[]) => v.length ? v.map((rid) => roles.find((r) => r.id === rid)?.name || rid).join('、') : <Tag>无</Tag>,
            },
            {
              title: '用户组', dataIndex: 'group_names', width: 200,
              render: (v: string[]) => v?.length ? v.join('、') : <Tag>无</Tag>,
            },
            {
              title: '操作', width: 260,
              render: (_, u) => (
                <Space>
                  <DefaultButton size="small" onClick={() => openAssign(u, 'roles')}>分配角色</DefaultButton>
                  <DefaultButton size="small" onClick={() => openAssign(u, 'groups')}>分配用户组</DefaultButton>
                  <DefaultButton size="small" icon={<EditOutlined />} onClick={() => openEdit(u)} />
                  <Popconfirm title="删除该用户？" onConfirm={() => remove(u)}>
                    <DefaultButton size="small" danger icon={<DeleteOutlined />} />
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Drawer title={editing ? `编辑用户：${editing.username}` : '新建用户'} width={440} open={open} onClose={() => setOpen(false)}>
        <Form form={form} layout="vertical">
          <Form.Item name="username" label="用户名" rules={[{ required: true, message: '请输入用户名' }]}>
            <Input placeholder="登录账号" disabled={!!editing} />
          </Form.Item>
          <Form.Item name="password" label={editing ? '重置密码（留空不修改）' : '密码'} rules={editing ? [] : [{ required: true, message: '请输入初始密码' }]}>
            <Input.Password placeholder={editing ? '留空则不修改' : '初始密码'} />
          </Form.Item>
          <Form.Item name="display_name" label="显示名">
            <Input placeholder="展示名称" />
          </Form.Item>
          <Form.Item name="role_id" label="主角色">
            <GlassSelect
              placeholder="选择主角色"
              options={roles.map((r) => ({ label: r.name, value: r.id }))}
            />
          </Form.Item>
          <Form.Item name="status" label="状态" valuePropName="checked">
            <Switch checkedChildren="启用" unCheckedChildren="停用" />
          </Form.Item>
        </Form>
        <div style={{ textAlign: 'right' }}>
          <PrimaryButton onClick={save}>保存</PrimaryButton>
        </div>
      </Drawer>

      <Drawer
        title={assigning ? `分配${assignTab === 'roles' ? '角色' : '用户组'}：${assigning.username}` : ''}
        width={420}
        open={!!assigning}
        onClose={() => setAssigning(null)}
      >
        <TabSwitch
          value={assignTab}
          onChange={(t) => {
            setAssignTab(t)
            setAssignIds(assigning?.[t === 'roles' ? 'role_ids' : 'group_ids'] ?? [])
          }}
          items={[
            { key: 'roles', label: '角色' },
            { key: 'groups', label: '用户组' },
          ]}
        />
        <GlassSelect
          mode="multiple"
          style={{ width: '100%' }}
          placeholder="选择并保存（多选）"
          value={assignIds}
          onChange={setAssignIds}
          options={assignTab === 'roles'
            ? roles.map((r) => ({ label: r.name, value: r.id }))
            : groups.map((g) => ({ label: g.name, value: g.id }))}
        />
        <div style={{ textAlign: 'right', marginTop: 16 }}>
          <PrimaryButton onClick={saveAssign}>保存分配</PrimaryButton>
        </div>
      </Drawer>
    </div>
  )
}
