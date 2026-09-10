/**
 * 图表工厂：注册式，根据 ChartType 生成 ECharts option
 * 新增图表类型只需实现 buildXxxOption 并注册
 */
import type { ChartDataset, ChartType } from '../types/chart';
import { extractXData, extractSeriesData } from './chart-adapter';

type OptionBuilder = (dataset: ChartDataset, custom?: Record<string, any>) => any;

const registry = new Map<ChartType, OptionBuilder>();

/** 公共配色（海外版：紫靛主色 + 语义伙伴色） */
const COLORS = ['#6C5CE7', '#10B981', '#F97316', '#F59E0B', '#3B82F6', '#14B8A6', '#EC4899', '#A78BFA'];

/** 公共 tooltip */
const baseTooltip = { trigger: 'axis', axisPointer: { type: 'shadow' } };

/** 公共 grid */
const baseGrid = { left: '3%', right: '4%', bottom: '3%', containLabel: true };

// ---------- 柱状图 ----------
function buildBarOption(dataset: ChartDataset, _custom?: Record<string, any>) {
  const metric = dataset.metrics[0];
  return {
    color: COLORS,
    tooltip: baseTooltip,
    grid: baseGrid,
    xAxis: { type: 'category', data: extractXData(dataset), axisLabel: { rotate: dataset.rows.length > 8 ? 30 : 0 } },
    yAxis: { type: 'value' },
    series: [{ type: 'bar', name: metric, data: extractSeriesData(dataset, metric), barMaxWidth: 40 }],
  };
}

// ---------- 折线图 ----------
function buildLineOption(dataset: ChartDataset) {
  const metric = dataset.metrics[0];
  return {
    color: COLORS,
    tooltip: { trigger: 'axis' },
    grid: baseGrid,
    xAxis: { type: 'category', data: extractXData(dataset), boundaryGap: false },
    yAxis: { type: 'value' },
    series: [{ type: 'line', name: metric, data: extractSeriesData(dataset, metric), smooth: true, areaStyle: { opacity: 0.1 } }],
  };
}

// ---------- 分组柱状图 ----------
function buildGroupBarOption(dataset: ChartDataset) {
  return {
    color: COLORS,
    tooltip: baseTooltip,
    legend: { top: 0 },
    grid: { ...baseGrid, top: 40 },
    xAxis: { type: 'category', data: extractXData(dataset) },
    yAxis: { type: 'value' },
    series: dataset.metrics.map((m) => ({ type: 'bar', name: m, data: extractSeriesData(dataset, m), barMaxWidth: 30 })),
  };
}

// ---------- 雷达图 ----------
function buildRadarOption(dataset: ChartDataset) {
  const dim = dataset.dimensions[0];
  const indicators = dataset.rows.map((r) => ({ name: String(r[dim]), max: Math.max(...dataset.metrics.flatMap((m) => dataset.rows.map((row) => Number(row[m]) || 0))) * 1.2 || 100 }));
  return {
    color: COLORS,
    tooltip: {},
    legend: { top: 0 },
    radar: { indicator: indicators, radius: '60%' },
    series: [{
      type: 'radar',
      data: dataset.metrics.map((m, i) => ({
        name: m,
        value: dataset.rows.map((r) => Number(r[m]) || 0),
        areaStyle: { opacity: 0.2 },
      })),
    }],
  };
}

// ---------- 堆叠柱状图 ----------
function buildStackBarOption(dataset: ChartDataset) {
  return {
    color: COLORS,
    tooltip: baseTooltip,
    legend: { top: 0 },
    grid: { ...baseGrid, top: 40 },
    xAxis: { type: 'category', data: extractXData(dataset) },
    yAxis: { type: 'value' },
    series: dataset.metrics.map((m) => ({ type: 'bar', name: m, stack: 'total', data: extractSeriesData(dataset, m) })),
  };
}

// ---------- 双Y轴柱线组合图 ----------
function buildComboOption(dataset: ChartDataset) {
  const [barMetric, lineMetric] = dataset.metrics;
  return {
    color: COLORS,
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    legend: { top: 0 },
    grid: { ...baseGrid, top: 40 },
    xAxis: { type: 'category', data: extractXData(dataset) },
    yAxis: [
      { type: 'value', name: barMetric || '左轴' },
      { type: 'value', name: lineMetric || '右轴' },
    ],
    series: [
      { type: 'bar', name: barMetric, data: extractSeriesData(dataset, barMetric), yAxisIndex: 0 },
      { type: 'line', name: lineMetric, data: extractSeriesData(dataset, lineMetric), yAxisIndex: 1, smooth: true },
    ],
  };
}

