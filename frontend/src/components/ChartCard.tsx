// ECharts 图表卡片：metric / line / area / bar / stacked_bar / grouped_bar / pie / table 降级
// 切换按钮（柱状/折线/表格/还原）位置固定、顺序固定，切换时按钮不跳动
import { useEffect, useRef, useState } from 'react'
import type { Key } from 'react'
import * as echarts from 'echarts'
import { Button, Descriptions, Radio, Space, Table, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'

interface ChartPayload {
  type: string
  value?: unknown
  label?: string
  option?: Record<string, unknown>
  columns?: string[]
  rows?: unknown[][]
}

// 单元格内容超过该长度时收起，鼠标悬停/点击行可查看完整内容
const MAX_CONTENT_LEN = 60

// 表格行类型：key 用于行唯一标识 + 展开控制
type ChartRow = Record<string, unknown> & { key: number }

// 固定顺序的图表切换选项（柱状/折线/表格 场景共用）
const SWITCH_OPTIONS = [
  { label: '柱状', value: 'bar' },
  { label: '折线', value: 'line' },
  { label: '表格', value: 'table' },
]

// 饼图等特殊类型的切换选项（仅表格）
const PIE_SWITCH_OPTIONS = [{ label: '表格', value: 'table' }]

function exportCSV(columns: string[], rows: unknown[][], filename: string) {
  const escape = (v: unknown) => {
    const s = String(v ?? '')
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  const csv = [columns.map(escape).join(','),
    ...rows.map((r) => r.map(escape).join(','))].join('\n')
  const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

export default function ChartCard({ chart }: { chart: ChartPayload }) {
  const ref = useRef<HTMLDivElement>(null)
  const instance = useRef<echarts.ECharts | null>(null)
  const [chartType, setChartType] = useState(chart.type || 'table')
  const [expandedKeys, setExpandedKeys] = useState<Key[]>([])

  const toggleExpand = (key: Key) => {
    setExpandedKeys((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key],
    )
  }

  // 是否展示图表（表格模式或无 option 时展示表格）
  const showChart = chartType !== 'table' && !!chart.option

  // 图表初始化与更新：实例在组件生命周期内复用，切换表格/还原推荐不销毁重建，
  // 避免容器卸载→dispose→重新 init 的时序问题导致白屏（尤其 pie 图）。
  useEffect(() => {
    if (!ref.current) return
    if (!instance.current) {
      instance.current = echarts.init(ref.current)
    }
    const option = (chart.option ?? {}) as Record<string, unknown>
    const series = (option.series ?? []) as unknown[]
    // 柱状/折线互切：仅修改 series.type，保留原有数据与配置（pie 等特殊类型不参与互切）
    if (chartType !== chart.type && chartType !== 'table' && series.length && chart.type !== 'pie') {
      const mapped = series.map((s) => ({
        ...(s as Record<string, unknown>),
        type: chartType === 'line' ? 'line' : 'bar',
      }))
      instance.current.setOption({ ...option, series: mapped }, true)
    } else {
      instance.current.setOption(option, true)
    }
    // 容器从隐藏(display:none)切回显示时，必须 resize 才能正确渲染
    if (showChart) {
      instance.current.resize()
    }
  }, [chart, chartType, showChart])

  // 窗口 resize 监听 + 组件卸载时 dispose（只在卸载时销毁实例）
  useEffect(() => {
    const onResize = () => instance.current?.resize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      instance.current?.dispose()
      instance.current = null
    }
  }, [])

  // 单值指标卡：无图表切换按钮
  if (chart.type === 'metric') {
    return (
      <div style={{ padding: '8px 0' }}>
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          {chart.label}
        </Typography.Text>
        <div style={{ fontSize: 30, fontWeight: 700, color: '#0e7490', marginTop: 4 }}>
          {String(chart.value ?? '-')}
        </div>
      </div>
    )
  }

  const isPie = chart.type === 'pie'
  const switchOptions = isPie ? PIE_SWITCH_OPTIONS : SWITCH_OPTIONS

  // 统一工具栏：按钮组固定位置与顺序 + CSV 导出
  const renderToolbar = () => (
    <Space style={{ marginBottom: 8 }}>
      <Radio.Group
        size="small"
        value={chartType}
        options={switchOptions}
        onChange={(e) => setChartType(e.target.value)}
        optionType="button"
      />
      <Button size="small" onClick={() => setChartType(chart.type)}>
        还原推荐
      </Button>
      <Button
        size="small"
        onClick={() => exportCSV(chart.columns ?? [], chart.rows ?? [], `query_result_${Date.now()}.csv`)}
      >
        导出 CSV
      </Button>
    </Space>
  )

  // 单元格渲染：超过长度收起（悬停/点击行查看完整内容）
  const renderCell = (v: unknown) => {
    if (v === null || v === undefined) return <Typography.Text type="secondary">-</Typography.Text>
    const s = String(v)
    if (s.length <= MAX_CONTENT_LEN) return s
    return (
      <Tooltip title={s}>
        <span style={{ cursor: 'default' }}>{s.slice(0, MAX_CONTENT_LEN)}…</span>
      </Tooltip>
    )
  }

  // 表格内容：横向滚动平铺 + 长字段收起 + 点击行展开明细
  const renderTable = () => {
    const allCols = chart.columns ?? []
    // 列宽按列名长度自适应，保证 max-content 横向滚动可计算；长内容由单元格 ellipsis 收起
    const estWidth = (name: string) => {
      const cn = (name.match(/[\u4e00-\u9fff]/g) ?? []).length
      const other = name.length - cn
      return Math.min(280, Math.max(110, cn * 16 + other * 9 + 32))
    }
    const cols = allCols.map((c) => ({
      title: <Tooltip title={c}><span style={{ fontWeight: 600 }}>{c}</span></Tooltip>,
      dataIndex: c,
      key: c,
      width: estWidth(c),
      ellipsis: { showTitle: false } as const,
      render: (v: unknown) => renderCell(v),
    })) as ColumnsType<ChartRow>
    const rows = (chart.rows ?? []).map((r, i) => {
      const obj = { key: i } as ChartRow
      allCols.forEach((c, j) => (obj[c] = r[j]))
      return obj
    })
    return (
      // 外层 overflow-x:auto 双保险：列总宽超出容器时出现横向滚动条
      <div style={{ width: '100%', overflowX: 'auto', overflowY: 'hidden' }}>
        <Table<ChartRow>
          size="small"
          columns={cols}
          dataSource={rows}
          rowKey="key"
          pagination={rows.length > 20 ? { pageSize: 20, size: 'small' } : false}
          scroll={{ x: 'max-content' }}
          rowClassName={() => 'chart-table-row-clickable'}
          onRow={(record) => ({
            onClick: () => toggleExpand(record.key),
            style: { cursor: 'pointer' },
          })}
          expandable={{
            expandedRowKeys: expandedKeys,
            onExpandedRowsChange: (keys) => setExpandedKeys([...keys]),
            // 展开图标点击不触发行点击的二次切换
            expandIcon: ({ expanded, onExpand, record }) => (
              <span
                onClick={(e) => { e.stopPropagation(); onExpand(record, e) }}
                style={{ display: 'inline-block', width: 16, cursor: 'pointer', color: '#6C5CE7' }}
              >
                {expanded ? '−' : '+'}
              </span>
            ),
            expandedRowRender: (r) => {
              const record = r as Record<string, unknown>
              return (
                <Descriptions
                  size="small"
                  column={1}
                  bordered
                  items={allCols.map((c) => ({
                    key: c,
                    label: c,
                    children: <span style={{ wordBreak: 'break-all', whiteSpace: 'pre-wrap' }}>{String(record[c] ?? '-')}</span>,
                  }))}
                />
              )
            },
          }}
        />
      </div>
    )
  }

  // 图表容器始终挂载，用 display 控制显隐：
  // 避免表格模式下卸载图表容器→dispose 实例→还原推荐时重建实例的时序白屏问题。
  // 表格改为仅激活时挂载：若在 display:none 容器中常驻挂载，Ant Table 会在隐藏状态下
  // 测量容器宽度（结果为 0/陈旧值），切到表格视图时列宽被压缩成“挤压”状态，需二次切换才恢复。
  return (
    <div style={{ margin: '8px 0' }}>
      {renderToolbar()}
      <div
        ref={ref}
        style={{
          width: '100%',
          height: isPie ? 360 : 300,
          display: showChart ? 'block' : 'none',
        }}
      />
      {showChart ? null : renderTable()}
    </div>
  )
}
