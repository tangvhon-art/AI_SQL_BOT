/**
 * KPI 指标卡片：展示单个关键指标
 * 参考后台管理 Dashboard 风格：白底圆角、左侧色条、大数字
 */
import React from 'react';
import { formatNumber } from '../../utils/chart-adapter';
import type { ChartDataset } from '../../types/chart';

const KPI_COLORS = ['#6C5CE7', '#10B981', '#F97316', '#F59E0B', '#EC4899', '#14B8A6'];

interface KpiCardProps {
  title: string;
  dataset: ChartDataset;
  unit?: string;
  colorIndex?: number;
}

export const KpiCard: React.FC<KpiCardProps> = ({ title, dataset, unit, colorIndex = 0 }) => {
  const metric = dataset.metrics[0];
  const value = dataset.rows[0]?.[metric];
  const displayValue = value !== undefined && value !== null ? formatNumber(value) : '-';
  const color = KPI_COLORS[colorIndex % KPI_COLORS.length];

  return (
    <div style={{
      background: '#fff',
      borderRadius: 12,
      padding: '20px 24px',
      boxShadow: '0 1px 2px rgba(15,35,34,.04), 0 6px 16px -4px rgba(15,35,34,.06), 0 20px 40px -16px rgba(15,35,34,.10)',
      border: '1px solid rgba(30,35,60,.08)',
      display: 'flex',
      flexDirection: 'column',
      justifyContent: 'center',
      minHeight: 120,
    }}>
      <div style={{
        fontSize: 13,
        color: '#8c8c8c',
        marginBottom: 10,
        fontWeight: 400,
        lineHeight: 1.4,
      }}>{title}</div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
        <div style={{
          width: 4,
          height: 32,
          background: color,
          borderRadius: 2,
          marginRight: 8,
        }} />
        <span style={{
          fontSize: 32,
          fontWeight: 700,
          color: '#1f1f1f',
          lineHeight: 1,
          letterSpacing: '-0.5px',
        }}>{displayValue}</span>
        {unit && <span style={{ fontSize: 14, color: '#8c8c8c', fontWeight: 400 }}>{unit}</span>}
      </div>
    </div>
  );
};

/**
 * KPI 组合卡片：一行展示多个指标
 */
interface KpiGroupCardProps {
  title: string;
  dataset: ChartDataset;
}

export const KpiGroupCard: React.FC<KpiGroupCardProps> = ({ title, dataset }) => {
  const row = dataset.rows[0] || {};
  const items = dataset.metrics.map((metric, i) => ({
    label: metric,
    value: row[metric],
    color: KPI_COLORS[i % KPI_COLORS.length],
  }));

  return (
    <div style={{
      background: '#fff',
      borderRadius: 12,
      padding: '20px 24px',
      boxShadow: '0 1px 2px rgba(15,35,34,.04), 0 6px 16px -4px rgba(15,35,34,.06), 0 20px 40px -16px rgba(15,35,34,.10)',
      border: '1px solid rgba(30,35,60,.08)',
    }}>
      <div style={{ fontSize: 14, color: '#262626', fontWeight: 600, marginBottom: 16 }}>{title}</div>
      <div style={{
        display: 'grid',
        gridTemplateColumns: `repeat(${Math.min(items.length, 4)}, 1fr)`,
        gap: 20,
      }}>
        {items.map((item, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <div style={{
              width: 4,
              height: 36,
              background: item.color,
              borderRadius: 2,
              flexShrink: 0,
            }} />
            <div>
              <div style={{ fontSize: 12, color: '#8c8c8c', marginBottom: 4 }}>{item.label}</div>
              <div style={{ fontSize: 24, fontWeight: 700, color: '#1f1f1f', lineHeight: 1 }}>
                {item.value !== undefined && item.value !== null ? formatNumber(item.value) : '-'}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};
