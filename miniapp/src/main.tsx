import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './styles.css'
import { actions, syncFromServer } from './store'
import { initTelegram, onForeground } from './telegram'

initTelegram()
// Load the real history first so "today's workout" accounts for sets logged via the bot chat.
const refresh = () => syncFromServer().finally(() => actions.prepareToday())
void refresh()
// The app may stay open in Telegram across days; sync and prepare again when it returns to the foreground.
onForeground(() => void refresh())

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
