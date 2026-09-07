// 角色管理：角色 CRUD + 分配用户 / 分配用户组 / 分配菜单（权限口径：用户角色∪用户组角色）
import { useEffect, useMemo, useState } from 'react'
import {
  Card, Drawer, Form, Input, Popconfirm, Space, Table, Tag, Tree, message,
} from 'antd'
import { DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons'
import { client, errMsg } from '../api/client'
import type { MenuItem } from '../types'
import PageHeader from '../components/PageHeader'
import { DefaultButton, GlassSelect, PrimaryButton, TabSwitch } from '../ui'

interface RoleRow {
  id: number
  code: string
  name: string
  user_ids: number[]
  group_ids: number[]
  menu_ids: number[]
}

interface UserRow { id: number; username: string; display_name: string }
interface GroupRow { id: number; name: string }

export default function RolePage() {
  const [msgApi, ctx] = message.useMessage()
  const [roles, setRoles] = useState<RoleRow[]>([])
  const [users, setUsers] = useState<UserRow[]>([])
  const [groups, setGroups] = useState<GroupRow[]>([])
  const [menus, setMenus] = useState<MenuItem[]>([])
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<RoleRow | null>(null)
  const [assigning, setAssigning] = useState<RoleRow | null>(null)
  const [assignTab, setAssignTab] = useState<'users' | 'groups' | 'menus'>('users')
  const [assignIds, setAssignIds] = useState<number[]>([])
  const [savingAssign, setSavingAssign] = useState(false)
  const [form] = Form.useForm()

  const load = async () => {
    try {
      const r = await client.get<RoleRow[]>('/roles')
      setRoles(r.data)
    } catch (e) { msgApi.error(errMsg(e)) }
  }
  useEffect(() => {
    load()
    client.get<UserRow[]>('/users').then((r) => setUsers(r.data)).catch(() => {})
    client.get<GroupRow[]>('/user-groups').then((r) => setGroups(r.data)).catch(() => {})
    client.get<MenuItem[]>('/menus').then((r) => setMenus(r.data)).catch(() => {})
  }, [])

  // 扁平菜单 → 两级树（parent_id=0 为根；分组节点下挂子菜单）
  const menuTreeData = useMemo(() => {
    interface MenuNode { id: number; name: string; parent_id: number; children: MenuNode[] }
    const map = new Map<number, MenuNode>()
    menus.forEach((m) => map.set(m.id, { id: m.id, name: m.name, parent_id: m.parent_id, children: [] }))
    const roots: MenuNode[] = []
    menus.forEach((m) => {
      const node = map.get(m.id)!
      if (m.parent_id === 0 || !map.has(m.parent_id)) roots.push(node)
      else map.get(m.parent_id)!.children.push(node)
    })
    return roots.map((n) => ({
      key: n.id,
      title: n.name,
      children: n.children.length ? n.children.map((c) => ({ key: c.id, title: c.name })) : undefined,
    }))
  }, [menus])

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    setOpen(true)
  }
  const openEdit = (r: RoleRow) => {
    setEditing(r)
    form.setFieldsValue(r)
    setOpen(true)
  }
  const save = async () => {
    try {
      const v = await form.validateFields()
      if (editing) await client.put(`/roles/${editing.id}`, v)
      else await client.post('/roles', v)
      msgApi.success('已保存')
      setOpen(false)
      load()
    } catch (e) { if (typeof e === 'object' && e && 'errorFields' in e) return; msgApi.error(errMsg(e)) }
  }
  const remove = async (r: RoleRow) => {
    try {
      await client.delete(`/roles/${r.id}`)
      msgApi.success('已删除')
      load()
    } catch (e) { msgApi.error(errMsg(e)) }
  }

  const openAssign = (r: RoleRow, tab: 'users' | 'groups' | 'menus') => {
    setAssigning(r)
    setAssignTab(tab)
    setAssignIds(tab === 'users' ? r.user_ids : tab === 'groups' ? r.group_ids : r.menu_ids)
  }
  const saveAssign = async () => {
    if (!assigning) return
    setSavingAssign(true)
    try {
      await client.put(`/roles/${assigning.id}/${assignTab}`, { ids: assignIds })
      msgApi.success('分配已保存（角色权限取并集）')
      setAssigning(null)
      load()
    } catch (e) { msgApi.error(errMsg(e)) }
    finally { setSavingAssign(false) }
  }

  return (
    <div className="glass-page">
      {ctx}
      <PageHeader title="角色管理" description="角色可分配给用户与用户组；用户被分配的用户组或角色取并集生效；角色可被分配菜单" />
      <Card className="glass-card" variant="borderless">
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
          <PrimaryButton icon={<PlusOutlined />} onClick={openCreate}>新建角色</PrimaryButton>
        </div>
        <Table<RoleRow>
          rowKey="id"
          dataSource={roles}
          pagination={false}
          columns={[
            { title: 'ID', dataIndex: 'id', width: 60 },
            { title: '角色编码', dataIndex: 'code' },
            { title: '角色名称', dataIndex: 'name' },
            { title: '用户数', dataIndex: 'user_ids', width: 80, render: (v: number[]) => <Tag color="blue">{v.length}</Tag> },
            { title: '用户组数', dataIndex: 'group_ids', width: 90, render: (v: number[]) => <Tag color="cyan">{v.length}</Tag> },
            { title: '菜单数', dataIndex: 'menu_ids', width: 80, render: (v: number[]) => <Tag color="geekblue">{v.length}</Tag> },
            {
              title: '操作', width: 330,
              render: (_, r) => (
                <Space>
                  <DefaultButton size="small" onClick={() => openAssign(r, 'users')}>分配用户</DefaultButton>
                  <DefaultButton size="small" onClick={() => openAssign(r, 'groups')}>分配用户组</DefaultButton>
                  <DefaultButton size="small" onClick={() => openAssign(r, 'menus')}>分配菜单</DefaultButton>
                  <DefaultButton size="small" icon={<EditOutlined />} onClick={() => openEdit(r)} />
                  <Popconfirm title="删除该角色？" onConfirm={() => remove(r)}>
                    <DefaultButton size="small" danger icon={<DeleteOutlined />} />
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Drawer title={editing ? '编辑角色' : '新建角色'} width={420} open={open} onClose={() => setOpen(false)}>
        <Form form={form} layout="vertical">
          <Form.Item name="code" label="角色编码" rules={[{ required: true, message: '请输入角色编码' }]}>
            <Input placeholder="如 data_reader" />
          </Form.Item>
          <Form.Item name="name" label="角色名称" rules={[{ required: true, message: '请输入角色名称' }]}>
            <Input placeholder="如 数据只读员" />
          </Form.Item>
        </Form>
        <div style={{ textAlign: 'right' }}>
          <PrimaryButton onClick={save}>保存</PrimaryButton>
        </div>
      </Drawer>

      <Drawer
        title={assigning ? `分配${assignTab === 'users' ? '用户' : assignTab === 'groups' ? '用户组' : '菜单'}：${assigning.name}` : ''}
        width={420}
        open={!!assigning}
        onClose={() => setAssigning(null)}
      >
        <TabSwitch
          value={assignTab}
          onChange={(t) => {
            setAssignTab(t)
            setAssignIds(assigning?.[t === 'users' ? 'user_ids' : t === 'groups' ? 'group_ids' : 'menu_ids'] ?? [])
          }}
          items={[
            { key: 'users', label: '用户' },
            { key: 'groups', label: '用户组' },
            { key: 'menus', label: '菜单' },
          ]}
        />
        {assignTab === 'menus' ? (
          <Tree
            checkable
            defaultExpandAll
            treeData={menuTreeData}
            checkedKeys={assignIds}
            onCheck={(checked) => {
              const keys = Array.isArray(checked) ? checked : checked.checked
              setAssignIds(keys as number[])
            }}
            style={{ maxHeight: 420, overflow: 'auto', padding: '8px 4px' }}
          />
        ) : (
          <GlassSelect
            mode="multiple"
            style={{ width: '100%' }}
            placeholder="选择并保存"
            value={assignIds}
            onChange={setAssignIds}
            options={assignTab === 'users'
              ? users.map((u) => ({ label: `${u.display_name || u.username}（${u.username}）`, value: u.id }))
              : groups.map((g) => ({ label: g.name, value: g.id }))}
          />
        )}
        <div style={{ textAlign: 'right', marginTop: 16 }}>
          <PrimaryButton loading={savingAssign} onClick={saveAssign}>保存分配</PrimaryButton>
        </div>
      </Drawer>
    </div>
  )
}
