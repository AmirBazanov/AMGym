import { DropBadge } from 'gymapp-miniapp'

export function InSetRow() {
  return (
    <div className="row">
      <div className="grow">Сгибания на бицепс</div>
      <span className="num">16 × 12</span>
      <DropBadge />
    </div>
  )
}
