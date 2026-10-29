import { useEffect, useState } from 'react'
import { freshLogin, getLifeView, logoutAppLogin, switchAppLogin, takePendingLogin, webLogin } from './api'
import { IconHistory, IconNutrition, IconProgram, IconProgress, IconToday } from './components/icons'
import { ConfirmHost } from './components/ConfirmHost'
import { ErrorBoundary } from './components/ErrorBoundary'
import { History } from './screens/History'
import { LifeSection } from './screens/life/LifeSection'
import { Nutrition } from './screens/Nutrition'
import { ProgramScreen } from './screens/Program'
import { Progress } from './screens/Progress'
import { Today } from './screens/Today'
import { confirm, haptic } from './telegram'
import { useRemote } from './useRemote'

type Tab = 'today' | 'program' | 'history' | 'progress' | 'nutrition'
type Section = 'gym' | 'life'

const TABS: { key: Tab; label: string; Icon: () => React.JSX.Element }[] = [
  { key: 'today', label: 'Сегодня', Icon: IconToday },
  { key: 'program', label: 'Программа', Icon: IconProgram },
  { key: 'history', label: 'История', Icon: IconHistory },
  { key: 'progress', label: 'Прогресс', Icon: IconProgress },
  { key: 'nutrition', label: 'Питание', Icon: IconNutrition },
]

const SECTION_KEY = 'gymapp.section'

function initialTab(): Tab {
  const t = new URLSearchParams(location.search).get('tab')
  return TABS.some((x) => x.key === t) ? (t as Tab) : 'today'
}

/** `?section=life` (the bot's «Открыть дела») wins over the last section the user chose. */
function initialSection(): Section {
  const s = new URLSearchParams(location.search).get('section')
  if (s === 'life' || s === 'gym') return s
  try {
    return localStorage.getItem(SECTION_KEY) === 'life' ? 'life' : 'gym'
  } catch {
    return 'gym'
  }
}

export default function App() {
  const [tab, setTab] = useState<Tab>(initialTab)
  const [section, setSection] = useState<Section>(initialSection)

  // A /web link with another account's token than the stored one: never switch without asking.
  useEffect(() => {
    const token = takePendingLogin()
    if (!token) return
    void confirm('Войти по новой ссылке? Сейчас здесь выполнен вход, он будет заменён.').then((ok) => {
      if (ok) switchAppLogin(token)
    })
  }, [])

  function choose(s: Section) {
    if (s === section) return
    haptic.select()
    setSection(s)
    window.scrollTo({ top: 0 })
    try {
      localStorage.setItem(SECTION_KEY, s)
    } catch {
      // Private mode: the choice lasts until the app closes.
    }
  }

  return (
    <>
      <main className="app">
        {webLogin && <WebLoginBar />}
        <div className="segmented section-switch" role="tablist" aria-label="Раздел">
          {(
            [
              ['gym', 'Зал'],
              ['life', 'Дела'],
            ] as const
          ).map(([key, label]) => (
            <button key={key} role="tab" aria-selected={section === key} className={section === key ? 'active' : ''} onClick={() => choose(key)}>
              {label}
            </button>
          ))}
        </div>
        {section === 'life' ? (
          <LifeSection />
        ) : (
          // Keyed by tab: switching tabs clears a caught error.
          <ErrorBoundary key={tab}>
            {tab === 'today' && <Today />}
            {tab === 'program' && <ProgramScreen />}
            {tab === 'history' && <History />}
            {tab === 'progress' && <Progress />}
            {tab === 'nutrition' && <Nutrition />}
          </ErrorBoundary>
        )}
      </main>
      {section === 'gym' && (
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
      )}
      <ConfirmHost />
    </>
  )
}

/**
 * Logged in from a computer (/web): whose login it is, so a foreign login link is noticed, and a way out.
 * The name comes from the server (`user.name` of /api/life/view) when it sends one.
 */
function WebLoginBar() {
  const r = useRemote('life:me', () => getLifeView())
  const name = r.data?.userName
  return (
    <div className={`notice web-login ${freshLogin ? 'fresh' : ''}`}>
      <span className="grow">
        {freshLogin ? 'Вход выполнен' : 'Вход с компьютера'}
        {name ? `: ${name}` : ''}
        {freshLogin && '. Не ты? Выйди.'}
      </span>
      <button
        className="link-btn"
        onClick={async () => {
          if (await confirm('Выйти на этом компьютере?')) logoutAppLogin()
        }}
      >
        Выйти
      </button>
    </div>
  )
}
