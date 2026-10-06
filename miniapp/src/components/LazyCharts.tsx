import { lazy, Suspense, type ComponentProps } from 'react'

// Recharts is the heaviest dependency; load it only when a chart is on screen.
const Line = lazy(() => import('./ProgressChart').then((m) => ({ default: m.LineSeries })))
const Bar = lazy(() => import('./ProgressChart').then((m) => ({ default: m.BarSeries })))

type Props = ComponentProps<typeof Line>

export function LineSeries(props: Props) {
  return (
    <Suspense fallback={<div style={{ height: 200 }} />}>
      <Line {...props} />
    </Suspense>
  )
}

export function BarSeries(props: Props) {
  return (
    <Suspense fallback={<div style={{ height: 180 }} />}>
      <Bar {...props} />
    </Suspense>
  )
}
