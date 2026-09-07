// 审计日志：问数全链路留痕（含权限注入类型）
import { useEffect, useState } from 'react'
import { Button, Card, Input, Space, Table, Tag } from 'antd'
import { SearchOutlined } from '@ant-design/icons'
import { client } from '../api/client'
import type { QueryLog } from '../types'
import PageHeader from '../components/PageHeader'
import { GlassSelect } from '../ui'

export default function AuditPage() {
  const [list, setList] = useState<QueryLog[]>([])
  const [question, setQuestion] = useState('')
  const [injected, setInjected] = useState<string>('')

  const load = async () => {
    try {
      const r = await client.get<QueryLog[]>('/query-logs', {
        params: { question: question || undefined, injected: injected || undefined, limit: 100 },
      })
      setList(r.data)
    } catch { /* 忽略 */ }
  }
  useEffect(() => { load() }, [])

  return (
    <div className="glass-page">
      <PageHeader
        title="审计日志"
        description="问数全链路留痕：问题、命中表、生成 SQL、权限注入类型（1=1 / 1=2）、行数、耗时与反馈"
      />
      <Card styles={{ header: { display: 'none' } }}>
      <Space style={{ marginBottom: 12 }}>
        <Input placeholder="按问题搜索" value={question} onChange={(e) => setQuestion(e.target.value)} style={{ width: 220 }} />
        <GlassSelect
          placeholder="权限注入"
          value={injected || undefined}
          onChange={setInjected}
          allowClear
          style={{ width: 140 }}
          options={[{ label: '1=1（全可查）', value: '1=1' }, { label: '1=2（含受限字段）', value: '1=2' }]}
        />
        <Button type="primary" icon={<SearchOutlined />} onClick={load}>查询</Button>
      </Space>
      <Table
        rowKey="id"
        dataSource={list}
        pagination={{ pageSize: 20 }}
        size="small"
        columns={[
          { title: '时间', dataIndex: 'create_time', render: (v) => (v ? new Date(v).toLocaleString() : '—'), width: 160 },
          { title: '问题', dataIndex: 'question' },
          { title: '意图', dataIndex: 'intent', render: (v) => <Tag>{v}</Tag> },
          { title: '权限注入', dataIndex: 'permission_injected', render: (v: string) => v ? (
            <Tag color={v === '1=2' ? 'red' : 'green'}>{v}</Tag>) : '—' },
          { title: '行数', dataIndex: 'row_count', width: 70 },
          { title: '耗时(ms)', dataIndex: 'latency_ms', width: 90 },
          { title: '图表', dataIndex: 'chart_type', width: 90 },
          { title: '反馈', dataIndex: 'feedback', render: (v) => v ? (v === 'good' ? <Tag color="green">赞</Tag> : <Tag color="orange">踩</Tag>) : '—', width: 70 },
        ]}
        expandable={{
          expandedRowRender: (r) => (
            <div>
              <div><b>生成 SQL：</b></div>
              <pre style={{ background: '#0f172a', color: '#e2e8f0', padding: 10, borderRadius: 8, fontSize: 12, overflowX: 'auto' }}>{r.generated_sql || '—'}</pre>
              <div style={{ marginTop: 4 }}><b>命中表：</b>{r.matched_tables || '—'}</div>
            </div>
          ),
        }}
      />
      </Card>
    </div>
  )
}
