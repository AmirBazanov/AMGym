import { IntensityBadge } from 'gymapp-miniapp'

export function Heavy() {
  return <div className="row"><div className="grow">Неделя 3 · Пн</div><IntensityBadge value="heavy" /></div>
}

export function Medium() {
  return <div className="row"><div className="grow">Неделя 3 · Ср</div><IntensityBadge value="medium" /></div>
}
