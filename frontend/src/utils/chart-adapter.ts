/**
 * 图表数据适配器：将后端原始查询结果转换为统一 ChartDataset
 * 所有图表组件只接受 ChartDataset，不直接处理后端原始格式
 */
import type { ChartDataset } from '../types/chart';

/**
 * 后端查询结果格式：{ columns: string[], rows: any[][], row_count?: number }
 * 转换为 ChartDataset：第一列作为维度，其余作为指标
 */
export function adaptQueryResult(data: {
  columns: string[];
  rows: any[][];
}): ChartDataset {
  if (!data?.columns?.length) {
    return { dimensions: [], metrics: [], rows: [] };
  }
  const columns = data.columns;
  const rows = (data.rows || []).map((row) => {
    const obj: Record<string, any> = {};
    columns.forEach((col, i) => {
      obj[col] = row[i];
    });
    return obj;
  });
  // 第一列作为维度，其余作为指标
  const dimensions = [columns[0]];
  const metrics = columns.slice(1);
  return { dimensions, metrics, rows };
}

/**
 * 多指标时自动识别维度列：含时间/日期关键词的列优先作为维度
 */
export function autoDetectDimensions(columns: string[]): { dimensions: string[]; metrics: string[] } {
  const timeKeywords = ['时间', '日期', '月', '日', '年', '周', '季度', 'date', 'time', 'month', 'day', 'year'];
  const dimCol = columns.find((c) => timeKeywords.some((k) => c.toLowerCase().includes(k)));
  if (dimCol) {
    return { dimensions: [dimCol], metrics: columns.filter((c) => c !== dimCol) };
  }
  return { dimensions: [columns[0]], metrics: columns.slice(1) };
}

/**
 * 从 ChartDataset 提取 ECharts xAxis 数据
 */
export function extractXData(dataset: ChartDataset): any[] {
  const dim = dataset.dimensions[0];
  return dataset.rows.map((r) => r[dim]);
}

/**
 * 从 ChartDataset 提取 ECharts series 数据
 */
export function extractSeriesData(dataset: ChartDataset, metric: string): any[] {
  return dataset.rows.map((r) => r[metric]);
}

/**
 * 格式化数值：千分位 + 保留2位小数
 */
export function formatNumber(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '-';
  const num = typeof value === 'string' ? parseFloat(value) : value;
  if (isNaN(num)) return String(value);
  if (Math.abs(num) >= 100000000) return (num / 100000000).toFixed(2) + '亿';
  if (Math.abs(num) >= 10000) return (num / 10000).toFixed(2) + '万';
  return num.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
}
