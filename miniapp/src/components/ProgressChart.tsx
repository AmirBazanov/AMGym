import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

// Single-series charts: one hue (the Telegram button color), 2px line, recessive grid.
const ACCENT = 'var(--accent)'
const axis = { stroke: 'var(--hint)', fontSize: 11, tickLine: false, axisLine: false } as const

interface TipProps {
  active?: boolean
  payload?: { value: number; payload: Record<string, unknown> }[]
  unit: string
  labelKey: string
}

function Tip({ active, payload, unit, labelKey }: TipProps) {
  if (!active || !payload?.length) return null
  const p = payload[0]
  return (
    <div className="chart-tooltip">
      <div className="muted">{String(p.payload[labelKey])}</div>
      <b className="num">
        {p.value.toLocaleString('ru-RU')} {unit}
      </b>
    </div>
  )
}

export function LineSeries({
  data,
  x,
  y,
  unit,
  decimals = false,
}: {
  data: object[]
  x: string
  y: string
  unit: string
  decimals?: boolean // fractional axis ticks, for narrow ranges like body weight
}) {
  return (
    <ResponsiveContainer width="100%" height={200}>
      <AreaChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
        <defs>
          <linearGradient id="fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={ACCENT} stopOpacity={0.22} />
            <stop offset="100%" stopColor={ACCENT} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid vertical={false} stroke="var(--sep)" />
        <XAxis dataKey={x} {...axis} minTickGap={16} />
        <YAxis
          {...axis}
          width={40}
          domain={['auto', 'auto']}
          allowDecimals={decimals}
          // Decimal comma for weight ticks; other charts keep the default (no thousands separator).
          tickFormatter={decimals ? (v: number) => String(v).replace('.', ',') : undefined}
        />
        <Tooltip
          content={<Tip unit={unit} labelKey={x} />}
          cursor={{ stroke: 'var(--hint)', strokeDasharray: '3 3' }}
        />
        <Area
          type="monotone"
          dataKey={y}
          stroke={ACCENT}
          strokeWidth={2}
          fill="url(#fill)"
          dot={{ r: 3, fill: ACCENT, strokeWidth: 0 }}
          activeDot={{ r: 5, stroke: 'var(--card)', strokeWidth: 2 }}
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  )
}

export function BarSeries({
  data,
  x,
  y,
  unit,
  target,
}: {
  data: object[]
  x: string
  y: string
  unit: string
  target?: number | null // horizontal goal line, e.g. the daily kcal target
}) {
  return (
    <ResponsiveContainer width="100%" height={180}>
      <BarChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
        <CartesianGrid vertical={false} stroke="var(--sep)" />
        <XAxis dataKey={x} {...axis} />
        <YAxis {...axis} width={40} />
        <Tooltip content={<Tip unit={unit} labelKey={x} />} cursor={{ fill: 'var(--soft)' }} />
        <Bar dataKey={y} fill={ACCENT} radius={[4, 4, 0, 0]} maxBarSize={36} isAnimationActive={false} />
        {target != null && (
          <ReferenceLine y={target} stroke="var(--text)" strokeDasharray="4 4" strokeOpacity={0.6} ifOverflow="extendDomain" />
        )}
      </BarChart>
    </ResponsiveContainer>
  )
}
