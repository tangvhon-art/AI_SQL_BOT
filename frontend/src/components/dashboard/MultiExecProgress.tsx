// Phase B 执行进度面板：按 sub_id 实时展示每张卡片状态
// generating/executing/success/error/cancelled/needs_clarify（执行中选表澄清，勾选后重跑）
import { useState } from 'react'
import { Button, Checkbox, Space, Spin, Tag, Typography } from 'antd'
import { RetweetOutlined, StopOutlined, TableOutlined } from '@ant-design/icons'
import { useMultiQueryStore } from '../../stores/multiQuery'
import type { SubCardStatus } from '../../types/chart'

const STATUS_META: Record<SubCardStatus, { color: string; text: string }> = {
  pending: { color: 'default', text: '等待执行' },
  generating: { color: 'processing', text: '生成 SQL…' },
  executing: { color: 'processing', text: '执行中…' },
  success: { color: 'success', text: '已完成' },
  error: { color: 'error', text: '失败' },
  needs_clarify: { color: 'warning', text: '待选表' },
  cancelled: { color: 'default', text: '已取消' },
}

interface Props {
  onRetryCard?: (subId: string, confirmedTables?: string[]) => void
  onCancelAll?: () => void
  cancelLoading?: boolean
  /** 全局 busy：拆解中/确认执行中/重试中/取消中 → 禁用所有操作按钮 */
  busy?: boolean
}

export default function MultiExecProgress({ onRetryCard, onCancelAll, cancelLoading, busy }: Props) {
  const subCards = useMultiQueryStore((s) => s.subCards)
  const cards = Object.values(subCards)

  const done = cards.filter((c) => c.status === 'success').length
  const failed = cards.filter((c) => c.status === 'error').length
  const running = cards.filter((c) => ['pending', 'generating', 'executing'].includes(c.status)).length
  const clarifying = cards.filter((c) => c.status === 'needs_clarify').length

  return (
    <div className="glass-msg-assistant" style={{ margin: '10px 0', padding: '14px 16px', maxWidth: '100%' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
        <Space size={8}>
          <Tag color="purple" style={{ marginInlineEnd: 0 }}>并行执行</Tag>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {running > 0 ? `执行中 ${running} 个` : '执行完成'} · 成功 {done} / 失败 {failed}
            {clarifying > 0 ? ` / 待选表 ${clarifying}` : ''} / 共 {cards.length}
          </Typography.Text>
        </Space>
        {onCancelAll && running > 0 ? (
          <Button size="small" icon={<StopOutlined />} loading={cancelLoading} onClick={onCancelAll}>
            取消全部
          </Button>
        ) : null}
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {cards.map((c) => {
          const meta = STATUS_META[c.status] ?? STATUS_META.pending
          if (c.status === 'needs_clarify') {
            return (
              <ClarifyRow key={c.sub_id} card={c} onConfirm={onRetryCard} />
            )
          }
          return (
            <div
              key={c.sub_id}
              style={{
                display: 'flex', alignItems: 'center', gap: 10,
                border: '1px solid #EDEAFD', borderRadius: 8, padding: '9px 12px',
                background: c.status === 'error' ? '#FFFBFB' : '#FAFAFE',
                opacity: c.status === 'cancelled' ? 0.6 : 1,
              }}
            >
              <span style={{ fontSize: 12, color: '#8c8c8c', width: 28, flexShrink: 0 }}>{c.sub_id}</span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: '#1A1B1C', lineHeight: 1.4, wordBreak: 'break-word' }}>
                  {c.title || c.question || c.sub_id}
                </div>
                {c.status === 'generating' || c.status === 'executing' ? (
                  <Spin size="small" style={{ marginRight: 6 }} />
                ) : null}
                {c.status === 'error' && c.error ? (
                  <div style={{ fontSize: 12, color: '#C0392B', marginTop: 2, wordBreak: 'break-word' }}>
                    {c.error}
                  </div>
                ) : null}
              </div>
              <Tag color={meta.color} style={{ marginInlineEnd: 0, flexShrink: 0 }}>{meta.text}</Tag>
              {c.status === 'error' && c.retryable !== false && onRetryCard ? (
                <Button
                  size="small" icon={<RetweetOutlined />} style={{ flexShrink: 0 }}
                  onClick={() => onRetryCard(c.sub_id)}
                  disabled={busy}
                >
                  重试
                </Button>
              ) : null}
            </div>
          )
        })}
      </div>
    </div>
  )
}

/** 执行中澄清行：候选表勾选 → 确认后带 confirmed_tables 重跑该子查询 */
function ClarifyRow({
  card, onConfirm,
}: {
  card: { sub_id: string; title?: string; question?: string; clarifyCandidates?: Array<{ table: string; comment?: string }> }
  onConfirm?: (subId: string, confirmedTables?: string[]) => void
}) {
  const [checked, setChecked] = useState<string[]>([])
  const candidates = card.clarifyCandidates ?? []
  return (
    <div
      style={{
        border: '1px solid #F5E3C3', borderRadius: 8, padding: '10px 12px', background: '#FFFCF5',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <TableOutlined style={{ color: '#B87A2A' }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: '#1A1B1C', lineHeight: 1.4, wordBreak: 'break-word' }}>
            {card.title || card.question || card.sub_id}
          </div>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            该子查询对应多张表，请勾选需要查询的表（可多选）
          </Typography.Text>
        </div>
        <Tag color="warning" style={{ marginInlineEnd: 0, flexShrink: 0 }}>待选表</Tag>
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center' }}>
        {(candidates ?? []).map((t) => (
          <Checkbox
            key={t.table}
            checked={checked.includes(t.table)}
            onChange={(e) => {
              setChecked((prev) => (e.target.checked
                ? [...prev, t.table]
                : prev.filter((x) => x !== t.table)))
            }}
            style={{ fontSize: 12 }}
          >
            {t.table}
            {t.comment ? <span style={{ color: '#8c8c8c', fontWeight: 400 }}>（{t.comment}）</span> : null}
          </Checkbox>
        ))}
        {(candidates ?? []).length === 0 ? (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>未识别到候选表</Typography.Text>
        ) : null}
        {onConfirm ? (
          <Button
            size="small" type="primary" icon={<RetweetOutlined />}
            disabled={checked.length === 0}
            onClick={() => onConfirm(card.sub_id, checked)}
          >
            确认表并重跑
          </Button>
        ) : null}
      </div>
    </div>
  )
}
