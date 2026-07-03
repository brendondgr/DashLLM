/** Generic ECharts host: init once, setOption on change, resize with its
 * container, dispose on unmount. Fills the parent flex cell. */

import { useEffect, useRef } from 'react';

import { echarts, type EChartsCoreOption } from '../lib/echarts';

interface Props {
  option: EChartsCoreOption | null;
}

export default function EChart({ option }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ReturnType<typeof echarts.init> | null>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const chart = echarts.init(host);
    chartRef.current = chart;
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(host);
    return () => {
      ro.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (option && chartRef.current) {
      chartRef.current.setOption(option);
    }
  }, [option]);

  return <div ref={hostRef} style={{ flex: 1, minHeight: 0, width: '100%' }} />;
}
