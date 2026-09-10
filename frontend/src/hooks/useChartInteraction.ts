/**
 * 公共图表交互 Hook：统一管理图例点击显隐、tooltip、dataZoom、resize
 * 所有图表组件通过此 Hook 获取交互能力，不重复实现
 */
import { useEffect, useRef, useCallback } from 'react';
import * as echarts from 'echarts';

interface UseChartInteractionOptions {
  /** 是否启用 dataZoom（数据量大时自动启用） */
  enableDataZoom?: boolean;
  /** 图例点击显隐（默认 true） */
  enableLegendToggle?: boolean;
  /** 容器 ref */
  containerRef: React.RefObject<HTMLDivElement | null>;
}

/**
 * 公共图表交互 Hook
 * 返回 chart 实例引用和更新方法
 */
export function useChartInteraction(options: UseChartInteractionOptions) {
  const { containerRef, enableDataZoom = false, enableLegendToggle = true } = options;
  const chartRef = useRef<echarts.ECharts | null>(null);

  /** 初始化图表 */
  const initChart = useCallback(() => {
    if (!containerRef.current || chartRef.current) return;
    chartRef.current = echarts.init(containerRef.current);
    // 图例点击显隐（ECharts 默认行为，此处确保启用）
    if (!enableLegendToggle) {
      chartRef.current.off('legendselectchanged');
    }
  }, [containerRef, enableLegendToggle]);

  /** 更新 option */
  const setOption = useCallback((option: any) => {
    if (!chartRef.current) initChart();
    if (!chartRef.current) return;
    // 自动添加 dataZoom
    if (enableDataZoom && option.xAxis && !option.dataZoom) {
      option.dataZoom = [
        { type: 'inside', start: 0, end: 100 },
        { type: 'slider', start: 0, end: 100, height: 20, bottom: 5 },
      ];
      option.grid = { ...option.grid, bottom: 50 };
    }
    chartRef.current.setOption(option, true);
  }, [initChart, enableDataZoom]);

  /** resize */
  const resize = useCallback(() => {
    chartRef.current?.resize();
  }, []);

  // 监听容器 resize
  useEffect(() => {
    if (!containerRef.current) return;
    const observer = new ResizeObserver(() => resize());
    observer.observe(containerRef.current);
    return () => {
      observer.disconnect();
      chartRef.current?.dispose();
      chartRef.current = null;
    };
  }, [containerRef, resize]);

  return { chartRef, initChart, setOption, resize };
}
