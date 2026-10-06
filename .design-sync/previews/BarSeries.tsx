import { BarSeries } from 'gymapp-miniapp'

const week = [
  { day: 'Пн', kcal: 2410 },
  { day: 'Вт', kcal: 2180 },
  { day: 'Ср', kcal: 2650 },
  { day: 'Чт', kcal: 1980 },
  { day: 'Пт', kcal: 2720 },
  { day: 'Сб', kcal: 2300 },
  { day: 'Вс', kcal: 0 },
]

export function WeeklyKcalWithTarget() {
  return (
    <div className="chart-card">
      <BarSeries data={week} x="day" y="kcal" unit="ккал" target={2600} />
    </div>
  )
}

export function WeeklyKcal() {
  return (
    <div className="chart-card">
      <BarSeries data={week} x="day" y="kcal" unit="ккал" />
    </div>
  )
}
