/**
 * 布局引擎：12栅格自动布局
 * 根据卡片数量和图表类型推荐布局，支持自定义覆盖
 */
import type { ChartCardConfig, LayoutItem } from '../types/chart';

/**
 * 推荐布局：根据卡片数量和图表类型返回 LayoutItem[]
 * 规则：
 * - kpi → span=3（一行4个）
 * - rank → span=4（一行3个）
 * - combo/radar/sankey/funnel → span=8（大卡片）
 * - 其余按数量平均分配
 */
export function recommendLayout(cards: ChartCardConfig[]): LayoutItem[] {
  const n = cards.length;
  if (n === 0) return [];
  return cards.map((card, i) => {
    const type = card.chartType;
    let span: number;
    if (type === 'kpi') span = 3;
    else if (type === 'rank') span = 4;
    else if (['combo', 'radar', 'sankey', 'funnel', 'heatmap', 'wordcloud'].includes(type)) span = 8;
    else if (n === 1) span = 12;
    else if (n === 2) span = 6;
    else if (n === 3) span = 4;
    else if (n === 4) span = 6; // 2x2
    else span = Math.max(3, Math.floor(12 / Math.ceil(Math.sqrt(n))));
    return { index: i, span };
  });
}

/**
 * 将布局项分组为行（每行 span 之和 <= 12）
 */
export function groupIntoRows(layout: LayoutItem[]): LayoutItem[][] {
  const rows: LayoutItem[][] = [];
  let currentRow: LayoutItem[] = [];
  let currentSpan = 0;
  for (const item of layout) {
    if (currentSpan + item.span > 12 && currentRow.length > 0) {
      rows.push(currentRow);
      currentRow = [];
      currentSpan = 0;
    }
    currentRow.push(item);
    currentSpan += item.span;
  }
  if (currentRow.length > 0) rows.push(currentRow);
  return rows;
}

/**
 * 计算卡片高度（根据 span 和类型）
 */
export function calcCardHeight(span: number, chartType: string): number {
  if (chartType === 'kpi') return 120;
  if (span >= 8) return 360;
  if (span >= 6) return 300;
  return 260;
}
