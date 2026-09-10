/**
 * Dashboard 容器：多查询结果汇总展示（V2：支持卡片执行中/错误/取消态 + 单卡重试 + 解读/异常/溯源）
 * 12栅格自动布局，支持 KPI 卡片 + 图表卡片混合
 */
import React, { useState } from 'react';
import { Row, Col, Button, Space, Tooltip, Spin, Alert, Tag, Collapse, Typography, Descriptions, Checkbox } from 'antd';
import { SaveOutlined, ReloadOutlined, RetweetOutlined, TableOutlined } from '@ant-design/icons';
import { ChartCard } from './ChartCard';
import { KpiCard, KpiGroupCard } from './KpiCard';
import { recommendLayout } from '../../utils/layout-engine';
import { adaptQueryResult, formatNumber } from '../../utils/chart-adapter';
import type { DashboardData, ChartCardConfig, ChartType } from '../../types/chart';

const KPI_COLORS = ['#6C5CE7', '#10B981', '#F97316', '#F59E0B', '#EC4899', '#14B8A6'];

interface DashboardProps {
  data: DashboardData;
  onSaveReport?: () => void;
  onRefresh?: () => void;
  showToolbar?: boolean;
  /** 单卡重试（error 卡片）；澄清卡勾选表后带 confirmed_tables 重跑 */
  onRetryCard?: (subId: string, confirmedTables?: string[]) => void;
  /** 重试防抖：返回当前是否可重试 */
  retryDisabled?: (subId: string) => boolean;
  /** 全局 busy：拆解中/确认执行中/重试中/取消中 → 禁用所有操作按钮 */
  busy?: boolean;
}

/**
 * 智能图表类型推荐：根据数据特征自动选择
 */
function recommendChartType(card: ChartCardConfig): ChartType {
  const { dataset } = card;
  const rowCount = dataset.rows.length;
  const colCount = dataset.dimensions.length + dataset.metrics.length;

  if (rowCount === 0) return card.chartType;
  if (rowCount === 1 && dataset.metrics.length === 1) return 'kpi';
  if (rowCount === 1 && dataset.metrics.length > 1) return 'kpi_group';

  const timeKeywords = ['时间', '日期', '月', '日', '年', '周', '季度', 'date', 'time', 'month', 'day', 'year'];
  const firstDim = dataset.dimensions[0]?.toLowerCase() || '';
  const isTimeSeries = timeKeywords.some((k) => firstDim.includes(k));
  if (isTimeSeries && dataset.metrics.length >= 1) return 'line';
  if (dataset.metrics.length > 1) return 'group_bar';
  return 'bar';
}

/**
 * 将后端 dashboard 事件转换为 DashboardData（V2：完整状态映射 + 解读/异常/溯源）
 */
export function convertDashboardEvent(event: any): DashboardData {
  const cards: ChartCardConfig[] = (event.cards || []).map((c: any, i: number) => {
    const dataset = c.data ? adaptQueryResult(c.data) : { dimensions: [], metrics: [], rows: [] };
    const baseCard: ChartCardConfig = {
      id: c.sub_id || `card-${i}`,
      title: c.title || `子查询 ${i + 1}`,
      chartType: (c.chart_type || 'bar') as ChartType,
      dataset,
      sql: c.sql,
      subId: c.sub_id,
      status: (c.status || (c.data ? 'success' : 'loading')) as ChartCardConfig['status'],
      error: c.error,
      retryable: c.retryable,
      clarifyCandidates: c.clarify_candidates,
      facts: c.facts,
      anomalies: c.anomalies,
      trace: c.trace,
      interpretation: c.interpretation,
    };
    // 仅成功卡片做智能图表类型修正（错误/取消卡无数据）
    if (baseCard.status === 'success' || baseCard.status === 'loading') {
      baseCard.chartType = recommendChartType(baseCard);
    }
    return baseCard;
  });
  return {
    originalQuestion: event.original_question || '',
    layout: event.layout || recommendLayout(cards),
    cards,
  };
}

