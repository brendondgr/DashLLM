# Design System

The dashboard's visual language is inherited **verbatim** from the approved
prototype (`LLM Proxy Dashboard.dc.html`). The port must not restyle it.

## Tokens

| Token | Value | Use |
| --- | --- | --- |
| `bg` | `#0B0E14` | page background |
| `bgSidebar` | `#0D1119` | sidebar, header, expanded rows |
| `bgCard` | `#11151D` | cards, chart panels |
| `bgInset` | `#131926` | inputs, segmented controls, header chips |
| `bgHover` | `#161C28` | nav hover/active, split lines |
| `border` | `#1D2430` | default borders |
| `borderStrong` | `#2A3547` | input borders, dropdown |
| `borderHover` | `#3A4A63` | hover borders, breakdown bars |
| `text` | `#E6EAF2` | primary text |
| `textMut` | `#8A94A6` | secondary text |
| `textDim` | `#525C6E` | tertiary text |
| `axis` | `#68738A` | chart axis labels |
| `accent` | `#FFB224` (default; options `#58C4DD`, `#3FB950`, `#BC8CFF`) | active states, tokens-out series |
| `cyan` | `#58C4DD` | tokens-in series, streaming states |
| `green` | `#3FB950` | success / online / requests bars |
| `red` | `#F85149` | errors / offline |
| `purple` | `#BC8CFF` | remote badge |

## Typography

- UI: `'IBM Plex Sans', sans-serif`, base 13px.
- Numerals / code / labels: `'JetBrains Mono', monospace`.
- Section headers: 600 11px, letter-spacing .07em, uppercase, `textMut`.

## Layout

- Fixed 200px sidebar, 48px header, scrollable content.
- Dashboard chart grid: 12-column CSS grid, three layout presets
  (A Overview / B Timeline / C Dense) with per-panel `span`/`height`/`order`.
- Cards: radius 8, 1px `border`, padding 12–16px.

## Motion

- `livepulse` 2s opacity pulse for live dots.
- `rowstream` 1.6s background pulse for streaming request rows.
- Toggle knobs animate `left .2s`; hot-swap "draining…" state before switch.
- ECharts default animations for windowed charts; `animation:false` on the
  live concurrency chart.

## Charts (ECharts)

- Canvas renderer, `echarts/core` with only used components (bar, line, grid,
  tooltip, legend, dataZoom) for bundle performance.
- Axis style: line `#1D2430`, labels `#68738A` 10px JetBrains Mono, split
  lines `#161C28`.
- Tooltip: bg `#161B26`, border `#2A3547`, text `#E6EAF2` 11px mono.
- Series colors: requests `#3FB950`, errors `#F85149`, in `#58C4DD`,
  out `#FFB224`.
