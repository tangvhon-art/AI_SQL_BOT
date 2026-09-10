/**
 * 图表卡片：统一 ECharts 渲染 + 公共交互
 * 支持所有已注册图表类型，表格类型走 antd Table 兜底
 * rank 排行榜多指标时支持切换指标列（如 数量 / 占比），让占比也能上图
 */
import React, { useEffect, useRef, useState } from 'react';
import { Select, Table, Empty, Spin } from 'antd';
import { useChartInteraction } from '../../hooks/useChartInteraction';
import { buildChartOption } from '../../utils/chart-factory';
import { CardWrapper } from './CardWrapper';
import type { ChartCardConfig } from '../../types/chart';

interface ChartCardProps {
  config: ChartCardConfig;
  height?: number;
}

export const ChartCard: React.FC<ChartCardProps> = ({ config, height = 300 }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  // rank 多指标时当前展示的指标（null = 默认第一个指标）
  const [rankMetric, setRankMetric] = useState<string | null>(null);
  const { initChart, setOption } = useChartInteraction({
    containerRef,
    enableDataZoom: config.dataset.rows.length > 15,
  });

  // 新查询结果到来时重置指标选择
  useEffect(() => {
    setRankMetric(null);
  }, [config]);

  useEffect(() => {
    if (config.status === 'error' || config.chartType === 'table') return;
    initChart();
    let optConfig = config;
    // rank 且多指标 + 用户已选 → 用所选指标重建数据集（rows 按指标名取值）
    if (config.chartType === 'rank' && config.dataset.metrics.length > 1 && rankMetric) {
      optConfig = { ...config, dataset: { ...config.dataset, metrics: [rankMetric] } };
    }
    const option = buildChartOption(optConfig.chartType, optConfig.dataset, optConfig.custom);
    if (option?.__table__) return;
    setOption(option);
  }, [config, rankMetric, initChart, setOption]);

  // 错误状态
  if (config.status === 'error') {
    return (
      <CardWrapper title={config.title} sql={config.sql} height={height}>
        <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <Empty description={config.error || '查询失败'} />
        </div>
      </CardWrapper>
    );
  }

  // 加载中
  if (config.status === 'loading') {
    return (
      <CardWrapper title={config.title} height={height}>
        <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <Spin />
        </div>
      </CardWrapper>
    );
  }

  // 空数据
  if (!config.dataset.rows.length) {
    return (
      <CardWrapper title={config.title} sql={config.sql} height={height}>
        <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <Empty description="无数据" />
        </div>
      </CardWrapper>
    );
  }

  // 表格兜底
  if (config.chartType === 'table') {
    const columns = [...config.dataset.dimensions, ...config.dataset.metrics].map((c) => ({
      title: c, dataIndex: c, key: c, ellipsis: true,
    }));
    return (
      <CardWrapper title={config.title} sql={config.sql} height={height}>
        <Table
          size="small"
          columns={columns}
          dataSource={config.dataset.rows.map((r, i) => ({ ...r, key: i }))}
          pagination={{ pageSize: 5, size: 'small' }}
          scroll={{ y: height - 80 }}
        />
      </CardWrapper>
    );
  }

  // ECharts 渲染（rank 多指标时顶部提供指标切换）
  const showMetricSwitch = config.chartType === 'rank' && config.dataset.metrics.length > 1;
  return (
    <CardWrapper title={config.title} sql={config.sql} height={height}>
      <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
        {showMetricSwitch && (
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 4, flexShrink: 0 }}>
            <Select
              size="small"
              style={{ width: 110 }}
              value={rankMetric ?? config.dataset.metrics[0]}
              onChange={setRankMetric}
              options={config.dataset.metrics.map((m) => ({ label: m, value: m }))}
            />
          </div>
        )}
        <div ref={containerRef} style={{ width: '100%', flex: 1, minHeight: 0 }} />
      </div>
    </CardWrapper>
  );
};
