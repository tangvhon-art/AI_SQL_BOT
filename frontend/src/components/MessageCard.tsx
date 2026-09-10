// 消息卡片：user 文本 / assistant 结果（四层结论+图表优先，SQL 默认收起）/ 进度 / 错误
import { useState } from 'react'
import { Alert, Button, Checkbox, Collapse, Descriptions, Space, Spin, Tag, Typography } from 'antd'
import { LikeOutlined, DislikeOutlined, SaveOutlined } from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { ChatMsg } from '../types'
import ChartCard from './ChartCard'
import SqlBlock from './SqlBlock'
import { Dashboard, convertDashboardEvent } from './dashboard/Dashboard'
import MultiSpecPreview from './dashboard/MultiSpecPreview'
import MultiExecProgress from './dashboard/MultiExecProgress'
import { AiInterpretation } from './dashboard/AiInterpretation'
import { useChatStore } from '../stores/chat'
import { useMultiQueryStore } from '../stores/multiQuery'
import type { InterpretationResult, SubQuerySpec } from '../types/chart'

// Dashboard + AI 解读组合块（V2：总览解读 + 单卡重试）
function DashboardBlock({ dashboard, aiInterpretation, aiLoading, onRetryCard, retryDisabled }: {
  dashboard: Record<string, unknown>
  aiInterpretation?: Record<string, unknown>
  aiLoading?: boolean
  onRetryCard?: (subId: string, confirmedTables?: string[]) => void
  retryDisabled?: (subId: string) => boolean
}) {
  const data = convertDashboardEvent(dashboard)
  const interpretation = aiInterpretation as InterpretationResult | undefined
  const overview = String((dashboard as Record<string, unknown>).overview ?? '')
  return (
    <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 12 }}>
      {overview ? (
        <div style={{
          padding: '10px 14px', background: '#F5F3FF', borderRadius: 10,
          border: '1px solid #EDEAFD', fontSize: 13.5, color: '#4B3FD4', lineHeight: 1.6,
        }}>
          {overview}
        </div>
      ) : null}
      <Dashboard data={data} showToolbar={false} onRetryCard={onRetryCard} retryDisabled={retryDisabled} />
      {(interpretation || aiLoading) && (
        <AiInterpretation result={interpretation} loading={aiLoading} rawText={interpretation?.rawText} />
      )}
    </div>
  )
}

interface Props {
  msg: ChatMsg
  onSaveQuery?: (msg: ChatMsg) => void
  onFeedback?: (msgId: number | undefined, feedback: string) => void
  onClarifyConfirm?: (tables: string[], originalQuestion: string) => void
  /** 多查询 V2：确认执行（Phase A → B） */
  onMultiConfirm?: (subs: SubQuerySpec[]) => void
  /** 多查询 V2：单卡重试 */
  onRetryCard?: (subId: string, confirmedTables?: string[]) => void
  /** 多查询 V2：整体取消 */
  onCancelMulti?: () => void
  /** 多查询 V2：重试防抖 */
  retryDisabled?: (subId: string) => boolean
  multiCancelLoading?: boolean
}

