import { haptic } from '../telegram'
import { NumField } from './NumField'

/** − value + with large buttons (one hand, sweaty fingers); the value can also be typed. */
export function Stepper({
  value,
  min,
  max,
  onChange,
  invalid,
  label,
}: {
  value: number | null
  min: number
  max: number
  onChange: (v: number | null) => void
  invalid?: boolean
  label: string
}) {
  const step = (d: number) => {
    const next = Math.min(max, Math.max(min, (value ?? (d > 0 ? min - 1 : min + 1)) + d))
    if (next === value) return
    haptic.select()
    onChange(next)
  }
  return (
    <div className="stepper" role="group" aria-label={label}>
      <button className="icon-btn" aria-label={`${label}: меньше`} disabled={value != null && value <= min} onClick={() => step(-1)}>
        −
      </button>
      <NumField value={value} invalid={invalid} onChange={(v) => onChange(v == null ? null : Math.round(v))} />
      <button className="icon-btn" aria-label={`${label}: больше`} disabled={value != null && value >= max} onClick={() => step(1)}>
        +
      </button>
    </div>
  )
}
