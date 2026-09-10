/**
 * 图表卡片体系公共类型定义
 * 所有图表组件、Dashboard、AI解读均依赖此文件
 */

/** 支持的图表类型（17种 + KPI） */
export type ChartType =
  | 'kpi'           // 指标卡片
  | 'kpi_group'     // 多指标组合卡片
  | 'bar'           // 柱状图
  | 'line'          // 折线图
  | 'group_bar'     // 分组柱状图
  | 'radar'         // 雷达图
  | 'stack_bar'     // 堆叠柱状图
  | 'combo'         // 双Y轴柱线组合图
  | 'rank'          // 排行榜
  | 'pie'           // 饼图
  | 'area'          // 面积图
  | 'scatter'       // 散点图
  | 'heatmap'       // 热力图
  | 'gauge'         // 仪表盘
  | 'funnel'        // 漏斗图
  | 'sankey'        // 桑基图
  | 'wordcloud'     // 词云
  | 'compare_bar_line' // 对比柱线图
  | 'table';        // 表格（兜底）

/** 统一数据模型：所有图表输入都转换为此结构 */
export interface ChartDataset {
  /** 维度列名（X轴/分类） */
  dimensions: string[];
  /** 指标列名（Y轴/数值） */
  metrics: string[];
  /** 原始行数据 */
  rows: Record<string, any>[];
  /** 元信息（单位、格式等） */
  meta?: Record<string, { unit?: string; format?: string }>;
}

/** 单个图表卡片配置 */
export interface ChartCardConfig {
  /** 卡片唯一ID */
  id: string;
  /** 卡片标题 */
  title: string;
  /** 图表类型 */
  chartType: ChartType;
  /** 数据 */
  dataset: ChartDataset;
  /** 原始SQL（展示用） */
  sql?: string;
  /** 子查询ID（多查询关联） */
  subId?: string;
  /** 状态 */
  status?: 'success' | 'error' | 'loading';
  /** 错误信息 */
  error?: string;
  /** 自定义配置（颜色、堆叠等） */
  custom?: Record<string, any>;
}

/** 布局项：12栅格 */
export interface LayoutItem {
  /** 卡片索引 */
  index: number;
  /** 栅格跨度（1-12） */
  span: number;
}

/** Dashboard 数据：多查询结果汇总 */
export interface DashboardData {
  /** 原始问题 */
  originalQuestion: string;
  /** 布局配置 */
  layout: LayoutItem[];
  /** 卡片列表 */
  cards: ChartCardConfig[];
}

/** AI 解读结构化结果 */
export interface InterpretationResult {
  /** 总体结论 */
  summary: string;
  /** 关键指标 */
  keyMetrics: Array<{ name: string; value: string; change?: string }>;
  /** 趋势分析 */
  trends: string[];
  /** 对比发现 */
  comparisons: string[];
  /** 异常点 */
  anomalies: Array<{ desc: string; severity: 'high' | 'medium' | 'low' }>;
  /** 行动建议 */
  suggestions: string[];
  /** 原始文本（流式拼接） */
  rawText?: string;
}

/** 多查询 SSE 事件：multi_spec */
export interface MultiSpecEvent {
  original_question: string;
  sub_queries: Array<{
    sub_id: string;
    question: string;
    intent: string;
    chart_hint?: string;
    title?: string;
  }>;
  layout_hint: string;
  source: string;
}

/** 多查询 SSE 事件：sub_result */
export interface SubResultEvent {
  sub_id: string;
  title: string;
  chart_type: string;
  data: {
    columns: string[];
    rows: any[][];
    row_count?: number;
  };
  sql: string;
}

/** 多查询 SSE 事件：dashboard */
export interface DashboardEvent {
  layout: LayoutItem[];
  cards: Array<{
    sub_id: string;
    title: string;
    chart_type: string;
    data?: any;
    sql?: string;
    status: string;
    error?: string;
  }>;
  original_question: string;
}