// ---------- 排行榜 ----------
function buildRankOption(dataset: ChartDataset) {
  const metric = dataset.metrics[0];
  const dim = dataset.dimensions[0];
  const sorted = [...dataset.rows].sort((a, b) => Number(b[metric]) - Number(a[metric])).slice(0, 10);
  return {
    color: ['#6C5CE7'],
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: baseGrid,
    xAxis: { type: 'value' },
    yAxis: { type: 'category', data: sorted.map((r) => String(r[dim])).reverse(), inverse: false },
    series: [{ type: 'bar', name: metric, data: sorted.map((r) => Number(r[metric])).reverse(), barMaxWidth: 20, label: { show: true, position: 'right' } }],
  };
}

// ---------- 饼图 ----------
function buildPieOption(dataset: ChartDataset) {
  const metric = dataset.metrics[0];
  const dim = dataset.dimensions[0];
  return {
    color: COLORS,
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: {
      orient: 'horizontal',
      bottom: 4,
      left: 'center',
      itemWidth: 12,
      itemHeight: 12,
      textStyle: { fontSize: 12, color: '#595959' },
    },
    series: [{
      type: 'pie',
      radius: ['26%', '45%'],
      center: ['50%', '45%'],
      avoidLabelOverlap: true,
      itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
      label: { show: false, position: 'center' },
      emphasis: { label: { show: true, fontSize: 14, fontWeight: 'bold' } },
      labelLine: { show: false },
      data: dataset.rows.map((r) => ({ name: String(r[dim]), value: Number(r[metric]) || 0 })),
    }],
  };
}

// ---------- 面积图 ----------
function buildAreaOption(dataset: ChartDataset) {
  const metric = dataset.metrics[0];
  return {
    color: COLORS,
    tooltip: { trigger: 'axis' },
    grid: baseGrid,
    xAxis: { type: 'category', data: extractXData(dataset), boundaryGap: false },
    yAxis: { type: 'value' },
    series: [{ type: 'line', name: metric, data: extractSeriesData(dataset, metric), areaStyle: { opacity: 0.3 }, smooth: true }],
  };
}

// ---------- 散点图 ----------
function buildScatterOption(dataset: ChartDataset) {
  const [xMetric, yMetric] = dataset.metrics;
  return {
    color: COLORS,
    tooltip: { trigger: 'item' },
    grid: baseGrid,
    xAxis: { type: 'value', name: xMetric },
    yAxis: { type: 'value', name: yMetric },
    series: [{ type: 'scatter', data: dataset.rows.map((r) => [Number(r[xMetric]), Number(r[yMetric])]), symbolSize: 12 }],
  };
}

