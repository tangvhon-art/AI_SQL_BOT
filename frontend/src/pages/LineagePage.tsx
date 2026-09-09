// C8 血缘图谱：表级/字段级血缘可视化（ECharts graph）
// - 选择数据源/表 → 方向（双向/上游/下游）→ 血缘图
// - 手动触发挖掘：QueryLog 关联边（confidence 30）+ 视图 DDL 边（confidence 100）
import { useEffect, useRef, useState } from 'react'
import * as echarts from 'echarts'
import { Button, Card, Empty, Radio, Select, Space, Tag, Typography } from 'antd'
import { ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { client } from '../api/client'
import type { Datasource, LineageGraph, TableMeta } from '../types'
import PageHeader from '../components/PageHeader'
import { useMessageApi } from '../hooks/useMessageApi'
import { useOptions } from '../hooks/useOptions'

const CATEGORY = [
  { name: '当前表' },
  { name: '上游（被依赖）' },
  { name: '下游（依赖它）' },
]

export default function LineagePage() {
  const { msgApi, ctx, toastError } = useMessageApi()
  const dsList = useOptions<Datasource>('datasources')
  const [dsId, setDsId] = useState<number | undefined>()
  const [tables, setTables] = useState<TableMeta[]>([])
  const [tableId, setTableId] = useState<number | undefined>()
  const [direction, setDirection] = useState('both')
  const [graph, setGraph] = useState<LineageGraph | null>(null)
  const [loading, setLoading] = useState(false)
  const [mining, setMining] = useState(false)
  const chartRef = useRef<HTMLDivElement>(null)
  const instanceRef = useRef<echarts.ECharts | null>(null)

  const loadTables = async (ds: number) => {
    setTables([])
    setTableId(undefined)
    setGraph(null)
    try {
      const r = await client.get<TableMeta[]>(`/datasources/${ds}/tables`)
      setTables(r.data)
    } catch (e) { toastError(e) }
  }

  const loadGraph = async (tid: number, dir: string) => {
    setLoading(true)
    try {
      const r = await client.get<LineageGraph>(`/lineage/${tid}`, { params: { direction: dir } })
      setGraph(r.data)
    } catch (e) { toastError(e) } finally { setLoading(false) }
  }

  const triggerMine = async () => {
    setMining(true)
    try {
      const r = await client.post('/lineage/mine', null, { params: dsId ? { datasource_id: dsId } : {} })
      msgApi.success(`挖掘完成：QueryLog 边 ${r.data.stats.query_edges} 条、视图 DDL 边 ${r.data.stats.ddl_edges} 条`)
      if (tableId) loadGraph(tableId, direction)
    } catch (e) { toastError(e) } finally { setMining(false) }
  }

  // ECharts 渲染：实例复用 + 卸载 dispose + resize
  useEffect(() => {
    if (!chartRef.current) return
    if (!instanceRef.current) {
      instanceRef.current = echarts.init(chartRef.current)
    }
    const chart = instanceRef.current
    if (!graph || !graph.nodes?.length) {
      chart.clear()
      return
    }
    const nodeIds = new Set(graph.nodes.map((n) => n.id))
    const links = (graph.edges || [])
      .filter((e) => nodeIds.has(e.source) && nodeIds.has(e.target))
      .map((e) => ({
        source: e.source,
        target: e.target,
        label: {
          show: true,
          formatter: [e.label, e.src_col && e.dst_col ? `${e.src_col}→${e.dst_col}` : ''].filter(Boolean).join(' / '),
          fontSize: 10,
          color: '#666',
        },
        lineStyle: {
          color: e.source_type === 'view_ddl' ? '#722ed1' : e.source_type === 'manual' ? '#fa8c16' : '#1677ff',
          width: e.source_type === 'view_ddl' ? 2.5 : 1.5,
          curveness: 0.12,
          opacity: 0.7,
        },
      }))
    chart.setOption({
      tooltip: {
        formatter: (p: { dataType: string; data: { name?: string; value?: string; source?: string }; dataIndex: number }) => {
          if (p.dataType === 'node') {
            const n = graph.nodes[p.dataIndex]
            return `<b>${p.data.name}</b><br/>来源：${n?.source || '未知'}<br/>说明：${n?.value || '-'}`
          }
          return ''
        },
      },
      legend: { data: CATEGORY.map((c) => c.name), bottom: 0 },
      series: [{
        type: 'graph',
        layout: 'force',
        roam: true,
        draggable: true,
        categories: CATEGORY,
        data: graph.nodes.map((n) => ({
          id: n.id,
          name: n.name,
          category: n.category ?? 1,
          value: n.value || '',
          source: n.source || '',
          symbolSize: n.category === 0 ? 56 : 40,
          itemStyle: n.category === 0 ? { color: '#1677ff', borderWidth: 2, borderColor: '#91caff' } : undefined,
          label: { show: true, fontSize: 11, fontWeight: n.category === 0 ? 700 : 500 },
        })),
        links,
        force: { repulsion: 320, edgeLength: [80, 160], gravity: 0.08 },
        emphasis: { focus: 'adjacency', lineStyle: { width: 3 } },
      }],
    }, true)
  }, [graph])

  useEffect(() => {
    const onResize = () => instanceRef.current?.resize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      instanceRef.current?.dispose()
      instanceRef.current = null
    }
  }, [])

  return (
    <div className="glass-page">
      <PageHeader
        title="血缘图谱"
        description="字段级血缘来自 QueryLog 已执行 SQL 的 JOIN/WHERE 跨表等值（confidence 30，蓝线）；表级血缘来自视图 DDL 解析（confidence 100，紫线）；支持手工边"
        extra={<Button icon={<ThunderboltOutlined />} loading={mining} onClick={triggerMine}>立即挖掘血缘</Button>}
      />
      {ctx}
      <Card size="small" style={{ marginBottom: 16 }}>
        <Space wrap>
          <Select
            placeholder="选择数据源"
            style={{ width: 220 }}
            showSearch
            optionFilterProp="label"
            value={dsId}
            onChange={(v) => { setDsId(v); loadTables(v) }}
            options={dsList.map((d) => ({ label: `${d.name}（${d.type}）`, value: d.id }))}
          />
          <Select
            placeholder="选择表"
            style={{ width: 260 }}
            showSearch
            optionFilterProp="label"
            loading={loading}
            value={tableId}
            disabled={!tables.length}
            onChange={(v) => { setTableId(v); loadGraph(v, direction) }}
            options={tables.map((t) => ({ label: `${t.table_name}（${t.comment || '无注释'}）`, value: t.id }))}
          />
          <Radio.Group value={direction} onChange={(e) => { setDirection(e.target.value); if (tableId) loadGraph(tableId, e.target.value) }}
            options={[
              { label: '双向', value: 'both' },
              { label: '上游', value: 'upstream' },
              { label: '下游', value: 'downstream' },
            ]}
            optionType="button"
          />
          <Button icon={<ReloadOutlined />} onClick={() => tableId && loadGraph(tableId, direction)}>刷新</Button>
        </Space>
      </Card>

      <Card size="small" title={
        <Space>
          <span>血缘图</span>
          {graph ? (
            <>
              <Tag color="blue">节点 {graph.nodes?.length || 0}</Tag>
              <Tag color="green">边 {graph.edges?.length || 0}</Tag>
              <Tag color="purple">紫=视图 DDL</Tag>
              <Tag color="blue">蓝=查询日志</Tag>
            </>
          ) : null}
        </Space>
      }>
        {graph && graph.nodes?.length ? (
          <div ref={chartRef} style={{ width: '100%', height: 560 }} />
        ) : (
          <Empty
            style={{ padding: '40px 0' }}
            description={
              <Space direction="vertical">
                <Typography.Text type="secondary">
                  {tableId ? '该表暂无血缘关系，点击「立即挖掘血缘」从查询日志与视图 DDL 中提取' : '选择数据源与表后展示血缘图'}
                </Typography.Text>
                {tableId ? <Button size="small" icon={<ThunderboltOutlined />} loading={mining} onClick={triggerMine}>立即挖掘</Button> : null}
              </Space>
            }
          />
        )}
      </Card>

      <Card size="small" title="血缘边说明" style={{ marginTop: 16 }}>
        <Space wrap>
          <Tag color="blue">查询日志（JOIN/WHERE 跨表等值字段，confidence 30）</Tag>
          <Tag color="purple">视图 DDL（FROM/JOIN 表级依赖，confidence 100）</Tag>
          <Tag color="orange">手工边（人工维护）</Tag>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>查询血缘由调度周期挖掘（默认 720 分钟），也可手动触发</Typography.Text>
        </Space>
      </Card>
    </div>
  )
}
