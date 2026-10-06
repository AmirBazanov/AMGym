import { ExerciseSheet } from 'gymapp-miniapp'

// Reads the app store: with no saved history it shows the program plan for the exercise.
// Same containing-block wrapper as Sheet so the fixed overlay stays inside the card.
export function BenchPress() {
  return (
    <div style={{ position: 'relative', height: 600, overflow: 'hidden', transform: 'translateZ(0)', background: 'var(--bg)' }}>
      <ExerciseSheet name="жим лёжа" onClose={() => {}} />
    </div>
  )
}
