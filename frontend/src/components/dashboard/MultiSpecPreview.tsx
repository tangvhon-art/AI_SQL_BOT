// Phase A 拆解预览面板：子查询列表（勾选/编辑/删除/新增）+ 选表澄清 + 确认/重拆/取消
// 组件持有本地可编辑清单，确认时把最终清单交给父层 → confirm 接口进入 Phase B
import { useEffect, useState } from 'react'
import {
  Alert, Button, Checkbox, Input, Space, Tag, Tooltip, Typography,
} from 'antd'
import {
  DeleteOutlined, EditOutlined, PlusOutlined, ReloadOutlined,
  CheckOutlined, CloseOutlined, TableOutlined, SearchOutlined,
} from '@ant-design/icons'
import type { SubQuerySpec } from '../../types/chart'

const INTENT_LABELS: Record<string, string> = {
  value: '数值', compare: '对比', ranking: '排行',
  trend: '趋势', detail: '明细', statistic: '统计',
}

interface Props {
  subQueries: SubQuerySpec[]
  confirming?: boolean
  /** 全局 busy：拆解中/确认执行中/重试中/取消中 → 禁用所有操作按钮 */
  busy?: boolean
  onConfirm: (subs: SubQuerySpec[]) => void
  onRegen?: () => void
  onCancel?: () => void
}

