import { INTENSITY_LABEL, type Intensity } from '../program'

export function IntensityBadge({ value }: { value: Intensity | null }) {
  if (!value || value === 'light') return null
  return <span className={`badge ${value}`}>{INTENSITY_LABEL[value]}</span>
}

export function DropBadge() {
  return <span className="badge drop">дропсет</span>
}
