// 消息卡片：user 文本 / assistant 结果（四层结论+图表优先，SQL 默认收起）/ 进度 / 错误
import { useState } from 'react'
import { Alert, Button, Checkbox, Collapse, Descriptions, Select, Space, Spin, Tag, Typography } from 'antd'
import { LikeOutlined, DislikeOutlined, SaveOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { postSseStream } from '../api/sse'
import { errMsg } from '../api/client'
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

// Dashboard + AI 解读组合块（V2：总览解读 + 单卡重试 + 保存报告）
function DashboardBlock({ dashboard, aiInterpretation, aiLoading, interpretationTemplate, onRetryCard, retryDisabled, onSaveReport }: {
  dashboard: Record<string, unknown>
  aiInterpretation?: Record<string, unknown>
  aiLoading?: boolean
  interpretationTemplate?: string
  onRetryCard?: (subId: string, confirmedTables?: string[]) => void
  retryDisabled?: (subId: string) => boolean
  onSaveReport?: () => void
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
        <AiInterpretation result={interpretation} loading={aiLoading} rawText={interpretation?.rawText} templateName={interpretationTemplate} />
      )}
      {onSaveReport && (
        <Space size={4} style={{ alignSelf: 'flex-end' }}>
          <Button size="small" type="primary" icon={<SaveOutlined />} onClick={onSaveReport}>
            保存至报告中心
          </Button>
        </Space>
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
  /** 保存至报告中心（多查询 Dashboard 结果） */
  onSaveReport?: (msg: ChatMsg) => void
  /** 多查询 V2：重试防抖 */
  retryDisabled?: (subId: string) => boolean
  multiCancelLoading?: boolean
  /** 该消息在消息流中的下标（AI 解读结果写回消息内容用） */
  index?: number
  /** 该消息对应的原始问题（取上一个用户消息文本） */
  question?: string
  /** AI解读可选提示词列表（scene_type=ai_interpret） */
  prompts?: Array<{ id: number; name: string; is_default: boolean }>
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
  onMultiConfirm, onRetryCard, onCancelMulti, retryDisabled, multiCancelLoading, onSaveReport,
  index, question, prompts,
}: Props) {
  const c = (msg.content ?? {}) as Record<string, unknown>
  // AI 解读提示词选择（未选择时用场景默认提示词）
  const [selPromptId, setSelPromptId] = useState<number | null>(null)
  const effectivePromptId = selPromptId ?? prompts?.find((p) => p.is_default)?.id ?? null
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
    // ===== AI 解读（点击触发，在卡片内流式展示；结果写回消息内容，保存报告可带上）=====
    const dashboard = (c.dashboard ?? {}) as Record<string, unknown>
    const canInterpret = !!(
      Object.keys(dashboard).length || (columns.length > 0 && rows.length > 0)
      || chart.type || chart.option
    )
    const runInterpret = async () => {
      if (c.ai_interpretation_loading || index == null) return
      const patch = (content: Record<string, unknown>) => {
        const msgs = useChatStore.getState().messages
        let idx = msgs.findIndex((m) => m === msg)
        if (idx < 0) idx = index
        if (idx >= 0 && idx < msgs.length) {
          useChatStore.getState().patchMessage(idx, {
            content_type: 'result',
            content: content as never,
          })
        }
      }
      const data = Object.keys(dashboard).length
        ? dashboard
        : { columns, rows, chart, title: '查询结果' }
      const fallbackQ = String((dashboard as { original_question?: string }).original_question ?? '')
      patch({ ai_interpretation: null, ai_interpretation_loading: true, ai_interpretation_error: null })
      try {
        await postSseStream('/api/v1/ai/interpret', {
          question: question ?? fallbackQ,
          data,
          prompt_template_id: effectivePromptId ?? undefined,
        }, {
          onAiInterpretationStart: (p) => patch({
            ai_interpretation_template: String(p.template_name ?? ''),
          }),
          onAiInterpretation: (p) => patch({
            ai_interpretation: { rawText: String(p.raw_text ?? '') },
            ai_interpretation_loading: true,
            ai_interpretation_error: null,
          }),
          onAiInterpretationDone: (p) => {
            const res = (p as { result?: InterpretationResult }).result
            patch({
              ai_interpretation: res ?? {},
              ai_interpretation_loading: false,
              ai_interpretation_error: null,
            })
          },
          onAiInterpretationError: (p) => patch({
            ai_interpretation_loading: false,
            ai_interpretation_error: String(p.error ?? '解读失败'),
          }),
          onError: (p) => patch({
            ai_interpretation_loading: false,
            ai_interpretation_error: String((p as { msg?: string }).msg ?? '解读失败'),
          }),
        })
      } catch (e) {
        patch({ ai_interpretation_loading: false, ai_interpretation_error: errMsg(e) })
      }
    }
    const interpObj = (c.ai_interpretation ?? {}) as Record<string, unknown>
    const interpError = c.ai_interpretation_error ? String(c.ai_interpretation_error) : ''
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
            interpretationTemplate={String(c.ai_interpretation_template ?? '')}
            onRetryCard={onRetryCard}
            retryDisabled={retryDisabled}
            onSaveReport={() => onSaveReport?.(msg)}
          />
        ) : null}
        {/* AI 解读：错误提示（两种模式共用） */}
        {interpError ? (
          <Alert type="error" showIcon message={interpError} style={{ marginTop: 8 }} />
        ) : null}
        {/* AI 解读：单查询结果卡片内展示（dashboard 模式由 DashboardBlock 展示） */}
        {!c.dashboard && (c.ai_interpretation_loading || c.ai_interpretation) ? (
          <div style={{ marginTop: 8 }}>
            <AiInterpretation
              result={(c.ai_interpretation as InterpretationResult | undefined) ?? undefined}
              loading={!!c.ai_interpretation_loading}
              rawText={String(interpObj.rawText ?? '')}
              templateName={String(c.ai_interpretation_template ?? '')}
            />
          </div>
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
          {canInterpret ? (
            <Space size={4}>
              <Select
                size="small"
                style={{ minWidth: 130 }}
                placeholder="提示词（默认）"
                value={effectivePromptId ?? undefined}
                onChange={setSelPromptId}
                options={(prompts ?? []).map((p) => ({ label: p.name, value: p.id }))}
                allowClear
              />
              <Button
                size="small"
                type="text"
                icon={<ThunderboltOutlined />}
                loading={!!c.ai_interpretation_loading}
                onClick={runInterpret}
              >
                AI解读
              </Button>
            </Space>
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
