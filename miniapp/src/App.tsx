import { useState } from 'react'
import { IconHistory, IconNutrition, IconProgram, IconProgress, IconToday } from './components/icons'
import { ConfirmHost } from './components/ConfirmHost'
import { ErrorBoundary } from './components/ErrorBoundary'
import { History } from './screens/History'
import { Nutrition } from './screens/Nutrition'
import { ProgramScreen } from './screens/Program'
import { Progress } from './screens/Progress'
import { Today } from './screens/Today'
import { haptic } from './telegram'

type Tab = 'today' | 'program' | 'history' | 'progress' | 'nutrition'

const TABS: { key: Tab; label: string; Icon: () => React.JSX.Element }[] = [
  { key: 'today', label: 'Сегодня', Icon: IconToday },
  { key: 'program', label: 'Программа', Icon: IconProgram },
  { key: 'history', label: 'История', Icon: IconHistory },
  { key: 'progress', label: 'Прогресс', Icon: IconProgress },
  { key: 'nutrition', label: 'Питание', Icon: IconNutrition },
]

function initialTab(): Tab {
  const t = new URLSearchParams(location.search).get('tab')
  return TABS.some((x) => x.key === t) ? (t as Tab) : 'today'
}

export default function App() {
  const [tab, setTab] = useState<Tab>(initialTab)

  return (
    <>
      <main className="app">
        {/* Keyed by tab: switching tabs clears a caught error. */}
        <ErrorBoundary key={tab}>
          {tab === 'today' && <Today />}
          {tab === 'program' && <ProgramScreen />}
          {tab === 'history' && <History />}
          {tab === 'progress' && <Progress />}
          {tab === 'nutrition' && <Nutrition />}
        </ErrorBoundary>
      </main>
      <nav className="tabbar">
        <div className="tabbar-inner">
          {TABS.map(({ key, label, Icon }) => (
            <button
              key={key}
              className={`tab ${tab === key ? 'active' : ''}`}
              onClick={() => {
                if (tab !== key) haptic.select()
                setTab(key)
                window.scrollTo({ top: 0 })
              }}
            >
              <Icon />
              {label}
            </button>
          ))}
        </div>
      </nav>
      <ConfirmHost />
    </>
  )
}
