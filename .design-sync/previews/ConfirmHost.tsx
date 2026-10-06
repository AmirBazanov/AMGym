import { useEffect } from 'react'
import { ConfirmHost, askInPage } from 'gymapp-miniapp'

// askInPage() opens the dialog; the host renders it over the page.
export function DeleteWorkout() {
  useEffect(() => {
    void askInPage('Удалить тренировку за 4 октября?')
  }, [])
  return (
    <div style={{ minHeight: 240 }}>
      <div className="card">Тренировка · 4 октября · 3 упражнения</div>
      <ConfirmHost />
    </div>
  )
}
