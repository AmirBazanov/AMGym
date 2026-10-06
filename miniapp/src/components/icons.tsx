// Inline stroke icons (24x24), no icon library needed.
const base = {
  width: 24,
  height: 24,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 2,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
}

export const IconToday = () => (
  <svg {...base}>
    <path d="M6.5 6.5v11M17.5 6.5v11M3 9.5v5M21 9.5v5M6.5 12h11" />
  </svg>
)
export const IconProgram = () => (
  <svg {...base}>
    <rect x="3.5" y="4.5" width="17" height="16" rx="3" />
    <path d="M3.5 9.5h17M8 2.5v4M16 2.5v4M8 13.5h3M8 16.5h6" />
  </svg>
)
export const IconHistory = () => (
  <svg {...base}>
    <path d="M3.5 12a8.5 8.5 0 1 0 2.5-6" />
    <path d="M3.5 4v4h4M12 7.5V12l3 2" />
  </svg>
)
export const IconProgress = () => (
  <svg {...base}>
    <path d="M4 19.5h16" />
    <path d="M5 15l4.5-4.5 3.5 3L19.5 7" />
    <path d="M15 7h4.5v4.5" />
  </svg>
)
export const IconNutrition = () => (
  <svg {...base}>
    <path d="M5 3v5.5a2.5 2.5 0 0 0 5 0V3M7.5 3v18" />
    <path d="M18.5 21V3c-2.5 1.5-3.5 4.5-3.5 8v3h3.5" />
  </svg>
)
export const IconChevronLeft = () => (
  <svg {...base} width={22} height={22}>
    <path d="M15 5l-7 7 7 7" />
  </svg>
)
export const IconChevronRight = () => (
  <svg {...base} width={22} height={22}>
    <path d="M9 5l7 7-7 7" />
  </svg>
)
export const IconClose = () => (
  <svg {...base} width={18} height={18}>
    <path d="M6 6l12 12M18 6L6 18" />
  </svg>
)
export const IconCheck = () => (
  <svg {...base} strokeWidth={2.6}>
    <path d="M5 12.5l4.5 4.5L19 7.5" />
  </svg>
)
export const IconChevron = () => (
  <svg {...base} width={18} height={18} className="chev">
    <path d="M9 5l7 7-7 7" />
  </svg>
)
export const IconPlus = () => (
  <svg {...base} width={18} height={18}>
    <path d="M12 5v14M5 12h14" />
  </svg>
)
