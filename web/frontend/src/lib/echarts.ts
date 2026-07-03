/** Slim ECharts build: only the components the dashboard uses (canvas
 * renderer, bar + line, grid/tooltip/legend/dataZoom/markLine). */

import { BarChart, LineChart } from 'echarts/charts';
import {
  DataZoomInsideComponent,
  DataZoomSliderComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
} from 'echarts/components';
import * as echarts from 'echarts/core';
import { CanvasRenderer } from 'echarts/renderers';

echarts.use([
  BarChart,
  LineChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomInsideComponent,
  DataZoomSliderComponent,
  MarkLineComponent,
  CanvasRenderer,
]);

export type { EChartsCoreOption } from 'echarts/core';
export { echarts };
