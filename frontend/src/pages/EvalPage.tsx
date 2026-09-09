// C4 评测闭环：评测用例管理（CRUD）+ 评测批次（发起/列表/报告明细）
// 指标：意图准确率 / 表命中率 / SQL 生成率 / 可执行率 / 正确率 / E2E 准确率 / 平均耗时
import { useEffect, useState } from 'react'
import {
  Button, Card, Descriptions, Drawer, Form, Input, Modal, Popconfirm, Progress, Select, Space, Statistic, Table, Tag, Typography,
} from 'antd'
import { PlayCircleOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { client } from '../api/client'
import type { Datasource, EvalCase, EvalResultItem, EvalRun, PageResult } from '../types'
import PageHeader from '../components/PageHeader'
import QueryBar from '../components/QueryBar'
import { GlassSelect } from '../ui'
import { useMessageApi } from '../hooks/useMessageApi'
import { useOptions } from '../hooks/useOptions'

const METRIC_META: Array<{ key: string; label: string; pct?: boolean }> = [
  { key: 'intent_accuracy', label: '意图准确率', pct: true },
  { key: 'table_hit_rate', label: '表命中率', pct: true },
  { key: 'sql_generation_rate', label: 'SQL 生成率', pct: true },
  { key: 'sql_executable_rate', label: '可执行率', pct: true },
  { key: 'sql_correct_rate', label: '正确率', pct: true },
  { key: 'e2e_accuracy', label: 'E2E 准确率', pct: true },
  { key: 'avg_latency_ms', label: '平均耗时', pct: false },
]

const RUN_STATUS: Record<string, { label: string; color: string }> = {
  pending: { label: '排队中', color: 'default' },
  running: { label: '执行中', color: 'processing' },
  success: { label: '完成', color: 'success' },
  failed: { label: '失败', color: 'error' },
}

export default function EvalPage() {
  const { msgApi, ctx, toastError } = useMessageApi()
  const dsList = useOptions<Datasource>('datasources')

  // ---------- 用例 ----------
  const [cases, setCases] = useState<EvalCase[]>([])
  const [caseTotal, setCaseTotal] = useState(0)
  const [casePage, setCasePage] = useState(1)
  const [caseLoading, setCaseLoading] = useState(false)
  const [fDs, setFDs] = useState<number | undefined>()
  const [fScene, setFScene] = useState('')
  const [caseOpen, setCaseOpen] = useState(false)
  const [editingCaseId, setEditingCaseId] = useState<number | null>(null)
  const [caseForm] = Form.useForm()

  // ---------- 批次 ----------
  const [runs, setRuns] = useState<EvalRun[]>([])
  const [runTotal, setRunTotal] = useState(0)
  const [runPage, setRunPage] = useState(1)
  const [runLoading, setRunLoading] = useState(false)
  const [runOpen, setRunOpen] = useState(false)
  const [runForm] = Form.useForm()
  const [runCreating, setRunCreating] = useState(false)
  // 报告
  const [report, setReport] = useState<{
    id: number; name: string; status: string; total: number; metrics: Record<string, number | null>;
    started_at: string | null; finished_at: string | null; mock_execute: boolean; results: EvalResultItem[]
  } | null>(null)
  const [reportLoading, setReportLoading] = useState(false)

  const loadCases = async (p = casePage) => {
    setCaseLoading(true)
    try {
      const r = await client.get<PageResult<EvalCase>>('/eval/cases', {
        params: { page: p, size: 20, datasource_id: fDs, scene_code: fScene || undefined },
      })
      setCases(r.data.items); setCaseTotal(r.data.total); setCasePage(p)
    } catch (e) { toastError(e) } finally { setCaseLoading(false) }
  }
  const loadRuns = async (p = runPage) => {
    setRunLoading(true)
    try {
      const r = await client.get<PageResult<EvalRun>>('/eval/runs', { params: { page: p, size: 10 } })
      setRuns(r.data.items); setRunTotal(r.data.total); setRunPage(p)
    } catch (e) { toastError(e) } finally { setRunLoading(false) }
  }
  useEffect(() => { loadCases(1); loadRuns(1) }, [])

  const saveCase = async () => {
    const v = await caseForm.validateFields().catch(() => null)
    if (!v) return
    const payload = {
      datasource_id: v.datasource_id, question: v.question.trim(),
      expect_tables: (v.expect_tables || '').split(/[,，\n]/).map((s: string) => s.trim()).filter(Boolean),
      expect_metrics: (v.expect_metrics || '').split(/[,，\n]/).map((s: string) => s.trim()).filter(Boolean),
      expect_filters: (v.expect_filters || '').split(/[,，\n]/).map((s: string) => s.trim()).filter(Boolean),
      expect_sql: v.expect_sql ?? '', scene_code: v.scene_code ?? '', tags: v.tags ?? '',
    }
    try {
      if (editingCaseId) {
        await client.put(`/eval/cases/${editingCaseId}`, payload)
        msgApi.success('用例已更新')
      } else {
        await client.post('/eval/cases', payload)
        msgApi.success('用例已添加')
      }
      setCaseOpen(false); setEditingCaseId(null); caseForm.resetFields(); loadCases(1)
    } catch (e) { toastError(e) }
  }

  const editCase = (c: any) => {
    caseForm.setFieldsValue({
      datasource_id: c.datasource_id,
      question: c.question,
      expect_tables: (c.expect_tables || []).join(', '),
      expect_metrics: (c.expect_metrics || []).join(', '),
      expect_filters: (c.expect_filters || []).join(', '),
      expect_sql: c.expect_sql || '',
      scene_code: c.scene_code || '',
      tags: c.tags || '',
    })
    setEditingCaseId(c.id)
    setCaseOpen(true)
  }

  const removeCase = async (id: number) => {
    try {
      await client.delete(`/eval/cases/${id}`)
      msgApi.success('已删除')
      loadCases()
    } catch (e) { toastError(e) }
  }

  const createRun = async () => {
    const v = await runForm.validateFields().catch(() => null)
    if (!v) return
    setRunCreating(true)
    try {
      await client.post('/eval/runs', {
        name: v.name || '评测批次',
        case_ids: v.case_ids || [],
        tags: (v.tags || '').split(/[,，\n]/).map((s: string) => s.trim()).filter(Boolean),
        mock_execute: v.mock_execute,
      })
      msgApi.success('评测批次已发起（后台执行）')
      setRunOpen(false); runForm.resetFields()
      loadRuns(1)
      // 异步执行：3 秒后自动刷新一次查看结果
      setTimeout(() => { loadRuns(1); loadRuns(1) }, 3000)
    } catch (e) { toastError(e) } finally { setRunCreating(false) }
  }

  const viewReport = async (id: number) => {
    setReportLoading(true)
    try {
      const r = await client.get(`/eval/runs/${id}`)
      setReport(r.data)
    } catch (e) { toastError(e) } finally { setReportLoading(false) }
  }

  const pct = (v: number | null | undefined) => (v == null ? '-' : `${(v * 100).toFixed(1)}%`)

  const renderMetric = (metrics: Record<string, number | null>) => (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 12 }}>
      {METRIC_META.map((m) => (
        <div key={m.key} style={{ background: 'rgba(0,0,0,0.03)', borderRadius: 8, padding: '10px 12px' }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>{m.label}</Typography.Text>
          <div style={{ fontSize: 20, fontWeight: 700, marginTop: 2 }}>
            {m.pct ? pct(metrics[m.key]) : metrics[m.key] == null ? '-' : `${metrics[m.key]} ms`}
          </div>
        </div>
      ))}
    </div>
  )

  const okTag = (v: boolean | null) => {
    if (v == null) return <Tag>未判定</Tag>
    return v ? <Tag color="success">通过</Tag> : <Tag color="error">未通过</Tag>
  }

  return (
    <div className="glass-page">
      <PageHeader
        title="评测中心"
        description="评测用例（问题 + 期望表/指标/过滤条件）+ 批次执行（mock 模式不实连业务库）+ 指标报告；SQL 语义等价判定按表集 / 投影列重叠 / 聚合函数集"
        extra={(
          <Space>
            <Button icon={<PlusOutlined />} onClick={() => setCaseOpen(true)}>新增用例</Button>
            <Button type="primary" icon={<PlayCircleOutlined />} onClick={() => setRunOpen(true)}>发起评测批次</Button>
          </Space>
        )}
      />
      {ctx}

      {/* 用例列表 */}
      <Card size="small" title={`评测用例（共 ${caseTotal} 条）`} style={{ marginBottom: 16 }}>
        <QueryBar onSearch={() => loadCases(1)} onReset={() => { setFDs(undefined); setFScene(''); loadCases(1) }} loading={caseLoading}>
          <Select placeholder="数据源" allowClear style={{ width: 180 }} value={fDs} onChange={(v) => setFDs(v)}
            options={dsList.map((d) => ({ label: d.name, value: d.id }))} />
          <Input placeholder="场景码（如 mgmt_ops）" allowClear style={{ width: 180 }} value={fScene}
            onChange={(e) => setFScene(e.target.value)} onPressEnter={() => loadCases(1)} />
        </QueryBar>
        <Table
          rowKey="id"
          size="small"
          loading={caseLoading}
          dataSource={cases}
          pagination={{ current: casePage, pageSize: 20, total: caseTotal, showTotal: (t) => `共 ${t} 条`, onChange: (p) => loadCases(p) }}
          locale={{ emptyText: '暂无用例，点击右上角「新增用例」或通过反馈加入评测集' }}
          expandable={{
            expandedRowRender: (c) => (
              <Descriptions size="small" column={2} bordered>
                <Descriptions.Item label="期望表">{c.expect_tables.join('、') || '-'}</Descriptions.Item>
                <Descriptions.Item label="期望指标">{c.expect_metrics.join('、') || '-'}</Descriptions.Item>
                <Descriptions.Item label="期望过滤">{c.expect_filters.join('、') || '-'}</Descriptions.Item>
                <Descriptions.Item label="场景">{c.scene_code || '-'}</Descriptions.Item>
                <Descriptions.Item label="期望 SQL" span={2}><code style={{ fontSize: 12 }}>{c.expect_sql || '-'}</code></Descriptions.Item>
                <Descriptions.Item label="标签" span={2}>{c.tags || '-'}</Descriptions.Item>
              </Descriptions>
            ),
          }}
          columns={[
            { title: 'ID', dataIndex: 'id', width: 60 },
            { title: '问题', dataIndex: 'question', ellipsis: true },
            { title: '数据源', dataIndex: 'datasource_name', width: 150 },
            { title: '期望表', dataIndex: 'expect_tables', width: 180, render: (v: string[]) => (v || []).map((t, i) => <Tag key={i}>{t}</Tag>) },
            { title: '场景', dataIndex: 'scene_code', width: 110, render: (v: string) => v ? <Tag color="blue">{v}</Tag> : '-' },
            {
              title: '操作', width: 140,
              render: (_, c) => (
                <Space size={4}>
                  <Button size="small" onClick={() => editCase(c)}>编辑</Button>
                  <Popconfirm title="删除该用例？" onConfirm={() => removeCase(c.id)}>
                    <Button size="small" danger>删除</Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      {/* 批次列表 */}
      <Card size="small" title={`评测批次（共 ${runTotal} 次）`} extra={<Button size="small" icon={<ReloadOutlined />} onClick={() => loadRuns()}>刷新</Button>}>
        <Table
          rowKey="id"
          size="small"
          loading={runLoading}
          dataSource={runs}
          pagination={{ current: runPage, pageSize: 10, total: runTotal, showTotal: (t) => `共 ${t} 条`, onChange: (p) => loadRuns(p) }}
          locale={{ emptyText: '暂无评测批次' }}
          columns={[
            { title: 'ID', dataIndex: 'id', width: 60 },
            { title: '批次名', dataIndex: 'name', width: 160 },
            { title: '状态', dataIndex: 'status', width: 100, render: (v: string) => (
              <Tag color={RUN_STATUS[v]?.color || 'default'}>{RUN_STATUS[v]?.label || v}</Tag>) },
            { title: '用例数', dataIndex: 'total', width: 80 },
            {
              title: 'E2E 准确率', dataIndex: 'metrics', width: 110,
              render: (m: Record<string, number | null>) => (m?.e2e_accuracy == null ? '-' : pct(m.e2e_accuracy)),
            },
            {
              title: '平均耗时', dataIndex: 'metrics', width: 100,
              render: (m: Record<string, number | null>) => (m?.avg_latency_ms == null ? '-' : `${m.avg_latency_ms} ms`),
            },
            {
              title: '执行时间', width: 170,
              render: (_, r) => <span style={{ fontSize: 12 }}>{r.started_at ? new Date(r.started_at).toLocaleString('zh-CN') : '-'}</span>,
            },
            {
              title: '操作', width: 90,
              render: (_, r) => <Button size="small" type="link" loading={reportLoading} onClick={() => viewReport(r.id)}>查看报告</Button>,
            },
          ]}
        />
      </Card>

      {/* 新增/编辑用例 Drawer */}
      <Drawer title={editingCaseId ? '编辑评测用例' : '新增评测用例'} open={caseOpen} onClose={() => { setCaseOpen(false); setEditingCaseId(null); caseForm.resetFields() }} width={520}>
        <Form form={caseForm} layout="vertical">
          <Form.Item name="datasource_id" label="数据源" rules={[{ required: true, message: '请选择数据源' }]}>
            <GlassSelect options={dsList.map((d) => ({ label: d.name, value: d.id }))} placeholder="选择数据源" />
          </Form.Item>
          <Form.Item name="question" label="业务问题" rules={[{ required: true, message: '请输入问题' }]}>
            <Input.TextArea rows={2} placeholder="如：本月各门店销售额 Top10" />
          </Form.Item>
          <Form.Item name="expect_tables" label="期望表（逗号分隔）">
            <Input placeholder="如：t_store, t_sale_order" />
          </Form.Item>
          <Form.Item name="expect_metrics" label="期望指标（逗号分隔）">
            <Input placeholder="如：销售额, 订单量" />
          </Form.Item>
          <Form.Item name="expect_filters" label="期望过滤条件（逗号分隔）">
            <Input placeholder="如：本月, 华东区" />
          </Form.Item>
          <Form.Item name="expect_sql" label="期望 SQL（可选，语义等价判定用）">
            <Input.TextArea rows={3} style={{ fontFamily: 'monospace', fontSize: 12 }} />
          </Form.Item>
          <Form.Item name="scene_code" label="场景码（可选）">
            <Input placeholder="如：mgmt_ops" />
          </Form.Item>
          <Form.Item name="tags" label="标签（逗号分隔）">
            <Input placeholder="如：回归, 门店" />
          </Form.Item>
          <Button type="primary" onClick={saveCase}>{editingCaseId ? '保存修改' : '添加用例'}</Button>
        </Form>
      </Drawer>

      {/* 发起批次 Modal */}
      <Modal title="发起评测批次" open={runOpen} onCancel={() => setRunOpen(false)} onOk={createRun} confirmLoading={runCreating} okText="发起">
        <Form form={runForm} layout="vertical" initialValues={{ mock_execute: true }}>
          <Form.Item name="name" label="批次名称">
            <Input placeholder="如：回归批次-0909" />
          </Form.Item>
          <Form.Item name="case_ids" label="指定用例（不选则执行全部启用用例）">
            <Select
              mode="multiple"
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="选择用例（留空=全部）"
              options={cases.map((c) => ({ label: `#${c.id} ${c.question}`, value: c.id }))}
            />
          </Form.Item>
          <Form.Item name="tags" label="按标签筛选（逗号分隔，与指定用例互斥优先）">
            <Input placeholder="如：回归" />
          </Form.Item>
          <Form.Item name="mock_execute" label="Mock 执行（不实连业务库）">
            <Typography.Text type="secondary" style={{ display: 'block', fontSize: 12, marginBottom: 8 }}>
              开启时仅验证 SQL 生成与语义等价判定，不真正执行查询；关闭时会对生成 SQL 做可执行性检查
            </Typography.Text>
            <Select options={[{ label: 'Mock 模式（默认）', value: true }, { label: '真实执行校验', value: false }]} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 报告 Drawer */}
      <Drawer title={report ? `评测报告 · ${report.name}（#${report.id}）` : '评测报告'} open={!!report}
        onClose={() => setReport(null)} width={760}>
        {report ? (
          <div>
            <Space style={{ marginBottom: 12 }}>
              <Tag color={RUN_STATUS[report.status]?.color || 'default'}>{RUN_STATUS[report.status]?.label || report.status}</Tag>
              <Tag>{report.mock_execute ? 'Mock 执行' : '真实执行'}</Tag>
              <Tag color="blue">用例 {report.total} 条</Tag>
              {report.finished_at ? <Typography.Text type="secondary" style={{ fontSize: 12 }}>完成于 {new Date(report.finished_at).toLocaleString('zh-CN')}</Typography.Text> : null}
            </Space>
            <Progress
              percent={report.total ? Math.round((report.results.length / report.total) * 100) : 0}
              status={report.status === 'success' ? 'success' : report.status === 'failed' ? 'exception' : 'active'}
              style={{ marginBottom: 16 }}
            />
            {renderMetric(report.metrics)}
            <Typography.Title level={5} style={{ marginTop: 16 }}>用例明细</Typography.Title>
            <Table
              rowKey="case_id"
              size="small"
              dataSource={report.results}
              pagination={{ pageSize: 10, showSizeChanger: false }}
              locale={{ emptyText: '暂无明细（批次执行中或未开始）' }}
              expandable={{
                expandedRowRender: (r) => (
                  <div>
                    <Space wrap style={{ marginBottom: 8 }}>
                      <Typography.Text type="secondary">LLM: {r.llm_used || '-'}</Typography.Text>
                      {r.error_msg ? <Tag color="red">错误: {r.error_msg}</Tag> : null}
                    </Space>
                    {r.detail?.generated_sql ? (
                      <div>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>生成 SQL：</Typography.Text>
                        <code style={{ display: 'block', fontSize: 12, whiteSpace: 'pre-wrap', background: 'rgba(0,0,0,0.03)', padding: 8, borderRadius: 6 }}>{String(r.detail.generated_sql ?? '')}</code>
                      </div>
                    ) : null}
                  </div>
                ),
              }}
              columns={[
                { title: '问题', dataIndex: 'question', ellipsis: true },
                { title: '意图', dataIndex: 'intent_ok', width: 90, render: okTag },
                { title: '表命中', dataIndex: 'tables_hit', width: 90, render: okTag },
                { title: '生成', dataIndex: 'sql_generated', width: 80, render: okTag },
                { title: '可执行', dataIndex: 'sql_executable', width: 90, render: okTag },
                { title: '正确', dataIndex: 'sql_correct', width: 80, render: okTag },
                { title: 'E2E', dataIndex: 'e2e_ok', width: 80, render: okTag },
                { title: '耗时', dataIndex: 'latency_ms', width: 90, render: (v: number | null) => (v == null ? '-' : `${v} ms`) },
              ]}
            />
          </div>
        ) : null}
      </Drawer>
    </div>
  )
}
