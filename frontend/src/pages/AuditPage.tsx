// 审计日志：查询条件（问题/意图/权限注入）+ 服务端分页（page/size）；问数全链路留痕（含权限注入类型）
import { useState } from 'react'
import { Card, Input, Table, Tag } from 'antd'
import type { QueryLog } from '../types'
import PageHeader from '../components/PageHeader'
import QueryBar from '../components/QueryBar'
import { GlassSelect } from '../ui'
import { tablePagination, usePagedList } from '../hooks/useCrudList'

export default function AuditPage() {
  const list = usePagedList<QueryLog>('/query-logs', { silent: true })
  const { items, total, page, size, loading, search, onPageChange, ctx } = list
  const [question, setQuestion] = useState('')
  const [intent, setIntent] = useState('')
  const [injected, setInjected] = useState('')

  const doQuery = () => search({
    question: question.trim() || undefined,
    intent: intent || undefined,
    injected: injected || undefined,
  })
  const doReset = () => {
    setQuestion(''); setIntent(''); setInjected('')
    search({})
  }

  return (
    <div className="glass-page">
      <PageHeader
        title="审计日志"
        description="问数全链路留痕：问题、命中表、生成 SQL、权限注入类型（1=1 / 1=2）、行数、耗时与反馈"
      />
      <Card styles={{ header: { display: 'none' } }}>
      {ctx}
      <QueryBar onSearch={doQuery} onReset={doReset} loading={loading}>
        <Input placeholder="按问题搜索" allowClear value={question} onChange={(e) => setQuestion(e.target.value)} style={{ width: 220 }} onPressEnter={doQuery} />
        <GlassSelect
          placeholder="意图"
          allowClear
          value={intent || undefined}
          onChange={setIntent}
          style={{ width: 130 }}
          options={[
            { label: 'value', value: 'value' },
            { label: 'compare', value: 'compare' },
            { label: 'ranking', value: 'ranking' },
            { label: 'trend', value: 'trend' },
            { label: 'detail', value: 'detail' },
            { label: 'statistic', value: 'statistic' },
          ]}
        />
        <GlassSelect
          placeholder="权限注入"
          value={injected || undefined}
          onChange={setInjected}
          allowClear
          style={{ width: 140 }}
          options={[{ label: '1=1（全可查）', value: '1=1' }, { label: '1=2（含受限字段）', value: '1=2' }]}
        />
      </QueryBar>
      <Table
        rowKey="id"
        dataSource={items}
        pagination={tablePagination(page, size, total, onPageChange)}
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
