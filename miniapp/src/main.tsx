import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './styles.css'
import { actions } from './store'
import { initTelegram } from './telegram'

initTelegram()
actions.prepareToday()
// The app may stay open in Telegram across days; prepare again when it comes back to the foreground.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') actions.prepareToday()
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
