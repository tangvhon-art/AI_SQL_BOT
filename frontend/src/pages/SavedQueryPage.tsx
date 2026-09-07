// 保存查询 + 定时任务：参数化编辑、cron 快捷模板、立即执行、运行历史（图表快照）
import { useState } from 'react'
import {
 Button, Card, Drawer, Form, Input, Popconfirm, Space, Table, Tabs, Tag, Timeline,
} from 'antd'
import { PlayCircleOutlined, PlusOutlined } from '@ant-design/icons'
import { client } from '../api/client'
import type { SavedQuery, ScheduledTask } from '../types'
import ChartCard from '../components/ChartCard'
import PageHeader from '../components/PageHeader'
import { GlassSelect } from '../ui'
import { useCrudList } from '../hooks/useCrudList'

const CRON_OPTIONS = [
  { label: '每天 09:00', value: 'daily 09:00' },
  { label: '每天 08:30', value: 'daily 08:30' },
  { label: '每小时', value: 'hourly' },
  { label: '每周一 09:00', value: 'weekly 0 09:00' },
  { label: '每 6 小时', value: 'interval 21600' },
]

export default function SavedQueryPage() {
  const sqHook = useCrudList<SavedQuery>('/saved-queries')
  const taskHook = useCrudList<ScheduledTask>('/scheduled-tasks')
  const { data: sqs, load: loadSqs, msgApi, toastError } = sqHook
  const { data: tasks, load: loadTasks } = taskHook
  const reload = () => { loadSqs(); loadTasks() }
  const [sqOpen, setSqOpen] = useState(false)
  const [editingSq, setEditingSq] = useState<SavedQuery | null>(null)
  const [sqForm] = Form.useForm()
  const [taskOpen, setTaskOpen] = useState(false)
  const [editingTask, setEditingTask] = useState<ScheduledTask | null>(null)
  const [taskForm] = Form.useForm()
  const [runs, setRuns] = useState<Array<{ id: number; run_time?: string; status: string; param_values: Record<string, unknown>; row_count: number; latency_ms?: number; error_msg: string; chart_snapshot: Record<string, unknown> }>>([])
  const [runsOpen, setRunsOpen] = useState(false)

  const openCreateSq = () => { setEditingSq(null); sqForm.resetFields(); setSqOpen(true) }
  const openEditSq = (sq: SavedQuery) => {
    setEditingSq(sq)
    sqForm.setFieldsValue({ ...sq, params: (sq.params ?? []).map((p) => ({ ...p })) })
    setSqOpen(true)
  }

  const submitSq = async () => {
    const v = await sqForm.validateFields()
    const body = {
      name: v.name,
      sql_text: v.sql_text,
      params: v.params ?? [],
      chart_config: {},
      tags: v.tags ?? '',
      remark: v.remark ?? '',
    }
    try {
      if (editingSq) await client.put(`/saved-queries/${editingSq.id}`, body)
      else await client.post('/saved-queries', body)
      msgApi.success('已保存')
      setSqOpen(false)
      reload()
    } catch (e) { toastError(e) }
  }

  const openCreateTask = () => {
    setEditingTask(null)
    taskForm.resetFields()
    setTaskOpen(true)
  }
  const openEditTask = (t: ScheduledTask) => {
    setEditingTask(t)
    taskForm.setFieldsValue({ ...t, param_values: t.param_values ?? {} })
    setTaskOpen(true)
  }

  const submitTask = async () => {
    const v = await taskForm.validateFields()
    try {
      if (editingTask) await client.put(`/scheduled-tasks/${editingTask.id}`, v)
      else await client.post('/scheduled-tasks', v)
      msgApi.success('已保存')
      setTaskOpen(false)
      reload()
    } catch (e) { toastError(e) }
  }

  const runNow = async (id: number) => {
    try {
      const r = await client.post(`/scheduled-tasks/${id}/run`)
      if (r.data.ok) msgApi.success(`执行成功，返回 ${r.data.row_count} 行`)
      else msgApi.error(`执行失败：${r.data.error}`)
    } catch (e) { toastError(e) }
  }

  const showRuns = async (id: number) => {
    try {
      const r = await client.get(`/scheduled-tasks/${id}/runs`)
      setRuns(r.data)
      setRunsOpen(true)
    } catch (e) { toastError(e) }
  }

  return (
    <div className="glass-page">
      <PageHeader
        title="定时任务"
        description="问数结果保存为参数化查询；cron 定时按参数化内容生成数据与图表快照，沿用创建者权限"
      />
      <Card styles={{ header: { display: 'none' } }}>
      {sqHook.ctx}{taskHook.ctx}
      <Tabs
        items={[
          {
            key: 'sq',
            label: `保存查询（${sqs.length}）`,
            children: (
              <div>
                <Button type="primary" icon={<PlusOutlined />} onClick={openCreateSq} style={{ marginBottom: 12 }}>
                  新建保存查询
                </Button>
                <Table
                  rowKey="id"
                  dataSource={sqs}
                  pagination={false}
                  columns={[
                    { title: '名称', dataIndex: 'name' },
                    { title: 'SQL', dataIndex: 'sql_text', render: (v: string) => <code style={{ fontSize: 12 }}>{v.slice(0, 80)}{v.length > 80 ? '…' : ''}</code> },
                    { title: '参数', dataIndex: 'params', render: (v: Array<{ key: string; type: string }>) => v?.length ? v.map((p) => <Tag key={p.key} color="blue">{p.key}</Tag>) : '—' },
                    { title: '备注', dataIndex: 'remark' },
                    {
                      title: '操作',
                      render: (_, r) => (
                        <Space size={4}>
                          <Button size="small" onClick={() => openEditSq(r)}>编辑</Button>
                          <Popconfirm title="删除？" onConfirm={async () => { await client.delete(`/saved-queries/${r.id}`); reload() }}>
                            <Button size="small" danger>删除</Button>
                          </Popconfirm>
                        </Space>
                      ),
                    },
                  ]}
                />
                <Drawer title={editingSq ? '编辑保存查询' : '新建保存查询'} open={sqOpen} onClose={() => setSqOpen(false)} width={520}>
                  <Form form={sqForm} layout="vertical">
                    <Form.Item name="name" label="名称" rules={[{ required: true }]}>
                      <Input />
                    </Form.Item>
                    <Form.Item name="sql_text" label="SQL（{param} 为参数占位符）" rules={[{ required: true }]}>
                      <Input.TextArea rows={5} placeholder="SELECT ... WHERE date = '{date}'" />
                    </Form.Item>
                    <Form.Item name="params" label="参数（JSON，key/type/默认值）">
                      <Input.TextArea rows={3} placeholder='[{"key":"date","type":"date","default":"T-1"}]'
                        onChange={(e) => {
                          try { sqForm.setFieldValue('params', JSON.parse(e.target.value)) } catch { /* 非法 JSON 忽略 */ }
                        }} />
                    </Form.Item>
                    <Form.Item name="tags" label="标签"><Input /></Form.Item>
                    <Form.Item name="remark" label="备注"><Input /></Form.Item>
                    <Button type="primary" onClick={submitSq}>保存</Button>
                  </Form>
                </Drawer>
              </div>
            ),
          },
          {
            key: 'task',
            label: `定时任务（${tasks.length}）`,
            children: (
              <div>
                <Button type="primary" icon={<PlusOutlined />} onClick={openCreateTask} style={{ marginBottom: 12 }}>
                  新建定时任务
                </Button>
                <Table
                  rowKey="id"
                  dataSource={tasks}
                  pagination={false}
                  columns={[
                    { title: '任务名', dataIndex: 'name' },
                    { title: '关联查询', dataIndex: 'saved_query_name' },
                    { title: 'cron', dataIndex: 'cron_expr', render: (v) => <Tag>{v}</Tag> },
                    { title: '参数', dataIndex: 'param_values', render: (v: Record<string, unknown>) =>
                      v && Object.keys(v).length ? Object.entries(v).map(([k, val]) => <Tag key={k} color="blue">{k}={String(val)}</Tag>) : '—' },
                    { title: '状态', dataIndex: 'status', render: (v) => (
                      <Tag color={v === 'enabled' ? 'green' : 'default'}>{v === 'enabled' ? '启用' : '停用'}</Tag>) },
                    { title: '下次执行', dataIndex: 'next_run_at', render: (v) => v ? new Date(v).toLocaleString() : '—' },
                    {
                      title: '操作',
                      render: (_, r) => (
                        <Space size={4}>
                          <Button size="small" icon={<PlayCircleOutlined />} onClick={() => runNow(r.id)}>立即执行</Button>
                          <Button size="small" onClick={() => showRuns(r.id)}>历史</Button>
                          <Button size="small" onClick={() => openEditTask(r)}>编辑</Button>
                          <Popconfirm title="删除任务？" onConfirm={async () => { await client.delete(`/scheduled-tasks/${r.id}`); reload() }}>
                            <Button size="small" danger>删除</Button>
                          </Popconfirm>
                        </Space>
                      ),
                    },
                  ]}
                />
                <Drawer title={editingTask ? '编辑定时任务' : '新建定时任务'} open={taskOpen} onClose={() => setTaskOpen(false)} width={460}>
                  <Form form={taskForm} layout="vertical" initialValues={{ status: 'enabled', timezone: 'Asia/Shanghai', cron_expr: 'daily 09:00' }}>
                    <Form.Item name="name" label="任务名" rules={[{ required: true }]}><Input /></Form.Item>
                    <Form.Item name="saved_query_id" label="关联保存查询" rules={[{ required: true }]}>
                      <GlassSelect options={sqs.map((s) => ({ label: s.name, value: s.id }))} />
                    </Form.Item>
                    <Form.Item name="cron_expr" label="cron 表达式" rules={[{ required: true }]}>
                      <GlassSelect options={CRON_OPTIONS} showSearch allowClear placeholder="或自定义：daily 08:00 / hourly / interval 3600" />
                    </Form.Item>
                    <Form.Item name="param_values" label="参数值（JSON，T-1 自动解析为昨日）">
                      <Input.TextArea rows={3} placeholder='{"date": "T-1"}' />
                    </Form.Item>
                    <Form.Item name="status" label="状态">
                      <GlassSelect options={[{ label: '启用', value: 'enabled' }, { label: '停用', value: 'disabled' }]} />
                    </Form.Item>
                    <Button type="primary" onClick={submitTask}>保存</Button>
                  </Form>
                </Drawer>
              </div>
            ),
          },
        ]}
      />
      <Drawer title="运行历史（含图表快照）" open={runsOpen} onClose={() => setRunsOpen(false)} width={640}>
        <Timeline
          items={runs.map((r) => ({
            color: r.status === 'success' ? 'green' : 'red',
            children: (
              <div>
                <Space>
                  <Tag>{r.run_time ? new Date(r.run_time).toLocaleString() : ''}</Tag>
                  <Tag color={r.status === 'success' ? 'green' : 'red'}>{r.status}</Tag>
                  {r.row_count ? <Tag>行数 {r.row_count}</Tag> : null}
                  {r.latency_ms ? <Tag>{r.latency_ms}ms</Tag> : null}
                </Space>
                {r.error_msg ? <div style={{ color: '#dc2626', fontSize: 12 }}>{r.error_msg}</div> : null}
                {r.chart_snapshot?.chart ? <ChartCard chart={r.chart_snapshot.chart as never} /> : null}
              </div>
            ),
          }))}
        />
      </Drawer>
      </Card>
    </div>
  )
}
