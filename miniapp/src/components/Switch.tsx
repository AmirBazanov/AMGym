/** On/off toggle with a large touch target (64x52), used by reminders and facts. */
export function Switch({ on, disabled, label, onToggle }: { on: boolean; disabled?: boolean; label: string; onToggle: () => void }) {
  return (
    <button
      className={`switch ${on ? 'on' : ''}`}
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={onToggle}
    >
      <span className="switch-track" />
    </button>
  )
}
