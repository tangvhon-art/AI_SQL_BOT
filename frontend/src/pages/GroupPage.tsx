// 用户组管理：用户组 CRUD + 成员管理（组内角色随组生效）
import { useEffect, useState } from 'react'
import {
  Card, Drawer, Form, Input, Popconfirm, Space, Table, Tag, message,
} from 'antd'
import { DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons'
import { client, errMsg } from '../api/client'
import PageHeader from '../components/PageHeader'
import { DefaultButton, GlassSelect, PrimaryButton } from '../ui'

interface GroupRow {
  id: number
  name: string
  remark: string
  member_ids: number[]
  role_ids: number[]
}
interface UserRow { id: number; username: string; display_name: string }
interface RoleRow { id: number; name: string }

export default function GroupPage() {
  const [msgApi, ctx] = message.useMessage()
  const [groups, setGroups] = useState<GroupRow[]>([])
  const [users, setUsers] = useState<UserRow[]>([])
  const [roles, setRoles] = useState<RoleRow[]>([])
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<GroupRow | null>(null)
  const [assigning, setAssigning] = useState<GroupRow | null>(null)
  const [memberIds, setMemberIds] = useState<number[]>([])
  const [form] = Form.useForm()

  const load = async () => {
    try {
      const r = await client.get<GroupRow[]>('/user-groups')
      setGroups(r.data)
    } catch (e) { msgApi.error(errMsg(e)) }
  }
  useEffect(() => {
    load()
    client.get<UserRow[]>('/users').then((r) => setUsers(r.data)).catch(() => {})
    client.get<RoleRow[]>('/roles').then((r) => setRoles(r.data)).catch(() => {})
  }, [])

  const openCreate = () => { setEditing(null); form.resetFields(); setOpen(true) }
  const openEdit = (g: GroupRow) => { setEditing(g); form.setFieldsValue(g); setOpen(true) }
  const save = async () => {
    try {
      const v = await form.validateFields()
      if (editing) await client.put(`/user-groups/${editing.id}`, v)
      else await client.post('/user-groups', v)
      msgApi.success('已保存')
      setOpen(false)
      load()
    } catch (e) { if (typeof e === 'object' && e && 'errorFields' in e) return; msgApi.error(errMsg(e)) }
  }
  const remove = async (g: GroupRow) => {
    try {
      await client.delete(`/user-groups/${g.id}`)
      msgApi.success('已删除')
      load()
    } catch (e) { msgApi.error(errMsg(e)) }
  }
  const saveMembers = async () => {
    if (!assigning) return
    try {
      await client.put(`/user-groups/${assigning.id}/members`, { ids: memberIds })
      msgApi.success('成员已更新')
      setAssigning(null)
      load()
    } catch (e) { msgApi.error(errMsg(e)) }
  }

  return (
    <div className="glass-page">
      {ctx}
      <PageHeader title="用户组管理" description="用户组用于批量授权：组内用户继承组角色，与用户直接角色取并集" />
      <Card className="glass-card" variant="borderless">
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
          <PrimaryButton icon={<PlusOutlined />} onClick={openCreate}>新建用户组</PrimaryButton>
        </div>
        <Table<GroupRow>
          rowKey="id"
          dataSource={groups}
          pagination={false}
          columns={[
            { title: 'ID', dataIndex: 'id', width: 60 },
            { title: '组名', dataIndex: 'name' },
            { title: '备注', dataIndex: 'remark', ellipsis: true },
            { title: '成员数', dataIndex: 'member_ids', width: 80, render: (v: number[]) => <Tag color="blue">{v.length}</Tag> },
            {
              title: '组角色', dataIndex: 'role_ids', width: 200,
              render: (v: number[]) => v.length
                ? v.map((rid) => roles.find((r) => r.id === rid)?.name || rid).join('、')
                : <Tag>未分配</Tag>,
            },
            {
              title: '操作', width: 200,
              render: (_, g) => (
                <Space>
                  <DefaultButton size="small" onClick={() => { setAssigning(g); setMemberIds(g.member_ids) }}>管理成员</DefaultButton>
                  <DefaultButton size="small" icon={<EditOutlined />} onClick={() => openEdit(g)} />
                  <Popconfirm title="删除该用户组？" onConfirm={() => remove(g)}>
                    <DefaultButton size="small" danger icon={<DeleteOutlined />} />
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Drawer title={editing ? '编辑用户组' : '新建用户组'} width={420} open={open} onClose={() => setOpen(false)}>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="组名" rules={[{ required: true, message: '请输入组名' }]}>
            <Input placeholder="如 数据分析组" />
          </Form.Item>
          <Form.Item name="remark" label="备注">
            <Input.TextArea rows={3} placeholder="组用途说明" />
          </Form.Item>
        </Form>
        <div style={{ textAlign: 'right' }}>
          <PrimaryButton onClick={save}>保存</PrimaryButton>
        </div>
      </Drawer>

      <Drawer title={assigning ? `管理成员：${assigning.name}` : ''} width={420} open={!!assigning} onClose={() => setAssigning(null)}>
        <GlassSelect
          mode="multiple"
          style={{ width: '100%' }}
          placeholder="选择成员（用户）"
          value={memberIds}
          onChange={setMemberIds}
          options={users.map((u) => ({ label: `${u.display_name || u.username}（${u.username}）`, value: u.id }))}
        />
        <div style={{ textAlign: 'right', marginTop: 16 }}>
          <PrimaryButton onClick={saveMembers}>保存成员</PrimaryButton>
        </div>
      </Drawer>
    </div>
  )
}