export default function MultiSpecPreview({
  subQueries, confirming, busy, onConfirm, onRegen, onCancel,
}: Props) {
  // 本地可编辑清单（初始来自拆解结果，用户编辑只影响本组件）
  const [items, setItems] = useState<SubQuerySpec[]>(subQueries)
  useEffect(() => { setItems(subQueries) }, [subQueries])

  const [editingId, setEditingId] = useState<string | null>(null)
  const [draft, setDraft] = useState({ question: '', title: '' })
  // 每个子查询的待选表搜索关键词（按 sub_id 隔离）
  const [tableSearch, setTableSearch] = useState<Record<string, string>>({})

  const enabledCount = items.filter((q) => q.enabled !== false).length
  // 需澄清但未选表的子查询数（阻塞确认）
  const blockedCount = items.filter(
    (q) => q.enabled !== false && q.needs_tables && !(q.confirmed_tables ?? []).length).length

  const startEdit = (q: SubQuerySpec) => {
    setEditingId(q.sub_id)
    setDraft({ question: q.question, title: q.title ?? '' })
  }
  const commitEdit = () => {
    setItems((prev) => prev.map((q) =>
      q.sub_id === editingId ? { ...q, question: draft.question.trim() || q.question, title: draft.title.trim() || q.title } : q))
    setEditingId(null)
  }
  const patch = (subId: string, p: Partial<SubQuerySpec>) =>
    setItems((prev) => prev.map((q) => (q.sub_id === subId ? { ...q, ...p } : q)))

  const fmtTime = (q: SubQuerySpec) => {
    const expr = q.time?.expr
    const start = q.time?.start
    const end = q.time?.end
    if (expr) return expr
    if (start && end) return `${start}~${end}`
    return ''
  }
  const fmtMetrics = (q: SubQuerySpec) => (q.metrics ?? []).map((m) => m.name).filter(Boolean).join('、')
  const fmtDims = (q: SubQuerySpec) => (q.dimensions ?? []).map((d) => d.name).filter(Boolean).join('、')

  const lowConfidence = (q: SubQuerySpec) =>
    typeof q.confidence === 'number' && q.confidence < 0.5

  return (
    <div className="glass-msg-assistant" style={{ margin: '10px 0', padding: '14px 16px', maxWidth: '100%' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
        <Space size={8}>
          <Tag color="purple" style={{ marginInlineEnd: 0 }}>拆解确认</Tag>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            已拆解为 {items.length} 个子查询，请确认后执行
          </Typography.Text>
        </Space>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          已启用 {enabledCount} 个
        </Typography.Text>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {items.map((q) => {
          const enabled = q.enabled !== false
          const editing = editingId === q.sub_id
          return (
            <div
              key={q.sub_id}
              style={{
                border: '1px solid #EDEAFD', borderRadius: 8, padding: '10px 12px',
                background: enabled ? '#FAFAFE' : '#F5F5F5',
                opacity: enabled ? 1 : 0.62,
              }}
            >
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
                <Checkbox
                  checked={enabled}
                  disabled={!!busy}
                  onChange={(e) => patch(q.sub_id, { enabled: e.target.checked })}
                  style={{ marginTop: 3 }}
                />
                <div style={{ flex: 1, minWidth: 0 }}>
                  {editing ? (
                    <Space direction="vertical" size={4} style={{ width: '100%' }}>
                      <Input
                        size="small" placeholder="子问题（完整可独立执行的查询）"
                        value={draft.question}
                        onChange={(e) => setDraft((d) => ({ ...d, question: e.target.value }))}
                        maxLength={200}
                      />
                      <Input
                        size="small" placeholder="卡片标题"
                        value={draft.title}
                        onChange={(e) => setDraft((d) => ({ ...d, title: e.target.value }))}
                        maxLength={30}
                      />
                      <Space size={4}>
                        <Button size="small" type="primary" icon={<CheckOutlined />} onClick={commitEdit}>
                          保存
                        </Button>
                        <Button size="small" icon={<CloseOutlined />} onClick={() => setEditingId(null)}>
                          取消
                        </Button>
                      </Space>
                    </Space>
                  ) : (
                    <>
                      <div style={{ fontSize: 13.5, fontWeight: 600, color: '#1A1B1C', lineHeight: 1.5 }}>
                        {q.title || q.question}
                        {enabled ? null : (
                          <Tag color="default" style={{ marginLeft: 8, fontSize: 11 }}>已停用</Tag>
                        )}
                      </div>
                      <div style={{ marginTop: 4, display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' }}>
                        <Tag color="blue" style={{ fontSize: 11, marginInlineEnd: 0 }}>
                          {INTENT_LABELS[q.intent] ?? q.intent}
                        </Tag>
                        {fmtMetrics(q) ? (
                          <Typography.Text style={{ fontSize: 12, color: '#555' }}>
                            指标：{fmtMetrics(q)}
                          </Typography.Text>
                        ) : null}
                        {fmtDims(q) ? (
                          <Typography.Text style={{ fontSize: 12, color: '#555' }}>
                            维度：{fmtDims(q)}
                          </Typography.Text>
                        ) : null}
                        {fmtTime(q) ? (
                          <Typography.Text style={{ fontSize: 12, color: '#555' }}>
                            时间：{fmtTime(q)}
                          </Typography.Text>
                        ) : null}
                        {q.chart_hint ? (
                          <Tag color="green" style={{ fontSize: 11, marginInlineEnd: 0 }}>
                            {q.chart_hint}
                          </Tag>
                        ) : null}
                      </div>
                      {lowConfidence(q) ? (
                        <Alert
                          type="warning" showIcon style={{ marginTop: 6, padding: '2px 10px', fontSize: 12 }}
                          message={<span style={{ fontSize: 12 }}>未识别到完整指标/维度，可编辑补全</span>}
                        />
                      ) : null}
                      {q.needs_tables ? (() => {
                        const raw = q.candidate_tables ?? []
                        const kw = (tableSearch[q.sub_id] || '').trim().toLowerCase()
                        const filtered = kw
                          ? raw.filter((t) =>
                              t.table.toLowerCase().includes(kw) ||
                              (t.comment || '').toLowerCase().includes(kw))
                          : raw
                        return (
                        <div style={{
                          marginTop: 8, border: '1px dashed #B7A8F8', borderRadius: 8,
                          padding: '8px 10px', background: '#FBF9FF',
                        }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8, flexWrap: 'wrap' }}>
                            <TableOutlined style={{ color: '#7B61E8' }} />
                            <span style={{ fontSize: 12.5, fontWeight: 600, color: '#4B3FD4' }}>
                              请选择该子查询要查询的数据表（可多选）
                            </span>
                            <span style={{ fontSize: 11, color: '#8c8c8c' }}>
                              {kw ? `匹配 ${filtered.length}/${raw.length}` : `共 ${raw.length} 张`}
                            </span>
                            <Input
                              size="small"
                              placeholder="搜索表名或注释"
                              prefix={<SearchOutlined style={{ color: '#bfbfbf' }} />}
                              value={tableSearch[q.sub_id] || ''}
                              allowClear
                              disabled={!!busy}
                              onChange={(e) => setTableSearch((prev) => ({ ...prev, [q.sub_id]: e.target.value }))}
                              style={{ width: 200, marginLeft: 'auto' }}
                            />
                          </div>
                          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, maxHeight: 180, overflowY: 'auto' }}>
                            {filtered.map((t) => {
                              const checked = (q.confirmed_tables ?? []).includes(t.table)
                              return (
                                <Checkbox
                                  key={t.table}
                                  checked={checked}
                                  disabled={!!busy}
                                  onChange={(e) => {
                                    const cur = q.confirmed_tables ?? []
                                    const next = e.target.checked
                                      ? [...cur, t.table]
                                      : cur.filter((x) => x !== t.table)
                                    patch(q.sub_id, { confirmed_tables: next })
                                  }}
                                  style={{ fontSize: 12 }}
                                >
                                  {t.table}
                                  {t.comment ? (
                                    <span style={{ color: '#8c8c8c', fontWeight: 400 }}>（{t.comment}）</span>
                                  ) : null}
                                </Checkbox>
                              )
                            })}
                            {raw.length === 0 ? (
                              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                                未识别到候选表，可编辑问题补充表名后重试
                              </Typography.Text>
                            ) : filtered.length === 0 ? (
                              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                                未找到匹配「{tableSearch[q.sub_id]}」的表
                              </Typography.Text>
                            ) : null}
                          </div>
                        </div>
                        )
                      })() : null}
                    </>
                  )}
                </div>
                <Space size={2}>
                  <Tooltip title="编辑">
                    <Button size="small" type="text" icon={<EditOutlined />} disabled={!!busy} onClick={() => startEdit(q)} />
                  </Tooltip>
                  <Tooltip title="删除">
                    <Button size="small" type="text" danger icon={<DeleteOutlined />} disabled={!!busy}
                      onClick={() => setItems((prev) => prev.filter((x) => x.sub_id !== q.sub_id))} />
                  </Tooltip>
                </Space>
              </div>
            </div>
          )
        })}
      </div>

      <Space style={{ marginTop: 12 }} wrap size={8}>
        <Button size="small" icon={<PlusOutlined />}
          disabled={!!busy}
          onClick={() => setItems((prev) => [...prev, {
            sub_id: `q${prev.length + 1}`, question: '', intent: 'value',
            metrics: [], dimensions: [], filters: [], time: {},
            confidence: 0.3, enabled: true,
          }])}>
          新增子查询
        </Button>
        {onRegen ? (
          <Button size="small" icon={<ReloadOutlined />} disabled={!!busy} onClick={onRegen}>重新拆解</Button>
        ) : null}
        {onCancel ? (
          <Button size="small" disabled={!!busy} onClick={onCancel}>取消</Button>
        ) : null}
        <Button
          type="primary" size="small"
          loading={confirming}
          disabled={enabledCount === 0 || blockedCount > 0 || !!busy || !!confirming}
          onClick={() => onConfirm(items.filter((q) => q.enabled !== false))}
        >
          确认执行（{enabledCount}）
        </Button>
      </Space>
      {blockedCount > 0 ? (
        <Alert
          type="warning" showIcon style={{ marginTop: 8, padding: '2px 10px', fontSize: 12 }}
          message={
            <span style={{ fontSize: 12 }}>
              还有 {blockedCount} 个子查询需要先选择数据表，选择后可执行
            </span>
          }
        />
      ) : null}
    </div>
  )
}