/** 卡片级解读 + 异常 + 溯源（per-card 信息密度） */
function CardInsight({ card }: { card: ChartCardConfig }) {
  if (!card.interpretation && !card.anomalies?.length && !card.trace) return null
  const anomalies = card.anomalies ?? []
  const trace = card.trace ?? {}
  const tables = (trace.tables ?? []) as string[]
  return (
    <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 6 }}>
      {card.interpretation ? (
        <div style={{ fontSize: 13, color: 'rgba(0,0,0,.72)', lineHeight: 1.6 }}>{card.interpretation}</div>
      ) : null}
      {anomalies.map((a, i) => (
        <Alert key={i} type="warning" showIcon message={a.desc} style={{ padding: '3px 10px', fontSize: 12 }} />
      ))}
      {Object.keys(trace).length ? (
        <Collapse
          size="small" ghost
          style={{ background: 'transparent' }}
          items={[{
            key: 'trace',
            label: (
              <Space size={6}>
                <Tag color="cyan" style={{ marginInlineEnd: 0, fontSize: 11 }}>口径与溯源</Tag>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {tables.join('、') || '数据来源'} · 耗时 {String(trace.latency_ms ?? '-')}ms
                </Typography.Text>
              </Space>
            ),
            children: (
              <Descriptions size="small" column={1} bordered
                items={[
                  { key: 'tables', label: '数据源表', children: tables.join('、') || '—' },
                  { key: 'sql', label: 'SQL', children: <span style={{ fontFamily: 'monospace', fontSize: 11, wordBreak: 'break-all' }}>{String(trace.sql ?? '—')}</span> },
                  { key: 'rows', label: '行数', children: String(trace.row_count ?? '—') },
                  { key: 'latency', label: '耗时', children: `${String(trace.latency_ms ?? '-')} ms` },
                ]}
              />
            ),
          }]}
        />
      ) : null}
    </div>
  )
}

