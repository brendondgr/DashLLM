# Component Map

Single React island mounted from `src/pages/index.astro`. No client routing —
screen switching is component state, exactly like the prototype.

```
App.tsx                      shell: sidebar nav, header (in-flight chip,
│                            hot-swap dropdown), screen switching, shared
│                            polling for endpoints + live stats
├── screens/Dashboard.tsx    range/layout controls, KPI row, chart grid,
│                            by-endpoint breakdown
├── screens/Requests.tsx     filter chips, search, expandable request table
├── screens/Endpoints.tsx    endpoint cards, add form, test / set-active
├── screens/SshTunnel.tsx    tunnel config form, generated command, test log,
│                            active tunnels list
├── screens/ProxyInfo.tsx    base URL, API key reveal/copy/regenerate,
│                            curl/python/node snippets, proxy stat cards
├── screens/Settings.tsx     toggles, proxy port, retention slider
└── EChart.tsx               generic ECharts host (init once, setOption,
                             ResizeObserver, dispose on unmount)
```

## Ownership

| Concern | Owner |
| --- | --- |
| Visual tokens & shared style helpers (`dot`, `badge`, `track`, `knob`, seg styles) | `src/lib/styles.ts` |
| Chart option builders (volume, tokens, concurrency, by-hour, by-day) | `src/lib/chartOptions.ts` |
| Typed API client (all `/admin/*` calls) | `src/lib/api.ts` |
| Polling | `src/hooks/usePoll.ts` (interval + visibility-aware) |
| Number/time formatting (`fmt`, `fmtT`) | `src/lib/format.ts` |
| Frontend event logging (batched POST to `/admin/logs/frontend`) | `src/lib/logger.ts` |
| Fonts, global CSS, keyframes | `src/layouts/Base.astro` |

## Conventions

- Inline React style objects preserving the prototype's exact values — the
  prototype is the design spec; no Tailwind/shadcn restyling.
- Files stay under ~500 lines (hard cap 800); screens are separate modules.
- All UI actions call `log.event(...)` so they reach the backend log stream.
