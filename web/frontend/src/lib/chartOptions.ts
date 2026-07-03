/** Chart option builders, ported 1:1 from the prototype's chartOpt() but
 * consuming the backend's ECharts dataset payloads. */

import type { EChartsCoreOption } from './echarts';
import { fmt, fmtBucket, fmtDay } from './format';
import { ACCENT, C, MONO } from './styles';
import type { RangeId, StatsDataset } from './types';

const axis = () => ({
  axisLine: { lineStyle: { color: C.border } },
  axisTick: { show: false },
  axisLabel: { color: C.axis, fontSize: 10, fontFamily: 'JetBrains Mono' },
  splitLine: { lineStyle: { color: C.bgHover } },
});

const tooltip = () => ({
  trigger: 'axis' as const,
  backgroundColor: '#161B26',
  borderColor: C.borderStrong,
  textStyle: { color: C.text, fontSize: 11, fontFamily: 'JetBrains Mono' },
  axisPointer: { lineStyle: { color: C.borderHover } },
});

/** Request volume: green request bars + stacked red error bars. */
export function volumeOption(
  data: StatsDataset, range: RangeId,
): EChartsCoreOption {
  const A = axis();
  const labels = data.source.map((r) => fmtBucket(String(r[0]), range));
  return {
    grid: { left: 44, right: 8, top: 14, bottom: 24 },
    tooltip: tooltip(),
    xAxis: { type: 'category', data: labels, ...A, splitLine: { show: false } },
    yAxis: { type: 'value', ...A },
    dataZoom: [{ type: 'inside' }],
    series: [
      {
        name: 'requests', type: 'bar',
        data: data.source.map((r) => r[1]),
        itemStyle: { color: C.green, borderRadius: [2, 2, 0, 0] },
        barMaxWidth: 14,
      },
      {
        name: 'errors', type: 'bar', stack: 'e',
        data: data.source.map((r) => r[2]),
        itemStyle: { color: C.red }, barMaxWidth: 14,
      },
    ],
  };
}

/** Tokens in/out over time: smooth area lines + brush slider. */
export function tokensOption(
  data: StatsDataset, range: RangeId,
): EChartsCoreOption {
  const A = axis();
  const labels = data.source.map((r) => fmtBucket(String(r[0]), range));
  return {
    grid: { left: 52, right: 8, top: 26, bottom: 44 },
    tooltip: tooltip(),
    legend: {
      top: 0, right: 0, itemWidth: 12, itemHeight: 8,
      textStyle: { color: C.textMut, fontSize: 10, fontFamily: 'JetBrains Mono' },
    },
    xAxis: {
      type: 'category', data: labels, ...A,
      splitLine: { show: false }, boundaryGap: false,
    },
    yAxis: {
      type: 'value', ...A,
      axisLabel: { ...A.axisLabel, formatter: (v: number) => fmt(v) },
    },
    dataZoom: [
      { type: 'inside' },
      {
        type: 'slider', height: 16, bottom: 4, borderColor: C.border,
        backgroundColor: C.bgSidebar, fillerColor: 'rgba(88,196,221,.12)',
        handleStyle: { color: C.borderStrong },
        textStyle: { color: C.textDim, fontSize: 9 },
      },
    ],
    series: [
      {
        name: 'tokens in', type: 'line',
        data: data.source.map((r) => r[1]),
        showSymbol: false, smooth: 0.25,
        lineStyle: { color: C.cyan, width: 1.5 },
        areaStyle: { color: 'rgba(88,196,221,.14)' },
      },
      {
        name: 'tokens out', type: 'line',
        data: data.source.map((r) => r[2]),
        showSymbol: false, smooth: 0.25,
        lineStyle: { color: ACCENT, width: 1.5 },
        areaStyle: { color: 'rgba(255,178,36,.12)' },
      },
    ],
  };
}

/** Live concurrency: step line over time with the burst mark line. */
export function concurrencyOption(
  series: [number, number][], maxConcurrency: number,
): EChartsCoreOption {
  const A = axis();
  return {
    animation: false,
    grid: { left: 34, right: 8, top: 12, bottom: 22 },
    tooltip: tooltip(),
    xAxis: { type: 'time', ...A, splitLine: { show: false } },
    yAxis: { type: 'value', max: maxConcurrency, ...A },
    series: [{
      type: 'line', data: series, showSymbol: false, step: 'end',
      lineStyle: { color: ACCENT, width: 1.5 },
      areaStyle: { color: 'rgba(255,178,36,.12)' },
      markLine: {
        silent: true, symbol: 'none',
        label: { color: C.red, fontSize: 9, fontFamily: 'JetBrains Mono' },
        lineStyle: { color: C.red, type: 'dashed', width: 1 },
        data: [{ yAxis: Math.round(maxConcurrency * 8 / 9), name: 'burst' }],
      },
    }],
  };
}

/** Tokens by hour of day (0-23), stacked in/out bars. */
export function hourOfDayOption(data: StatsDataset): EChartsCoreOption {
  const A = axis();
  return {
    grid: { left: 48, right: 8, top: 12, bottom: 24 },
    tooltip: tooltip(),
    xAxis: {
      type: 'category',
      data: data.source.map((r) => String(r[0]).padStart(2, '0')),
      ...A, splitLine: { show: false },
    },
    yAxis: {
      type: 'value', ...A,
      axisLabel: { ...A.axisLabel, formatter: (v: number) => fmt(v) },
    },
    series: [
      {
        name: 'in', type: 'bar', stack: 't',
        data: data.source.map((r) => r[1]),
        itemStyle: { color: C.cyan }, barMaxWidth: 10,
      },
      {
        name: 'out', type: 'bar', stack: 't',
        data: data.source.map((r) => r[2]),
        itemStyle: { color: ACCENT, borderRadius: [2, 2, 0, 0] },
        barMaxWidth: 10,
      },
    ],
  };
}

/** Tokens per day: grouped in/out bars. */
export function dailyOption(data: StatsDataset): EChartsCoreOption {
  const A = axis();
  return {
    grid: { left: 48, right: 8, top: 12, bottom: 24 },
    tooltip: tooltip(),
    xAxis: {
      type: 'category',
      data: data.source.map((r) => fmtDay(String(r[0]))),
      ...A, splitLine: { show: false },
    },
    yAxis: {
      type: 'value', ...A,
      axisLabel: { ...A.axisLabel, formatter: (v: number) => fmt(v) },
    },
    series: [
      {
        name: 'in', type: 'bar',
        data: data.source.map((r) => r[1]),
        itemStyle: { color: C.cyan, borderRadius: [2, 2, 0, 0] },
        barMaxWidth: 12,
      },
      {
        name: 'out', type: 'bar',
        data: data.source.map((r) => r[2]),
        itemStyle: { color: ACCENT, borderRadius: [2, 2, 0, 0] },
        barMaxWidth: 12,
      },
    ],
  };
}