export const Dashboard: React.FC<DashboardProps> = ({
  data, onSaveReport, onRefresh, showToolbar = true, onRetryCard, retryDisabled, busy,
}) => {
  const layout = data.layout?.length ? data.layout : recommendLayout(data.cards);

  const kpiCards = data.cards.filter((c) => c.chartType === 'kpi' || c.chartType === 'kpi_group');
  const chartCards = data.cards.filter((c) => c.chartType !== 'kpi' && c.chartType !== 'kpi_group');

  /** 非成功卡片的统一占位块（loading / error / cancelled） */
  const renderStateCard = (card: ChartCardConfig) => {
    const status = card.status ?? 'loading'
    if (status === 'error') {
      return (
        <Col key={card.id} span={24}>
          <div style={{
            border: '1px solid #FDECEC', background: '#FFFBFB', borderRadius: 12, padding: '14px 18px',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 13.5, fontWeight: 600, marginBottom: 4 }}>{card.title}</div>
                <Alert type="error" showIcon message={card.error || '执行失败'} style={{ fontSize: 12 }} />
              </div>
              {onRetryCard && card.subId && card.retryable !== false ? (
                <Button
                  size="small" icon={<RetweetOutlined />}
                  disabled={retryDisabled?.(card.subId)}
                  onClick={() => onRetryCard(card.subId!)}
                >
                  重试
                </Button>
              ) : null}
            </div>
          </div>
        </Col>
      )
    }
    if (status === 'cancelled') {
      return (
        <Col key={card.id} span={24}>
          <div style={{
            border: '1px dashed #E5E7EB', background: '#FAFAFA', borderRadius: 12, padding: '14px 18px',
            color: '#9CA3AF',
          }}>
            <div style={{ fontSize: 13, fontWeight: 600 }}>{card.title}</div>
            <div style={{ fontSize: 12, marginTop: 2 }}>已取消</div>
          </div>
        </Col>
      )
    }
    if (status === 'needs_clarify') {
      const candidates = card.clarifyCandidates ?? []
      return (
        <Col key={card.id} span={24}>
          <div style={{
            border: '1px solid #F5E3C3', background: '#FFFCF5', borderRadius: 12, padding: '14px 18px',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
              <TableOutlined style={{ color: '#B87A2A' }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13.5, fontWeight: 600, marginBottom: 2 }}>{card.title}</div>
                <div style={{ fontSize: 12, color: '#8c8c8c' }}>
                  该子查询对应多张表，请勾选需要查询的表（可多选）后重新执行
                </div>
              </div>
              <Tag color="warning" style={{ marginInlineEnd: 0 }}>待选表</Tag>
            </div>
            <ClarifyCheckboxGroup card={card} onRetry={onRetryCard} retryDisabled={retryDisabled} busy={busy} />
            {candidates.length === 0 ? (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>未识别到候选表</Typography.Text>
            ) : null}
          </div>
        </Col>
      )
    }
    // pending / generating / executing / loading
    return (
      <Col key={card.id} span={24}>
        <div style={{
          border: '1px solid #EDEAFD', background: '#FAFAFE', borderRadius: 12, padding: '16px 18px',
          display: 'flex', alignItems: 'center', gap: 12,
        }}>
          <Spin size="small" />
          <div>
            <div style={{ fontSize: 13.5, fontWeight: 600 }}>{card.title || '子查询'}</div>
            <div style={{ fontSize: 12, color: '#8c8c8c' }}>
              {status === 'generating' ? '正在生成 SQL…' : status === 'executing' ? '正在执行…' : '等待执行…'}
            </div>
          </div>
        </div>
      </Col>
    )
  }

  const renderCard = (card: ChartCardConfig) => {
    if (card.status && card.status !== 'success' && card.status !== 'loading') {
      return renderStateCard(card)
    }
    // kpi_group 本身就是大卡片，占整行
    if (card.chartType === 'kpi_group') {
      return (
        <Col key={card.id} span={24}>
          <KpiGroupCard title={card.title} dataset={card.dataset} />
          <CardInsight card={card} />
        </Col>
      );
    }
    return (
      <Col key={card.id} span={24}>
        <ChartCard config={card} height={320} />
        <CardInsight card={card} />
      </Col>
    );
  };

  const singleKpis = kpiCards.filter((c) => c.chartType === 'kpi');
  const kpiGroupCards = kpiCards.filter((c) => c.chartType === 'kpi_group');

  return (
    <div style={{ width: '100%' }}>
      {showToolbar && (
        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          marginBottom: 16, padding: '10px 16px',
          background: '#f7f8fa', borderRadius: 10,
        }}>
          <span style={{ fontSize: 13, color: '#595959', fontWeight: 500 }}>
            分析结果 · 共 {data.cards.length} 个子查询
          </span>
          <Space size={8}>
            {onRefresh && (
              <Tooltip title="重新生成">
                <Button size="small" icon={<ReloadOutlined />} onClick={onRefresh} />
              </Tooltip>
            )}
            {onSaveReport && (
              <Button size="small" type="primary" icon={<SaveOutlined />} onClick={onSaveReport}>
                保存为报告
              </Button>
            )}
          </Space>
        </div>
      )}

      {/* KPI 指标区域：单个 KPI 合并为一个大卡片 */}
      {singleKpis.length > 0 && (
        <div style={{
          background: '#fff',
          borderRadius: 12,
          padding: '20px 24px',
          boxShadow: '0 1px 2px rgba(15,35,34,.04), 0 6px 16px -4px rgba(15,35,34,.06), 0 20px 40px -16px rgba(15,35,34,.10)',
          border: '1px solid rgba(30,35,60,.08)',
          marginBottom: (kpiGroupCards.length > 0 || chartCards.length > 0) ? 16 : 0,
        }}>
          <div style={{ fontSize: 14, color: '#262626', fontWeight: 600, marginBottom: 16 }}>核心指标</div>
          <div style={{
            display: 'grid',
            gridTemplateColumns: `repeat(${Math.min(singleKpis.length, 4)}, 1fr)`,
            gap: 20,
          }}>
            {singleKpis.map((card, i) => {
              const metric = card.dataset.metrics[0];
              const value = card.dataset.rows[0]?.[metric];
              const color = KPI_COLORS[i % KPI_COLORS.length];
              return (
                <div key={card.id} style={{ display: 'flex', alignItems: 'center', gap: 12, minWidth: 0 }}>
                  <div style={{ width: 4, height: 36, background: color, borderRadius: 2, flexShrink: 0 }} />
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: 12, color: '#8c8c8c', marginBottom: 4, lineHeight: 1.4 }}>{card.title}</div>
                    <div style={{ fontSize: 24, fontWeight: 700, color: '#1f1f1f', lineHeight: 1, letterSpacing: '-0.5px' }}>
                      {value !== undefined && value !== null ? formatNumber(value) : '-'}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* KPI 组卡片 */}
      {kpiGroupCards.length > 0 && (
        <Row gutter={[16, 16]} style={{ marginBottom: chartCards.length > 0 ? 16 : 0 }}>
          {kpiGroupCards.map((card) => renderCard(card))}
        </Row>
      )}

      {/* 图表卡片区域 */}
      {chartCards.length > 0 && (
        <Row gutter={[16, 16]}>
          {chartCards.map((card) => renderCard(card))}
        </Row>
      )}
    </div>
  );
};

/** 执行中澄清：候选表勾选 → 确认后带 confirmed_tables 重跑该子查询 */
function ClarifyCheckboxGroup({
  card, onRetry, retryDisabled, busy,
}: {
  card: ChartCardConfig
  onRetry?: (subId: string, confirmedTables?: string[]) => void
  retryDisabled?: (subId: string) => boolean
  busy?: boolean
}) {
  const [checked, setChecked] = useState<string[]>([])
  const candidates = card.clarifyCandidates ?? []
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'center' }}>
      {candidates.map((t) => (
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
      {onRetry && card.subId ? (
        <Button
          size="small" type="primary" icon={<RetweetOutlined />}
          disabled={checked.length === 0 || retryDisabled?.(card.subId)}
          onClick={() => onRetry(card.subId!, checked)}
        >
          确认表并重跑
        </Button>
      ) : null}
    </div>
  )
}