// ---------- 热力图 ----------
function buildHeatmapOption(dataset: ChartDataset) {
  const [xDim, yDim] = dataset.dimensions;
  const metric = dataset.metrics[0];
  const xData = [...new Set(dataset.rows.map((r) => String(r[xDim])))];
  const yData = [...new Set(dataset.rows.map((r) => String(r[yDim])))];
  const data = dataset.rows.map((r) => [xData.indexOf(String(r[xDim])), yData.indexOf(String(r[yDim])), Number(r[metric]) || 0]);
  return {
    tooltip: { position: 'top' },
    grid: { ...baseGrid, top: 40 },
    xAxis: { type: 'category', data: xData, splitArea: { show: true } },
    yAxis: { type: 'category', data: yData, splitArea: { show: true } },
    visualMap: { min: 0, max: Math.max(...data.map((d) => d[2])) || 100, calculable: true, orient: 'horizontal', left: 'center', bottom: '0%' },
    series: [{ type: 'heatmap', data, label: { show: true }, emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.5)' } } }],
  };
}

// ---------- 仪表盘 ----------
function buildGaugeOption(dataset: ChartDataset) {
  const metric = dataset.metrics[0];
  const value = Number(dataset.rows[0]?.[metric]) || 0;
  return {
    series: [{
      type: 'gauge',
      progress: { show: true, width: 18 },
      axisLine: { lineStyle: { width: 18 } },
      axisTick: { show: false },
      splitLine: { length: 15, lineStyle: { width: 2, color: '#999' } },
      axisLabel: { distance: 25, color: '#999', fontSize: 12 },
      anchor: { show: true, showAbove: true, size: 20, itemStyle: { borderWidth: 6 } },
      title: { show: true, offsetCenter: [0, '70%'], fontSize: 14 },
      detail: { valueAnimation: true, fontSize: 24, offsetCenter: [0, '40%'], formatter: (v: number) => v.toFixed(1) },
      data: [{ value, name: metric }],
    }],
  };
}

// ---------- 漏斗图 ----------
function buildFunnelOption(dataset: ChartDataset) {
  const metric = dataset.metrics[0];
  const dim = dataset.dimensions[0];
  return {
    color: COLORS,
    tooltip: { trigger: 'item' },
    legend: { top: 0 },
    series: [{
      type: 'funnel',
      left: '10%', top: 40, bottom: 20, width: '80%',
      min: 0, max: Math.max(...dataset.rows.map((r) => Number(r[metric]) || 0)) || 100,
      minSize: '0%', maxSize: '100%',
      sort: 'descending', gap: 2,
      label: { show: true, position: 'inside' },
      labelLine: { length: 10, lineStyle: { width: 1, type: 'solid' } },
      itemStyle: { borderColor: '#fff', borderWidth: 1 },
      emphasis: { label: { fontSize: 16 } },
      data: dataset.rows.map((r) => ({ name: String(r[dim]), value: Number(r[metric]) || 0 })),
    }],
  };
}

// ---------- 桑基图 ----------
function buildSankeyOption(dataset: ChartDataset) {
  const [sourceDim, targetDim] = dataset.dimensions;
  const metric = dataset.metrics[0];
  const nodes = [...new Set([...dataset.rows.map((r) => String(r[sourceDim])), ...dataset.rows.map((r) => String(r[targetDim]))])].map((name) => ({ name }));
  const links = dataset.rows.map((r) => ({ source: String(r[sourceDim]), target: String(r[targetDim]), value: Number(r[metric]) || 0 }));
  return {
    color: COLORS,
    tooltip: { trigger: 'item', triggerOn: 'mousemove' },
    series: [{ type: 'sankey', data: nodes, links, emphasis: { focus: 'adjacency' }, lineStyle: { color: 'gradient', curveness: 0.5 } }],
  };
}

// ---------- 词云 ----------
function buildWordcloudOption(dataset: ChartDataset) {
  const dim = dataset.dimensions[0];
  const metric = dataset.metrics[0];
  const maxVal = Math.max(...dataset.rows.map((r) => Number(r[metric]) || 1));
  return {
    tooltip: { show: true },
    series: [{
      type: 'wordCloud',
      shape: 'circle',
      left: 'center', top: 'center',
      width: '90%', height: '90%',
      sizeRange: [12, 60],
      rotationRange: [-45, 45],
      rotationStep: 45,
      gridSize: 8,
      drawOutOfBound: false,
      textStyle: { fontFamily: 'sans-serif', fontWeight: 'bold', color: () => COLORS[Math.floor(Math.random() * COLORS.length)] },
      emphasis: { textStyle: { textShadowBlur: 10, textShadowColor: '#333' } },
      data: dataset.rows.map((r) => ({ name: String(r[dim]), value: Math.round((Number(r[metric]) || 1) / maxVal * 100) })),
    }],
  };
}

// ---------- 对比柱线图 ----------
function buildCompareBarLineOption(dataset: ChartDataset) {
  return buildComboOption(dataset);
}

// ---------- 表格（兜底） ----------
function buildTableOption(dataset: ChartDataset) {
  return { __table__: true, dataset };
}

// ---------- KPI 卡片 ----------
function buildKpiOption(dataset: ChartDataset) {
  const metric = dataset.metrics[0] || '数值'
  const firstRow = dataset.rows[0] || {}
  const value = firstRow[metric] ?? firstRow[Object.keys(firstRow)[0]] ?? 0
  return {
    __kpi__: true,
    title: metric,
    value: String(value),
    subtitle: dataset.dimensions[0] ? String(firstRow[dataset.dimensions[0]] ?? '') : '',
  };
}

// ---------- 注册 ----------
registry.set('bar', buildBarOption);
registry.set('line', buildLineOption);
registry.set('group_bar', buildGroupBarOption);
registry.set('radar', buildRadarOption);
registry.set('stack_bar', buildStackBarOption);
registry.set('combo', buildComboOption);
registry.set('rank', buildRankOption);
registry.set('pie', buildPieOption);
registry.set('area', buildAreaOption);
registry.set('scatter', buildScatterOption);
registry.set('heatmap', buildHeatmapOption);
registry.set('gauge', buildGaugeOption);
registry.set('funnel', buildFunnelOption);
registry.set('sankey', buildSankeyOption);
registry.set('wordcloud', buildWordcloudOption);
registry.set('compare_bar_line', buildCompareBarLineOption);
registry.set('table', buildTableOption);
registry.set('kpi', buildKpiOption);

/**
 * 图表工厂：根据类型生成 ECharts option
 * 未注册的类型回退到柱状图
 */
export function buildChartOption(chartType: ChartType, dataset: ChartDataset, custom?: Record<string, any>): any {
  const builder = registry.get(chartType);
  if (builder) {
    return builder(dataset, custom);
  }
  // 回退到柱状图
  return buildBarOption(dataset, custom);
}

/** 注册新图表类型 */
export function registerChartType(type: ChartType, builder: OptionBuilder): void {
  registry.set(type, builder);
}

/** 获取所有已注册图表类型 */
export function getRegisteredChartTypes(): ChartType[] {
  return Array.from(registry.keys()) as ChartType[];
}
