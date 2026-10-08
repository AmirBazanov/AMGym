import { Component, type ErrorInfo, type ReactNode } from 'react'

/** A render error in a screen shows a reload button instead of a blank page. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(error, info.componentStack)
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <div className="empty">
        <div className="big">Что-то сломалось</div>
        <p>Данные на месте. Перезагрузи дневник.</p>
        <button className="btn" onClick={() => location.reload()}>
          Перезагрузить
        </button>
      </div>
    )
  }
}
