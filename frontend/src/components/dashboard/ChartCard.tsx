/**
 * 图表卡片：统一 ECharts 渲染 + 公共交互
 * 支持所有已注册图表类型，表格类型走 antd Table 兜底
 */
import React, { useEffect, useRef } from 'react';
import { Table, Empty, Spin } from 'antd';
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
  const { initChart, setOption } = useChartInteraction({
    containerRef,
    enableDataZoom: config.dataset.rows.length > 15,
  });

  useEffect(() => {
    if (config.status === 'error' || config.chartType === 'table') return;
    initChart();
    const option = buildChartOption(config.chartType, config.dataset, config.custom);
    if (option?.__table__) return;
    setOption(option);
  }, [config, initChart, setOption]);

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

  // ECharts 渲染
  return (
    <CardWrapper title={config.title} sql={config.sql} height={height}>
      <div ref={containerRef} style={{ width: '100%', height: '100%' }} />
    </CardWrapper>
  );
};
