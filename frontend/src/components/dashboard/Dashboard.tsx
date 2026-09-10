/**
 * Dashboard 容器：多查询结果汇总展示
 * 12栅格自动布局，支持 KPI 卡片 + 图表卡片混合
 * 智能图表类型推荐：根据数据特征自动选择 KPI/折线/柱状/分组柱状
 */
import React from 'react';
import { Row, Col, Button, Space, Tooltip } from 'antd';
import { SaveOutlined, ReloadOutlined } from '@ant-design/icons';
import { ChartCard } from './ChartCard';
import { KpiCard, KpiGroupCard } from './KpiCard';
import { recommendLayout } from '../../utils/layout-engine';
import { adaptQueryResult } from '../../utils/chart-adapter';
import type { DashboardData, ChartCardConfig, ChartType } from '../../types/chart';

interface DashboardProps {
  data: DashboardData;
  onSaveReport?: () => void;
  onRefresh?: () => void;
  showToolbar?: boolean;
}

/**
 * 智能图表类型推荐：根据数据特征自动选择
 * - 1行1列（单值）→ kpi
 * - 1行多列 → kpi_group
 * - 多行且第一列含时间关键词 → line
 * - 多行1列 → bar（排行榜风格）
 * - 多行多列 → group_bar
 */
function recommendChartType(card: ChartCardConfig): ChartType {
  const { dataset } = card;
  const rowCount = dataset.rows.length;
  const colCount = dataset.dimensions.length + dataset.metrics.length;

  // 空数据保持原类型
  if (rowCount === 0) return card.chartType;

  // 单值 → KPI
  if (rowCount === 1 && dataset.metrics.length === 1) return 'kpi';

  // 单行多指标 → KPI 组
  if (rowCount === 1 && dataset.metrics.length > 1) return 'kpi_group';

  // 多行：检查第一列是否含时间关键词
  const timeKeywords = ['时间', '日期', '月', '日', '年', '周', '季度', 'date', 'time', 'month', 'day', 'year'];
  const firstDim = dataset.dimensions[0]?.toLowerCase() || '';
  const isTimeSeries = timeKeywords.some((k) => firstDim.includes(k));

  if (isTimeSeries && dataset.metrics.length === 1) return 'line';
  if (isTimeSeries && dataset.metrics.length > 1) return 'line';

  // 多行多指标 → 分组柱状
  if (dataset.metrics.length > 1) return 'group_bar';

  // 多行单指标 → 柱状图（排行榜风格）
  return 'bar';
}

/**
 * 将后端 dashboard 事件转换为 DashboardData，并智能推荐图表类型
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
      status: c.status === 'error' ? 'error' : c.data ? 'success' : 'loading',
      error: c.error,
    };
    // 智能推荐图表类型（覆盖后端默认的 bar）
    baseCard.chartType = recommendChartType(baseCard);
    return baseCard;
  });
  return {
    originalQuestion: event.original_question || '',
    layout: event.layout || recommendLayout(cards),
    cards,
  };
}

export const Dashboard: React.FC<DashboardProps> = ({ data, onSaveReport, onRefresh, showToolbar = true }) => {
  const layout = data.layout?.length ? data.layout : recommendLayout(data.cards);

  // 分离 KPI 卡片和图表卡片
  const kpiCards = data.cards.filter((c) => c.chartType === 'kpi' || c.chartType === 'kpi_group');
  const chartCards = data.cards.filter((c) => c.chartType !== 'kpi' && c.chartType !== 'kpi_group');

  const renderCard = (card: ChartCardConfig, i: number) => {
    const span = layout[i]?.span || 12;
    if (card.chartType === 'kpi') {
      return (
        <Col key={card.id} span={span} xs={24} sm={12} md={span}>
          <KpiCard title={card.title} dataset={card.dataset} />
        </Col>
      );
    }
    if (card.chartType === 'kpi_group') {
      return (
        <Col key={card.id} span={24}>
          <KpiGroupCard title={card.title} dataset={card.dataset} />
        </Col>
      );
    }
    return (
      <Col key={card.id} span={span} xs={24} sm={24} md={span}>
        <ChartCard config={card} height={320} />
      </Col>
    );
  };

  return (
    <div style={{ width: '100%' }}>
      {showToolbar && (
        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          marginBottom: 16, padding: '10px 16px',
          background: '#f7f8fa', borderRadius: 10,
        }}>
          <span style={{ fontSize: 13, color: '#595959', fontWeight: 500 }}>
            分析结果 · 共 {data.cards.length} 个维度
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

      {/* KPI 卡片区域（顶部） */}
      {kpiCards.length > 0 && (
        <Row gutter={[16, 16]} style={{ marginBottom: chartCards.length > 0 ? 16 : 0 }}>
          {kpiCards.map((card, i) => renderCard(card, i))}
        </Row>
      )}

      {/* 图表卡片区域（下方） */}
      {chartCards.length > 0 && (
        <Row gutter={[16, 16]}>
          {chartCards.map((card, i) => renderCard(card, kpiCards.length + i))}
        </Row>
      )}
    </div>
  );
};