// AI 回复正文：Markdown 渲染（支持标题/列表/表格/代码块/引用）
function MarkdownBody({ text }: { text: string }) {
  return (
    <div className="md-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  )
}

const INTENT_LABELS: Record<string, string> = {
  value: '数值查询', compare: '对比分析', ranking: '排序榜单',
  trend: '趋势分析', detail: '筛选明细', statistic: '统计分析',
}
const AGG_LABELS: Record<string, string> = {
  sum: '求和', count: '计数', distinct_count: '去重计数', avg: '均值', max: '最大值', min: '最小值',
}

// "我理解的问题"：QuerySpec 结构化参数展示（可据此判断意图识别是否准确）
function SpecCard({ spec }: { spec: Record<string, unknown> }) {
  if (!spec || !Object.keys(spec).length) return null
  const intent = String(spec.intent ?? 'value')
  const metrics = (spec.metrics ?? []) as Array<{ name?: string; agg?: string; unit?: string }>
  const dimensions = (spec.dimensions ?? []) as Array<{ name?: string; granularity?: string }>
  const filters = (spec.filters ?? []) as Array<{ field?: string; op?: string; value?: unknown }>
  const time = (spec.time ?? {}) as { expr?: string; start?: string; end?: string; granularity?: string }
  const rows = [
    ['意图', `${INTENT_LABELS[intent] ?? intent}`],
    ['指标', metrics.length ? metrics.map((m) => `${m.name ?? ''}（${AGG_LABELS[m.agg ?? 'sum'] ?? m.agg}）`).join('、') : '—'],
    ['维度', dimensions.length ? dimensions.map((d) => d.name ?? '').join('、') : '—'],
    ['条件', filters.length ? filters.map((f) => `${f.field ?? ''} ${f.op ?? ''} ${String(f.value ?? '')}`).join('、') : '—'],
    ['时间', time.expr ? `${time.expr}（${time.start ?? ''}~${time.end ?? ''}）` : time.start ? `${time.start}~${time.end}` : '—'],
  ]
  return (
    <Collapse
      size="small"
      style={{ marginBottom: 8, background: 'transparent', border: 'none' }}
      items={[{
        key: 'spec',
        label: (
          <Space size={6} wrap>
            <Tag color="blue" style={{ marginInlineEnd: 0 }}>我理解的问题</Tag>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>意图识别与要素拆解结果，判断理解是否准确</Typography.Text>
          </Space>
        ),
        children: (
          <Descriptions size="small" column={1} bordered
            items={rows.map(([k, v]) => ({ key: k, label: <Typography.Text style={{ fontSize: 12 }}>{k}</Typography.Text>, children: <Typography.Text style={{ fontSize: 12 }}>{v}</Typography.Text> }))}
          />
        ),
      }]}
    />
  )
}

// 口径与溯源：数据源映射 / SQL / 权限 / 耗时
function TracePanel({ trace }: { trace: Record<string, unknown> }) {
  if (!trace || !Object.keys(trace).length) return null
  const mapping = (trace.mapping ?? {}) as Record<string, unknown>
  const metrics = (mapping.metrics ?? []) as Array<{ name?: string; column?: string; table?: string; agg?: string }>
  const dims = (mapping.dimensions ?? []) as Array<{ name?: string; column?: string; table?: string }>
  const filters = (mapping.filters ?? []) as Array<{ field?: string; column?: string; op?: string; value?: unknown }>
  const timeField = (mapping.time_field ?? {}) as { column?: string; comment?: string } | null
  const time = (trace.time ?? {}) as { expr?: string; start?: string; end?: string; granularity?: string }
  // 数据源表：优先带注释明细，回退纯表名列表
  const tableDetails = (trace.table_details ?? []) as Array<{ table?: string; comment?: string }>
  const tablesTxt = tableDetails.length
    ? tableDetails.map((t) => (t.comment ? `${t.table}（${t.comment}）` : t.table)).join('、')
    : String(((trace.tables ?? []) as string[])?.join('、') || '—')
  // 列名缺失（LLM 兜底路径无字段级映射）时省略 .column，避免展示"表.undefined"
  const fmtMetric = (m: { name?: string; table?: string; column?: string; agg?: string }) =>
    `${m.name}→${m.table || '?'}${m.column ? `.${m.column}` : ''}(${m.agg ?? 'sum'})`
  const fmtDim = (d: { name?: string; table?: string; column?: string }) =>
    `${d.name}→${d.table || '?'}${d.column ? `.${d.column}` : ''}`
  const fmtFilter = (f: { field?: string; column?: string; op?: string; value?: unknown }) =>
    `${f.field} ${f.op ?? '='} ${String(f.value ?? '')}${f.column ? `→${f.column}` : '（LLM 翻译字段）'}`
  const items = [
    { key: 'tables', label: '数据源表', children: tablesTxt },
    { key: 'metrics', label: '指标映射', children: metrics.length ? metrics.map(fmtMetric).join('；') : '—' },
    { key: 'dims', label: '维度映射', children: dims.length ? dims.map(fmtDim).join('；') : '—' },
    { key: 'filters', label: '条件映射', children: filters.length ? filters.map(fmtFilter).join('；') : '—' },
    { key: 'time', label: '时间口径', children: time.expr ? `${time.expr}（${time.start ?? ''}~${time.end ?? ''}，粒度 ${time.granularity ?? '-'}）` : '—' },
    { key: 'permission', label: '权限注入', children: String(trace.permission ?? '—') },
    { key: 'latency', label: '耗时', children: `${String(trace.latency_ms ?? '-')} ms` },
    { key: 'mode', label: 'SQL 来源', children: trace.template_sql ? '分析规则模板（确定性）' : 'LLM 翻译（兜底）' },
  ]
  return (
    <Collapse
      size="small"
      style={{ marginTop: 8, background: 'transparent', border: 'none' }}
      items={[{
        key: 'trace',
        label: (
          <Space size={6}>
            <Tag color="cyan" style={{ marginInlineEnd: 0 }}>口径与溯源</Tag>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>数据从哪来、怎么算的，全程可追溯</Typography.Text>
          </Space>
        ),
        children: (
          <Descriptions size="small" column={1} bordered
            items={items.map((it) => ({ ...it, label: <Typography.Text style={{ fontSize: 12 }}>{it.label}</Typography.Text>, children: <Typography.Text style={{ fontSize: 12 }}>{String(it.children ?? '—')}</Typography.Text> }))}
          />
        ),
      }]}
    />
  )
}

// 四层结论：核心数据结果 / 变化分析 / 亮点与异常 / 极简总结
function SummarySections({ sections }: { sections: Record<string, string> }) {
  if (!sections || !Object.keys(sections).length) return null
  return (
    <div style={{ marginBottom: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
      {sections.result ? (
        <div style={{ fontSize: 15, fontWeight: 600, color: '#1A1B1C', lineHeight: 1.5 }}>{sections.result}</div>
      ) : null}
      {sections.change ? (
        <div style={{ fontSize: 13, color: 'rgba(0,0,0,.72)', lineHeight: 1.6 }}>{sections.change}</div>
      ) : null}
      {sections.highlight ? (
        <div style={{ fontSize: 13, color: '#6C5CE7', lineHeight: 1.6 }}>{sections.highlight}</div>
      ) : null}
      {sections.summary ? (
        <div style={{ fontSize: 13, color: 'rgba(0,0,0,.6)', lineHeight: 1.6 }}>{sections.summary}</div>
      ) : null}
    </div>
  )
}

// 口径澄清：表歧义（table 候选）或 要素缺失（spec 候选，字段为 name）
function ClarifyCard({ text, candidates, originalQuestion, kind, onConfirm }: {
  text: string
  candidates: Array<{ table: string; comment?: string; name?: string; column?: string }>
  originalQuestion?: string
  kind?: string
  onConfirm?: (tables: string[], originalQuestion: string) => void
}) {
  const [checked, setChecked] = useState<string[]>(
    candidates.map((c) => (c.name ? String(c.name) : c.table)),
  )
  const isSpec = kind === 'spec'
  const label = (c: { table: string; comment?: string; name?: string; column?: string }) =>
    c.name ? `${c.name}${c.column ? `（${c.column}）` : ''}` : (c.comment ? `${c.comment}（${c.table}）` : c.table)
  return (
    <div className="glass-msg-assistant" style={{ margin: '10px 0', padding: '12px 16px', maxWidth: '100%' }}>
      <MarkdownBody text={text} />
      {candidates.length ? (
        <div style={{ marginTop: 10 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
            {isSpec ? '请选择（可多选，确认后将自动带入查询）：' : '请勾选需要查询的表（可多选，将分别查询；确认后结合原始问题生成 SQL）：'}
          </Typography.Text>
          <Checkbox.Group
            value={checked}
            onChange={(v) => setChecked(v as string[])}
            style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 10 }}
          >
            {candidates.map((cd) => {
              const val = cd.name ? String(cd.name) : cd.table
              return (
                <Checkbox key={val} value={val}>
                  {label(cd)}
                </Checkbox>
              )
            })}
          </Checkbox.Group>
          <Button
            type="primary"
            size="small"
            disabled={checked.length === 0}
            onClick={() => onConfirm?.(checked, originalQuestion ?? '')}
          >
            {isSpec ? `确认查询（${checked.length} 项）` : `确认查询（${checked.length} 张表）`}
          </Button>
        </div>
      ) : null}
    </div>
  )
}

export default function MessageCard({
  msg, onSaveQuery, onFeedback, onClarifyConfirm,
  onMultiConfirm, onRetryCard, onCancelMulti, retryDisabled, multiCancelLoading,
}: Props) {
  const c = (msg.content ?? {}) as Record<string, unknown>
  // 多查询 V2 状态（预览/执行进度由全局 store 驱动）
  const multiPhase = useMultiQueryStore((s) => s.phase)
  const preview = useMultiQueryStore((s) => s.preview)
  const confirmLoading = useMultiQueryStore((s) => s.confirmLoading)
  // 全局 busy：拆解中 / 确认执行中 / 重试中 / 取消中 → 禁用所有可点击元素，防止重复点击
  const streaming = useChatStore((s) => s.streaming)
  const busy = streaming || confirmLoading || !!multiCancelLoading

  // Phase A 拆解预览面板（消息为 multi_preview 或恢复历史预览）
  const previewPayload = c.multi_spec as Record<string, unknown> | undefined
  if (msg.content_type === 'multi_preview' || (c.mode === 'multi_preview' && previewPayload)) {
    const subs = (previewPayload?.sub_queries ?? []) as SubQuerySpec[]
    const list = subs.length ? subs : preview
    if (list.length) {
      const specObj = c.query_spec as Record<string, unknown> | undefined
      return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, margin: '10px 0', maxWidth: '100%' }}>
          {/* 拆解结果后展示"我理解的问题"：原始问题 + 意图识别摘要（可纠错） */}
          {specObj ? <SpecCard spec={specObj} /> : null}
          <MultiSpecPreview
            subQueries={list}
            confirming={confirmLoading}
            busy={busy}
            onConfirm={(confirmed) => onMultiConfirm?.(confirmed)}
            onCancel={onCancelMulti}
          />
        </div>
      )
    }
  }

  if (msg.role === 'user') {
    return (
      <div style={{ display: 'flex', justifyContent: 'flex-end', margin: '10px 0' }}>
        {/* 气泡宽度由 CSS 控制：宽屏 80%、窄屏放宽到 92%，保证问题完整可读 */}
        <div
          className="glass-msg-user"
          style={{
            padding: '9px 15px',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            color: '#fff',
          }}
        >
          {String(c.text ?? '')}
        </div>
      </div>
    )
  }

  if (msg.content_type === 'clarify') {
    return (
      <ClarifyCard
        text={String(c.text ?? '')}
        candidates={(c.candidates ?? []) as Array<{ table: string; comment?: string; name?: string; column?: string }>}
        originalQuestion={String(c.original_question ?? '')}
        kind={String(c.clarify_kind ?? '')}
        onConfirm={onClarifyConfirm}
      />
    )
  }

  if (msg.content_type === 'progress') {
    const streamText = String(c.stream_text ?? '')
    const thinkingText = String(c.thinking_text ?? '')
    return (
      <div style={{ margin: '10px 0' }}>
        <Spin size="small" /> <Typography.Text type="secondary">{String(c.msg ?? '处理中…')}</Typography.Text>
        {/* 思考过程：生成中默认展开，流式展示；最终结果出来后自动收起 */}
        {thinkingText ? (
          <Collapse
            size="small"
            defaultActiveKey={['thinking']}
            style={{ marginTop: 8, background: 'transparent', border: 'none' }}
            items={[{
              key: 'thinking',
              label: <Typography.Text type="secondary" style={{ fontSize: 12 }}>思考过程</Typography.Text>,
              children: (
                <div style={{
                  whiteSpace: 'pre-wrap', fontSize: 12, color: '#888',
                  maxHeight: 300, overflow: 'auto', lineHeight: 1.6,
                }}>{thinkingText}</div>
              ),
            }]}
          />
        ) : null}
        {streamText ? (
          <pre style={{
            marginTop: 8, padding: '10px 12px', background: '#f6f8fa',
            borderRadius: 6, fontSize: 12, maxHeight: 320, overflow: 'auto',
            whiteSpace: 'pre-wrap', wordBreak: 'break-all', marginBottom: 0,
          }}>{streamText}</pre>
        ) : null}
      </div>
    )
  }

  if (msg.content_type === 'error') {
    return <Alert type="error" showIcon message={String(c.msg ?? '发生错误')} style={{ margin: '10px 0' }} />
  }

  if (msg.content_type === 'result') {
    const chart = (c.chart ?? {}) as Record<string, unknown>
    const columns = (c.columns ?? []) as string[]
    const rows = (c.rows ?? []) as unknown[][]
    const sections = (c.sections ?? {}) as Record<string, string>
    const anomalies = (c.anomalies ?? []) as Array<{ type: string; desc: string }>
    const querySpec = (c.query_spec ?? {}) as Record<string, unknown>
    const trace = (c.trace ?? {}) as Record<string, unknown>
    // 多查询 V2：执行阶段 → 实时进度面板（store 驱动）；未确认预览 → 预览面板（上面已处理）
    if (multiPhase === 'executing' && !c.dashboard) {
      return (
        <MultiExecProgress
          onRetryCard={onRetryCard}
          onCancelAll={onCancelMulti}
          cancelLoading={multiCancelLoading}
          busy={busy}
        />
      )
    }
    return (
      <div
        className="glass-msg-assistant"
        style={{
          margin: '10px 0',
          padding: '12px 16px',
          maxWidth: '100%',
        }}
      >
        {/* 思考过程：默认收起，支持展开查看（仅在有思考内容时展示） */}
        {c.thinking_text ? (
          <Collapse
            size="small"
            style={{ marginBottom: 8, background: 'transparent', border: 'none' }}
            items={[{
              key: 'thinking',
              label: <Typography.Text type="secondary" style={{ fontSize: 12 }}>思考过程</Typography.Text>,
              children: (
                <div style={{
                  whiteSpace: 'pre-wrap',
                  fontSize: 12,
                  color: '#888',
                  maxHeight: 300,
                  overflow: 'auto',
                  lineHeight: 1.6,
                }}>{String(c.thinking_text)}</div>
              ),
            }]}
          />
        ) : null}
        {/* "我理解的问题"：QuerySpec 结构化参数（可纠错）—— 多查询模式下隐藏，避免与结果卡片混排 */}
        {!c.dashboard && <SpecCard spec={querySpec} />}
        {/* 四层结论（确定性计算 + LLM 解读） */}
        {sections.result ? <SummarySections sections={sections} /> : c.text ? (
          <div style={{ marginBottom: 8 }}>
            <MarkdownBody text={String(c.text)} />
          </div>
        ) : null}
        {/* 异常标注（数据合理性校验：骤增骤降/负值/空数据/波动） */}
        {anomalies.length ? anomalies.map((a, i) => (
          <Alert key={i} type="warning" showIcon message={a.desc} style={{ marginBottom: 8 }} />
        )) : null}
        {/* 文件问答：引用来源 */}
        {c.references && Array.isArray(c.references) && c.references.length > 0 ? (
          <Collapse
            size="small"
            style={{ marginBottom: 8, background: 'transparent', border: 'none' }}
            items={[{
              key: 'references',
              label: (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  引用来源（{c.references.length}）
                </Typography.Text>
              ),
              children: (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {(c.references as Array<Record<string, unknown>>).map((ref, i) => {
                    const isFile = ref.source_type === 'file'
                    const loc = String(isFile
                      ? [ref.file_name ? `《${ref.file_name}》` : '', ref.sheet_name ? `Sheet:${ref.sheet_name}` : '', ref.page_num ? `第${ref.page_num}页` : ''].filter(Boolean).join(' ')
                      : (ref.title || '知识库'))
                    return (
                      <div key={i} style={{
                        display: 'flex', gap: 8, alignItems: 'flex-start',
                        padding: '6px 10px', background: '#F8F9FB', borderRadius: 8,
                      }}>
                        <span style={{ fontSize: 14, flexShrink: 0 }}>{isFile ? '📄' : '📚'}</span>
                        <div style={{ minWidth: 0, flex: 1 }}>
                          <div style={{ fontSize: 12, color: '#555', fontWeight: 500, marginBottom: 2 }}>
                            [{i + 1}] {loc}
                            {typeof ref.score === 'number' ? (
                              <span style={{ color: '#bbb', marginLeft: 6, fontWeight: 400 }}>
                                相似度 {Math.round(ref.score * 100)}%
                              </span>
                            ) : null}
                          </div>
                          <div style={{
                            fontSize: 11, color: '#888', lineHeight: 1.5,
                            display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical',
                            overflow: 'hidden',
                          }} title={String(ref.content ?? '')}>
                            {String(ref.content ?? '')}
                          </div>
                        </div>
                      </div>
                    )
                  })}
                </div>
              ),
            }]}
          />
        ) : null}
        {chart.type === 'metric' || chart.option || chart.type === 'table' ? (
          !c.dashboard ? <ChartCard chart={chart as never} /> : null
        ) : null}
        {/* 多查询 Dashboard 模式（V2：总览 + 单卡重试） */}
        {c.dashboard ? (
          <DashboardBlock
            dashboard={c.dashboard as never}
            aiInterpretation={c.ai_interpretation as never}
            aiLoading={!!c.ai_interpretation_loading}
            onRetryCard={onRetryCard}
            retryDisabled={retryDisabled}
          />
        ) : null}
        {c.sql ? <SqlBlock sql={String(c.sql)} permission={String(c.permission ?? '')} /> : null}
        {/* 口径与溯源 */}
        <TracePanel trace={trace} />
        <Space style={{ marginTop: 8 }} size={4}>
          {c.sql ? (
            <Button
              size="small"
              type="text"
              icon={<SaveOutlined />}
              onClick={() => onSaveQuery?.(msg)}
            >
              保存为查询
            </Button>
          ) : null}
          <Button size="small" type="text" icon={<LikeOutlined />} onClick={() => onFeedback?.(msg.id, 'good')} />
          <Button size="small" type="text" icon={<DislikeOutlined />} onClick={() => onFeedback?.(msg.id, 'bad')} />
        </Space>
      </div>
    )
  }

  return (
    <div
      className="glass-msg-assistant"
      style={{ margin: '10px 0', padding: '12px 16px', maxWidth: '100%' }}
    >
      <MarkdownBody text={String(c.text ?? '')} />
    </div>
  )
}
