import { describe, expect, it } from 'vitest'
import {
  addDays,
  agendaDoneKind,
  agendaLabel,
  AGENDA_PATH,
  daysBetween,
  dayHeading,
  dayMonth,
  doneTasks,
  dueChores,
  dueLabel,
  errorDetail,
  everyLabel,
  formBody,
  formValues,
  inAgenda,
  markDone,
  longDate,
  LIFE_PARSERS,
  openDeadlines,
  openTasks,
  parseInline,
  parseMarkdown,
  parseNote,
  parseNoteHead,
  parseView,
  projectGroups,
  safeHref,
  scheduleWeek,
  soonDeadlines,
  sortChores,
  sortNotes,
  timeRange,
  weekDates,
  weekdayOf,
  weekLabel,
} from './life'
import type {
  AgendaItem,
  LifeChore,
  LifeDeadline,
  LifeEvent,
  LifeNoteHead,
  LifeProject,
  LifeSlot,
  LifeTask,
  MdInline,
} from './life'

const TODAY = '2026-10-29' // Thursday

const slot = (o: Partial<LifeSlot>): LifeSlot => ({
  id: 1,
  weekday: 1,
  start: '10:00',
  end: null,
  title: 'Матан',
  area: 'study',
  tag: null,
  place: null,
  vld: null,
  note: null,
  ...o,
})
const event = (o: Partial<LifeEvent>): LifeEvent => ({
  id: 1,
  day: TODAY,
  start: null,
  end: null,
  title: 'Созвон',
  area: 'other',
  projectId: null,
  remindBefore: null,
  done: false,
  note: null,
  ...o,
})
const deadline = (o: Partial<LifeDeadline>): LifeDeadline => ({
  id: 1,
  day: TODAY,
  title: 'Лаба',
  course: null,
  projectId: null,
  done: false,
  ...o,
})
const task = (o: Partial<LifeTask>): LifeTask => ({ id: 1, title: 'Задача', tag: null, projectId: null, done: false, doneAt: null, ...o })
const chore = (o: Partial<LifeChore>): LifeChore => ({ id: 1, title: 'Уборка', everyDays: 7, lastDone: null, left: 0, ...o })
const project = (o: Partial<LifeProject>): LifeProject => ({
  id: 1,
  name: 'Проект',
  kind: 'own',
  status: 'active',
  description: null,
  link: null,
  nextStep: null,
  ...o,
})
const noteHead = (o: Partial<LifeNoteHead>): LifeNoteHead => ({
  id: 1,
  title: 'Заметка',
  tags: [],
  author: 'me',
  projectId: null,
  updatedAt: null,
  ...o,
})
const agendaItem = (o: Partial<AgendaItem>): AgendaItem => ({
  kind: 'event',
  id: 1,
  title: 'Пункт',
  start: null,
  end: null,
  area: null,
  place: null,
  done: false,
  overdueDays: 0,
  ...o,
})

/** parseView of a minimal valid answer plus `o`, asserted non-null. */
const view = (o: Record<string, unknown> = {}) => parseView({ today: TODAY, ...o })!

const sample = {
  today: TODAY,
  settings: { remindBefore: 15, summaryTime: '07:30', summaryEnabled: false },
  slots: [
    { id: 1, weekday: 4, start: '10:00', end: '11:30', title: 'Матан', area: 'study', tag: 'вуз', place: 'А-101', vld: '12:00', note: 'тетрадь' },
  ],
  events: [
    { id: 2, day: '2026-10-30', start: '09:00', end: '09:30', title: 'Созвон', area: 'work', projectId: 3, remindBefore: 10, done: false, note: null },
  ],
  deadlines: [{ id: 3, day: '2026-11-02', title: 'Лаба 1', course: 'Физика', projectId: null, done: true }],
  tasks: [{ id: 4, title: 'Резюме', tag: 'работа', projectId: 3, done: true, doneAt: '2026-10-28T10:00:00Z' }],
  chores: [{ id: 5, title: 'Уборка', everyDays: 7, lastDone: '2026-10-22', left: 0 }],
  projects: [{ id: 3, name: 'GymAPP', kind: 'own', status: 'paused', description: 'Бот', link: 'https://example.com', nextStep: 'Тесты' }],
  notes: [{ id: 6, title: 'Идеи', tags: ['a', 'b'], author: 'bot', projectId: 3, updatedAt: '2026-10-29T06:00:00Z' }],
  agenda: [
    { kind: 'slot', id: 1, title: 'Матан', start: '10:00', end: '11:30', area: 'study', place: 'А-101', done: false, overdueDays: 0 },
    { kind: 'chore', id: 5, title: 'Уборка', start: null, end: null, area: null, place: null, done: false, overdueDays: 0 },
  ],
}

describe('parseView', () => {
  it('round-trips a full valid sample', () => {
    expect(parseView(sample)).toEqual({ ...sample, userName: null })
  })

  it('reads the optional user name (for the /web login bar)', () => {
    expect(parseView({ ...sample, user: { name: 'Амир' } })?.userName).toBe('Амир')
    expect(parseView({ ...sample, user: { name: '' } })?.userName).toBeNull()
    expect(parseView({ ...sample, user: 'Амир' })?.userName).toBeNull()
  })

  it('returns null for a non-object answer', () => {
    for (const raw of [null, undefined, 'x', 42, true, []]) expect(parseView(raw)).toBeNull()
  })

  it('returns null without a valid `today`', () => {
    expect(parseView({})).toBeNull()
    expect(parseView({ ...sample, today: undefined })).toBeNull()
    expect(parseView({ ...sample, today: '' })).toBeNull()
    expect(parseView({ ...sample, today: '2026-1-5' })).toBeNull()
    expect(parseView({ ...sample, today: '29.10.2026' })).toBeNull()
    expect(parseView({ ...sample, today: 20261029 })).toBeNull()
    expect(parseView({ ...sample, today: '2026-10-29T00:00:00' })).toBeNull()
  })

  it('reads missing or non-array lists as []', () => {
    expect(parseView({ today: TODAY })).toEqual({
      today: TODAY,
      settings: null,
      slots: [],
      events: [],
      deadlines: [],
      tasks: [],
      chores: [],
      projects: [],
      notes: [],
      agenda: [],
      userName: null,
    })
    const v = view({ slots: 'x', events: {}, deadlines: null, tasks: 5 })
    expect(v.slots).toEqual([])
    expect(v.events).toEqual([])
    expect(v.deadlines).toEqual([])
    expect(v.tasks).toEqual([])
  })

  it('drops malformed slots and keeps the good ones', () => {
    const good = { id: 1, weekday: 1, title: 'ok' }
    const v = view({
      slots: [
        good,
        { weekday: 1, title: 'no id' },
        { id: 0, weekday: 1, title: 'id zero' },
        { id: -2, weekday: 1, title: 'negative id' },
        { id: '7', weekday: 1, title: 'string id' },
        { id: 1.5, weekday: 1, title: 'float id' },
        { id: 2, weekday: 1 },
        { id: 3, weekday: 1, title: '   ' },
        { id: 4, weekday: 8, title: 'weekday 8' },
        { id: 5, weekday: 0, title: 'weekday 0' },
        { id: 6, title: 'no weekday' },
        { id: 7, weekday: '1', title: 'weekday as string' },
        null,
        'x',
        [],
        { id: 8, weekday: 7, title: 'sunday' },
      ],
    })
    expect(v.slots.map((s) => s.id)).toEqual([1, 8])
    expect(v.slots[1]!.weekday).toBe(7)
  })

  it('drops events and deadlines with a bad date, id or title', () => {
    const v = view({
      events: [
        { id: 1, day: '2026-10-29', title: 'ok' },
        { id: 2, day: '29.10.2026', title: 'bad date' },
        { id: 3, day: '2026-1-5', title: 'short date' },
        { id: 4, title: 'no date' },
        { id: 0, day: '2026-10-29', title: 'id zero' },
        { id: 5, day: '2026-10-29' },
      ],
      deadlines: [
        { id: 1, day: '2026-11-01', title: 'ok' },
        { id: 2, day: 'tomorrow', title: 'bad date' },
        { id: 3, day: '2026-11-01', title: '' },
        { day: '2026-11-01', title: 'no id' },
      ],
    })
    expect(v.events.map((e) => e.id)).toEqual([1])
    expect(v.deadlines.map((d) => d.id)).toEqual([1])
  })

  it('drops tasks, chores, projects and agenda items that cannot be shown', () => {
    const v = view({
      tasks: [{ id: 1, title: 'ok' }, { id: 0, title: 'zero' }, { id: 2 }],
      chores: [
        { id: 1, title: 'ok', everyDays: 7 },
        { id: 2, title: 'zero days', everyDays: 0 },
        { id: 3, title: 'no days' },
        { id: 4, title: 'float days', everyDays: 1.5 },
        { id: 5, everyDays: 3 },
      ],
      projects: [{ id: 1, name: 'ok' }, { id: 2 }, { id: 3, name: ' ' }],
      agenda: [
        { kind: 'event', id: 1, title: 'ok' },
        { kind: 'meeting', id: 2, title: 'unknown kind' },
        { id: 3, title: 'no kind' },
        { kind: 'event', id: 0, title: 'id zero' },
        { kind: 'event', id: 4 },
      ],
    })
    expect(v.tasks.map((t) => t.id)).toEqual([1])
    expect(v.chores.map((c) => c.id)).toEqual([1])
    expect(v.projects.map((p) => p.id)).toEqual([1])
    expect(v.agenda.map((a) => a.id)).toEqual([1])
  })

  it('falls back on unknown enum values', () => {
    const v = view({
      slots: [{ id: 1, weekday: 1, title: 'a', area: 'weird' }, { id: 2, weekday: 1, title: 'b' }],
      events: [{ id: 1, day: TODAY, title: 'a', area: 'weird' }, { id: 2, day: TODAY, title: 'b' }],
      projects: [
        { id: 1, name: 'a', kind: 'weird', status: 'weird' },
        { id: 2, name: 'b' },
      ],
    })
    expect(v.slots.map((s) => s.area)).toEqual(['work', 'work'])
    expect(v.events.map((e) => e.area)).toEqual(['other', 'other'])
    expect(v.projects.map((p) => [p.kind, p.status])).toEqual([
      ['own', 'active'],
      ['own', 'active'],
    ])
  })

  it('keeps known enum values', () => {
    const v = view({
      slots: [{ id: 1, weekday: 1, title: 'a', area: 'study' }],
      events: [{ id: 1, day: TODAY, title: 'a', area: 'study' }, { id: 2, day: TODAY, title: 'b', area: 'work' }],
      projects: [
        { id: 1, name: 'a', kind: 'learning', status: 'idea' },
        { id: 2, name: 'b', kind: 'work', status: 'done' },
      ],
    })
    expect(v.slots[0]!.area).toBe('study')
    expect(v.events.map((e) => e.area)).toEqual(['study', 'work'])
    expect(v.projects.map((p) => [p.kind, p.status])).toEqual([
      ['learning', 'idea'],
      ['work', 'done'],
    ])
  })

  it('reads "10:00:00" as "10:00" and invalid times as null', () => {
    const v = view({
      slots: [
        { id: 1, weekday: 1, title: 'a', start: '10:00:00', end: '11:30:45', vld: '12:15:00' },
        { id: 2, weekday: 1, title: 'b', start: '25:00', end: '10:60', vld: '9:00' },
        { id: 3, weekday: 1, title: 'c', start: 1000, end: null },
      ],
    })
    expect(v.slots[0]).toMatchObject({ start: '10:00', end: '11:30', vld: '12:15' })
    expect(v.slots[1]).toMatchObject({ start: null, end: null, vld: null })
    expect(v.slots[2]).toMatchObject({ start: null, end: null })
  })

  it('normalises optional strings and numbers', () => {
    const v = view({
      events: [
        { id: 1, day: TODAY, title: 'a', note: '  ', projectId: 0, remindBefore: -1, done: 'yes' },
        { id: 2, day: TODAY, title: 'b', remindBefore: 0, done: true },
        { id: 3, day: TODAY, title: 'c', remindBefore: 1.5, projectId: 4 },
      ],
      chores: [{ id: 1, title: 'a', everyDays: 3, lastDone: 'yesterday' }, { id: 2, title: 'b', everyDays: 3, left: -4, lastDone: '2026-10-25' }],
    })
    expect(v.events[0]).toMatchObject({ note: null, projectId: null, remindBefore: null, done: false })
    expect(v.events[1]).toMatchObject({ remindBefore: 0, done: true })
    expect(v.events[2]).toMatchObject({ remindBefore: null, projectId: 4 })
    expect(v.chores[0]).toMatchObject({ lastDone: null, left: 0 })
    expect(v.chores[1]).toMatchObject({ lastDone: '2026-10-25', left: -4 })
  })

  it('reads absent or non-object settings as null', () => {
    expect(view().settings).toBeNull()
    expect(view({ settings: null }).settings).toBeNull()
    expect(view({ settings: 'x' }).settings).toBeNull()
    expect(view({ settings: [] }).settings).toBeNull()
  })

  it('gives partial settings their defaults (30, "08:00", true)', () => {
    expect(view({ settings: {} }).settings).toEqual({ remindBefore: 30, summaryTime: '08:00', summaryEnabled: true })
    expect(view({ settings: { remindBefore: 10 } }).settings).toEqual({ remindBefore: 10, summaryTime: '08:00', summaryEnabled: true })
    expect(view({ settings: { summaryTime: '07:15:00' } }).settings).toEqual({ remindBefore: 30, summaryTime: '07:15', summaryEnabled: true })
    expect(view({ settings: { remindBefore: 0, summaryEnabled: false } }).settings).toEqual({
      remindBefore: 0,
      summaryTime: '08:00',
      summaryEnabled: false,
    })
  })

  it('replaces invalid settings values with defaults', () => {
    expect(view({ settings: { remindBefore: -5, summaryTime: '25:00', summaryEnabled: 'no' } }).settings).toEqual({
      remindBefore: 30,
      summaryTime: '08:00',
      summaryEnabled: true,
    })
    expect(view({ settings: { remindBefore: 1.5 } }).settings!.remindBefore).toBe(30)
  })

  it('sorts the agenda: timed by start, untimed last, stable', () => {
    const v = view({
      agenda: [
        { kind: 'chore', id: 1, title: 'A' },
        { kind: 'event', id: 2, title: 'B', start: '12:00' },
        { kind: 'event', id: 3, title: 'C', start: '09:00' },
        { kind: 'deadline', id: 4, title: 'D' },
        { kind: 'slot', id: 5, title: 'E', start: '09:00' },
      ],
    })
    expect(v.agenda.map((a) => a.title)).toEqual(['C', 'E', 'B', 'A', 'D'])
  })

  it('clamps a negative overdueDays to 0 and fills agenda defaults', () => {
    const v = view({ agenda: [{ kind: 'chore', id: 1, title: 'A', overdueDays: -3 }, { kind: 'chore', id: 2, title: 'B', overdueDays: 4 }] })
    expect(v.agenda[0]).toEqual({
      kind: 'chore',
      id: 1,
      title: 'A',
      start: null,
      end: null,
      area: null,
      place: null,
      done: false,
      overdueDays: 0,
    })
    expect(v.agenda[1]!.overdueDays).toBe(4)
  })
})

describe('parseNoteHead / parseNote', () => {
  it('fills defaults for a bare note', () => {
    expect(parseNoteHead({ id: 3 })).toEqual({ id: 3, title: 'Без названия', tags: [], author: 'me', projectId: null, updatedAt: null })
    expect(parseNote({ id: 3 })).toEqual({
      id: 3,
      title: 'Без названия',
      tags: [],
      author: 'me',
      projectId: null,
      updatedAt: null,
      body: '',
    })
  })

  it('falls back to "Без названия" for a blank or non-string title', () => {
    expect(parseNoteHead({ id: 1, title: '   ' })!.title).toBe('Без названия')
    expect(parseNoteHead({ id: 1, title: 5 })!.title).toBe('Без названия')
    expect(parseNoteHead({ id: 1, title: 'Идеи' })!.title).toBe('Идеи')
  })

  it('returns null for a non-object or a bad id', () => {
    for (const raw of [null, undefined, 'x', [], {}, { id: 0 }, { id: '1' }, { id: -1 }]) {
      expect(parseNoteHead(raw)).toBeNull()
      expect(parseNote(raw)).toBeNull()
    }
  })

  it('drops non-string and blank tags', () => {
    expect(parseNoteHead({ id: 1, tags: ['a', 1, null, '', '  ', {}, 'b'] })!.tags).toEqual(['a', 'b'])
    expect(parseNoteHead({ id: 1, tags: 'a,b' })!.tags).toEqual([])
    expect(parseNoteHead({ id: 1, tags: null })!.tags).toEqual([])
  })

  it('reads any author other than "bot" as "me"', () => {
    expect(parseNoteHead({ id: 1, author: 'bot' })!.author).toBe('bot')
    expect(parseNoteHead({ id: 1, author: 'me' })!.author).toBe('me')
    expect(parseNoteHead({ id: 1, author: 'BOT' })!.author).toBe('me')
    expect(parseNoteHead({ id: 1, author: 'other' })!.author).toBe('me')
    expect(parseNoteHead({ id: 1, author: 7 })!.author).toBe('me')
  })

  it('keeps the body of a full note verbatim and the head without one', () => {
    const body = '\n  # Title\n\n- a\n'
    expect(parseNote({ id: 1, body })!.body).toBe(body)
    expect(parseNote({ id: 1, body: 5 })!.body).toBe('')
    expect(parseNoteHead({ id: 1, body })).not.toHaveProperty('body')
  })

  it('is registered in LIFE_PARSERS for every kind', () => {
    expect(Object.keys(LIFE_PARSERS).sort()).toEqual(['chores', 'deadlines', 'events', 'notes', 'projects', 'slots', 'tasks'])
    expect(LIFE_PARSERS.notes({ id: 1, body: 'x' })).toMatchObject({ id: 1, body: 'x' })
    expect(LIFE_PARSERS.tasks({ id: 1, title: 'x' })).toMatchObject({ id: 1, title: 'x', done: false })
    expect(LIFE_PARSERS.tasks({ id: 0, title: 'x' })).toBeNull()
  })
})

describe('errorDetail', () => {
  it('returns a string detail', () => {
    expect(errorDetail('422 {"detail":"Дата вне диапазона"}')).toBe('Дата вне диапазона')
    expect(errorDetail('400 {"detail":"Нельзя"}')).toBe('Нельзя')
  })

  it('returns the first msg of a pydantic list', () => {
    expect(errorDetail('422 {"detail":[{"loc":["body","title"],"msg":"Field required"},{"msg":"second"}]}')).toBe('Field required')
  })

  it('returns null for non-JSON text', () => {
    expect(errorDetail('500 Internal Server Error')).toBeNull()
    expect(errorDetail('Failed to fetch')).toBeNull()
    expect(errorDetail('')).toBeNull()
  })

  it('returns null when there is no usable detail', () => {
    expect(errorDetail('404 {}')).toBeNull()
    expect(errorDetail('404 null')).toBeNull()
    expect(errorDetail('500 42')).toBeNull()
    expect(errorDetail('422 {"detail":""}')).toBeNull()
    expect(errorDetail('422 {"detail":"  "}')).toBeNull()
    expect(errorDetail('422 {"detail":[]}')).toBeNull()
    expect(errorDetail('422 {"detail":[{"loc":[]}]}')).toBeNull()
    expect(errorDetail('422 {"detail":{"a":1}}')).toBeNull()
  })
})

describe('dates', () => {
  it('addDays crosses month and year ends', () => {
    expect(addDays('2026-10-31', 1)).toBe('2026-11-01')
    expect(addDays('2026-12-31', 1)).toBe('2027-01-01')
    expect(addDays('2027-01-01', -1)).toBe('2026-12-31')
    expect(addDays('2026-03-01', -1)).toBe('2026-02-28')
    expect(addDays('2026-10-29', 0)).toBe('2026-10-29')
    expect(addDays('2026-10-29', 365)).toBe('2027-10-29')
    expect(addDays('2026-10-29', -29)).toBe('2026-09-30')
  })

  it('addDays handles the leap day', () => {
    expect(addDays('2028-02-28', 1)).toBe('2028-02-29')
    expect(addDays('2028-02-29', 1)).toBe('2028-03-01')
    expect(addDays('2028-03-01', -1)).toBe('2028-02-29')
    expect(addDays('2027-02-28', 1)).toBe('2027-03-01')
  })

  it('daysBetween is positive forward and negative backward', () => {
    expect(daysBetween('2026-10-29', '2026-11-03')).toBe(5)
    expect(daysBetween('2026-11-03', '2026-10-29')).toBe(-5)
    expect(daysBetween('2026-10-29', '2026-10-29')).toBe(0)
    expect(daysBetween('2026-12-31', '2027-01-01')).toBe(1)
    expect(daysBetween('2028-02-28', '2028-03-01')).toBe(2)
    expect(daysBetween('2027-02-28', '2027-03-01')).toBe(1)
    expect(daysBetween('2026-01-01', '2027-01-01')).toBe(365)
    expect(daysBetween('2028-01-01', '2029-01-01')).toBe(366)
  })

  it('weekdayOf: 1 = Monday .. 7 = Sunday', () => {
    expect(weekdayOf('2026-10-29')).toBe(4)
    expect(weekdayOf('2026-11-01')).toBe(7)
    expect(weekdayOf('2026-10-26')).toBe(1)
    expect(weekdayOf('2026-10-31')).toBe(6)
    expect(weekdayOf('1970-01-01')).toBe(4) // Thursday
    expect(weekdayOf('2000-01-01')).toBe(6) // Saturday
  })

  it('weekDates: the Monday-first week of today', () => {
    const w = weekDates(TODAY, 0)
    expect(w).toHaveLength(7)
    expect(w[0]).toBe('2026-10-26')
    expect(w[6]).toBe('2026-11-01')
    expect(w).toEqual(['2026-10-26', '2026-10-27', '2026-10-28', '2026-10-29', '2026-10-30', '2026-10-31', '2026-11-01'])
    expect(weekDates(TODAY)).toEqual(w)
  })

  it('weekDates with an offset moves whole weeks', () => {
    expect(weekDates(TODAY, 1)).toEqual(['2026-11-02', '2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06', '2026-11-07', '2026-11-08'])
    expect(weekDates(TODAY, -1)).toEqual(['2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22', '2026-10-23', '2026-10-24', '2026-10-25'])
    expect(weekDates(TODAY, 2)[0]).toBe('2026-11-09')
  })

  it('weekDates: Monday and Sunday stay in their own week, and the year end rolls over', () => {
    expect(weekDates('2026-10-26')[0]).toBe('2026-10-26')
    expect(weekDates('2026-11-01')[0]).toBe('2026-10-26')
    expect(weekDates('2026-11-01')[6]).toBe('2026-11-01')
    const w = weekDates('2026-12-31')
    expect(w[0]).toBe('2026-12-28')
    expect(w[6]).toBe('2027-01-03')
  })

  it('does not drift around DST changes (Europe 2026-03-29 and 2026-10-25)', () => {
    expect(addDays('2026-03-28', 1)).toBe('2026-03-29')
    expect(addDays('2026-03-29', 1)).toBe('2026-03-30')
    expect(addDays('2026-03-28', 2)).toBe('2026-03-30')
    expect(daysBetween('2026-03-28', '2026-03-30')).toBe(2)
    expect(daysBetween('2026-03-29', '2026-03-30')).toBe(1)
    expect(addDays('2026-10-24', 1)).toBe('2026-10-25')
    expect(addDays('2026-10-25', 1)).toBe('2026-10-26')
    expect(daysBetween('2026-10-24', '2026-10-26')).toBe(2)
    expect(daysBetween('2026-10-25', '2026-10-26')).toBe(1)
    expect(weekdayOf('2026-03-29')).toBe(7)
    expect(weekdayOf('2026-10-25')).toBe(7)
    expect(weekDates('2026-03-29')).toEqual(['2026-03-23', '2026-03-24', '2026-03-25', '2026-03-26', '2026-03-27', '2026-03-28', '2026-03-29'])
    expect(weekDates('2026-10-25')).toEqual(['2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22', '2026-10-23', '2026-10-24', '2026-10-25'])
  })
})

describe('labels', () => {
  it('dueLabel: today, tomorrow and Russian plurals ahead', () => {
    expect(dueLabel(0)).toBe('сегодня')
    expect(dueLabel(1)).toBe('завтра')
    expect(dueLabel(2)).toBe('через 2 дня')
    expect(dueLabel(5)).toBe('через 5 дней')
    expect(dueLabel(11)).toBe('через 11 дней')
    expect(dueLabel(21)).toBe('через 21 день')
    expect(dueLabel(22)).toBe('через 22 дня')
    expect(dueLabel(111)).toBe('через 111 дней')
  })

  it('dueLabel: overdue', () => {
    expect(dueLabel(-1)).toBe('просрочено на 1 день')
    expect(dueLabel(-3)).toBe('просрочено на 3 дня')
    expect(dueLabel(-12)).toBe('просрочено на 12 дней')
    expect(dueLabel(-21)).toBe('просрочено на 21 день')
  })

  it('everyLabel', () => {
    expect(everyLabel(1)).toBe('каждый день')
    expect(everyLabel(7)).toBe('раз в неделю')
    expect(everyLabel(3)).toBe('раз в 3 дня')
    expect(everyLabel(5)).toBe('раз в 5 дней')
    expect(everyLabel(21)).toBe('раз в 21 день')
    expect(everyLabel(14)).toBe('раз в 14 дней')
  })

  it('timeRange', () => {
    expect(timeRange('10:00', '11:30')).toBe('10:00–11:30')
    expect(timeRange('10:00', null)).toBe('10:00')
    expect(timeRange(null, '11:30')).toBe('')
    expect(timeRange(null, null)).toBe('')
  })

  it('dayMonth', () => {
    expect(dayMonth('2026-11-03')).toBe('3 ноября')
    expect(dayMonth('2026-01-15')).toBe('15 января')
    expect(dayMonth('2026-12-31')).toBe('31 декабря')
    expect(dayMonth('2028-02-29')).toBe('29 февраля')
  })

  it('longDate', () => {
    expect(longDate('2026-10-29')).toBe('Четверг, 29 октября')
    expect(longDate('2026-11-01')).toBe('Воскресенье, 1 ноября')
    expect(longDate('2026-10-26')).toBe('Понедельник, 26 октября')
  })

  it('dayHeading is relative to today', () => {
    expect(dayHeading('2026-10-29', TODAY)).toBe('Сегодня · 29 октября')
    expect(dayHeading('2026-10-30', TODAY)).toBe('Завтра · 30 октября')
    expect(dayHeading('2026-10-28', TODAY)).toBe('Вчера · 28 октября')
    expect(dayHeading('2026-10-31', TODAY)).toBe('Сб · 31 октября')
    expect(dayHeading('2026-11-06', TODAY)).toBe('Пт · 6 ноября')
    expect(dayHeading('2026-10-27', TODAY)).toBe('Вт · 27 октября')
    expect(dayHeading('2026-11-01', TODAY)).toBe('Вс · 1 ноября')
  })

  it('dayHeading across a month end', () => {
    expect(dayHeading('2026-11-01', '2026-10-31')).toBe('Завтра · 1 ноября')
    expect(dayHeading('2026-10-31', '2026-11-01')).toBe('Вчера · 31 октября')
  })

  it('weekLabel: this, next and previous week', () => {
    expect(weekLabel(TODAY, 0)).toBe('Эта неделя')
    expect(weekLabel(TODAY, 1)).toBe('Следующая неделя')
    expect(weekLabel(TODAY, -1)).toBe('Прошлая неделя')
  })

  it('weekLabel: a range within one month', () => {
    expect(weekLabel(TODAY, 2)).toBe('9 – 15 ноября')
    expect(weekLabel(TODAY, -2)).toBe('12 – 18 октября')
  })

  it('weekLabel: a range across two months', () => {
    expect(weekLabel('2026-10-15', 2)).toBe('26 октября – 1 ноября')
    expect(weekLabel('2026-12-24', 2)).toBe('4 – 10 января')
    expect(weekLabel('2026-12-24', 1 + 1)).toBe('4 – 10 января')
    expect(weekLabel('2026-12-17', 2)).toBe('28 декабря – 3 января')
  })
})

describe('agendaLabel / agendaDoneKind', () => {
  it('slot: area label and place', () => {
    expect(agendaLabel(agendaItem({ kind: 'slot', area: 'study', place: 'А-101' }))).toBe('Учёба · А-101')
    expect(agendaLabel(agendaItem({ kind: 'slot', area: 'work' }))).toBe('Работа')
    expect(agendaLabel(agendaItem({ kind: 'slot' }))).toBe('Расписание')
    expect(agendaLabel(agendaItem({ kind: 'slot', area: 'weird' }))).toBe('Расписание')
    expect(agendaLabel(agendaItem({ kind: 'slot', place: 'Дом' }))).toBe('Расписание · Дом')
  })

  it('event: area label, "План" without an area', () => {
    expect(agendaLabel(agendaItem({ kind: 'event', area: 'other' }))).toBe('Личное')
    expect(agendaLabel(agendaItem({ kind: 'event', area: 'work', place: 'Офис' }))).toBe('Работа · Офис')
    expect(agendaLabel(agendaItem({ kind: 'event' }))).toBe('План')
    expect(agendaLabel(agendaItem({ kind: 'event', place: 'Кафе' }))).toBe('План · Кафе')
  })

  it('deadline', () => {
    expect(agendaLabel(agendaItem({ kind: 'deadline' }))).toBe('Дедлайн')
    expect(agendaLabel(agendaItem({ kind: 'deadline', area: 'study', place: 'x' }))).toBe('Дедлайн')
  })

  it('chore: due today or overdue', () => {
    expect(agendaLabel(agendaItem({ kind: 'chore', overdueDays: 0 }))).toBe('Дом · сегодня')
    expect(agendaLabel(agendaItem({ kind: 'chore', overdueDays: 1 }))).toBe('Дом · просрочено на 1 день')
    expect(agendaLabel(agendaItem({ kind: 'chore', overdueDays: 2 }))).toBe('Дом · просрочено на 2 дня')
    expect(agendaLabel(agendaItem({ kind: 'chore', overdueDays: 5 }))).toBe('Дом · просрочено на 5 дней')
  })

  it('agendaDoneKind: no /done for slots, plural paths for the rest', () => {
    expect(agendaDoneKind('slot')).toBeNull()
    expect(agendaDoneKind('event')).toBe('events')
    expect(agendaDoneKind('deadline')).toBe('deadlines')
    expect(agendaDoneKind('chore')).toBe('chores')
    expect(AGENDA_PATH).toEqual({ slot: 'events', event: 'events', deadline: 'deadlines', chore: 'chores' })
  })
})

describe('selections', () => {
  it('soonDeadlines: open ones within the window, overdue included, soonest first', () => {
    const list = [
      deadline({ id: 1, day: '2026-10-29', title: 'Физика' }),
      deadline({ id: 2, day: '2026-10-29', title: 'Алгебра' }),
      deadline({ id: 3, day: '2026-10-27', title: 'Просрочено' }),
      deadline({ id: 4, day: '2026-11-01', title: 'Через три' }),
      deadline({ id: 5, day: '2026-11-02', title: 'Через четыре' }),
      deadline({ id: 6, day: '2026-10-20', title: 'Сдано', done: true }),
    ]
    const soon = soonDeadlines(list, TODAY)
    expect(soon.map((d) => d.id)).toEqual([3, 2, 1, 4])
    expect(soon.map((d) => d.days)).toEqual([-2, 0, 0, 3])
  })

  it('soonDeadlines: custom window and the other fields are kept', () => {
    const list = [deadline({ id: 1, day: '2026-11-02', title: 'x', course: 'Физика' }), deadline({ id: 2, day: '2026-10-29' })]
    expect(soonDeadlines(list, TODAY, 0).map((d) => d.id)).toEqual([2])
    expect(soonDeadlines(list, TODAY, 4)[1]).toEqual({ ...list[0], days: 4 })
    expect(soonDeadlines([], TODAY)).toEqual([])
  })

  it('soonDeadlines does not mutate its input', () => {
    const list = [deadline({ id: 1, day: '2026-11-01' }), deadline({ id: 2, day: '2026-10-29' })]
    soonDeadlines(list, TODAY)
    expect(list.map((d) => d.id)).toEqual([1, 2])
  })

  it('openDeadlines: every open deadline, however far, soonest first', () => {
    const list = [
      deadline({ id: 1, day: '2027-03-01' }),
      deadline({ id: 2, day: '2026-11-02', title: 'Б' }),
      deadline({ id: 3, day: '2026-10-25' }),
      deadline({ id: 4, day: '2026-10-30', done: true }),
    ]
    const open = openDeadlines(list, TODAY)
    expect(open.map((d) => d.id)).toEqual([3, 2, 1])
    expect(open.map((d) => d.days)).toEqual([-4, 4, 123])
  })

  it('sortChores: most overdue first, then by title, without mutating', () => {
    const list = [
      chore({ id: 1, title: 'Б', left: 0 }),
      chore({ id: 2, title: 'В', left: 2 }),
      chore({ id: 3, title: 'А', left: 0 }),
      chore({ id: 4, title: 'Г', left: -3 }),
    ]
    expect(sortChores(list).map((c) => c.id)).toEqual([4, 3, 1, 2])
    expect(list.map((c) => c.id)).toEqual([1, 2, 3, 4])
  })

  it('dueChores: only due today or overdue', () => {
    const list = [
      chore({ id: 1, title: 'Б', left: 0 }),
      chore({ id: 2, title: 'В', left: 2 }),
      chore({ id: 3, title: 'А', left: -1 }),
      chore({ id: 4, title: 'Г', left: 1 }),
    ]
    expect(dueChores(list).map((c) => c.id)).toEqual([3, 1])
    expect(dueChores([])).toEqual([])
  })

  it('openTasks keeps the server order', () => {
    const list = [task({ id: 3 }), task({ id: 1, done: true }), task({ id: 2 }), task({ id: 5, done: true }), task({ id: 4 })]
    expect(openTasks(list).map((t) => t.id)).toEqual([3, 2, 4])
  })

  it('doneTasks: newest doneAt first, missing doneAt last, ties by id desc', () => {
    const list = [
      task({ id: 1, done: true, doneAt: '2026-10-27T10:00:00Z' }),
      task({ id: 2, done: true, doneAt: '2026-10-29T08:00:00Z' }),
      task({ id: 3, done: true, doneAt: null }),
      task({ id: 4, done: true, doneAt: '2026-10-28T10:00:00Z' }),
      task({ id: 5, done: true, doneAt: '2026-10-28T10:00:00Z' }),
      task({ id: 6, done: false }),
    ]
    expect(doneTasks(list).map((t) => t.id)).toEqual([2, 5, 4, 1, 3])
  })

  it('projectGroups: work, learning, own order; empty groups dropped; active first, then by name', () => {
    const list = [
      project({ id: 1, name: 'Свой Б', kind: 'own', status: 'done' }),
      project({ id: 2, name: 'Свой А', kind: 'own', status: 'paused' }),
      project({ id: 3, name: 'Работа Я', kind: 'work', status: 'active' }),
      project({ id: 4, name: 'Работа Б', kind: 'work', status: 'idea' }),
      project({ id: 5, name: 'Работа А', kind: 'work', status: 'active' }),
      project({ id: 6, name: 'Свой В', kind: 'own', status: 'active' }),
    ]
    const groups = projectGroups(list)
    expect(groups.map((g) => [g.kind, g.label])).toEqual([
      ['work', 'Работа'],
      ['own', 'Своё'],
    ])
    expect(groups[0]!.projects.map((p) => p.id)).toEqual([5, 3, 4])
    expect(groups[1]!.projects.map((p) => p.id)).toEqual([6, 2, 1])
  })

  it('projectGroups: all three groups with labels, empty list gives none', () => {
    const groups = projectGroups([
      project({ id: 1, kind: 'own' }),
      project({ id: 2, kind: 'learning' }),
      project({ id: 3, kind: 'work' }),
    ])
    expect(groups.map((g) => g.kind)).toEqual(['work', 'learning', 'own'])
    expect(groups.map((g) => g.label)).toEqual(['Работа', 'Обучение', 'Своё'])
    expect(projectGroups([])).toEqual([])
  })

  it('projectGroups: statuses rank active, paused, idea, done', () => {
    const list = [
      project({ id: 1, name: 'А', status: 'done' }),
      project({ id: 2, name: 'А', status: 'idea' }),
      project({ id: 3, name: 'А', status: 'paused' }),
      project({ id: 4, name: 'А', status: 'active' }),
    ]
    expect(projectGroups(list)[0]!.projects.map((p) => p.id)).toEqual([4, 3, 2, 1])
  })

  it('sortNotes: updatedAt desc, missing last, ties by id desc, without mutating', () => {
    const list = [
      noteHead({ id: 1, updatedAt: '2026-10-27T10:00:00Z' }),
      noteHead({ id: 2, updatedAt: '2026-10-29T10:00:00Z' }),
      noteHead({ id: 3, updatedAt: null }),
      noteHead({ id: 4, updatedAt: '2026-10-27T10:00:00Z' }),
    ]
    expect(sortNotes(list).map((n) => n.id)).toEqual([2, 4, 1, 3])
    expect(list.map((n) => n.id)).toEqual([1, 2, 3, 4])
  })

  it('inAgenda matches kind and id together', () => {
    const agenda = [agendaItem({ kind: 'event', id: 5 }), agendaItem({ kind: 'chore', id: 7 })]
    expect(inAgenda(agenda, 'event', 5)).toBe(true)
    expect(inAgenda(agenda, 'chore', 7)).toBe(true)
    expect(inAgenda(agenda, 'chore', 5)).toBe(false)
    expect(inAgenda(agenda, 'event', 6)).toBe(false)
    expect(inAgenda([], 'event', 5)).toBe(false)
  })
})

describe('scheduleWeek', () => {
  const dates = weekDates(TODAY)

  it('returns one day per date with the isToday flag', () => {
    const days = scheduleWeek({ slots: [], events: [], deadlines: [] }, dates, TODAY)
    expect(days.map((d) => d.date)).toEqual(dates)
    expect(days.map((d) => d.isToday)).toEqual([false, false, false, true, false, false, false])
    expect(days.every((d) => d.items.length === 0)).toBe(true)
  })

  it('places slots by weekday, and events and deadlines by date', () => {
    const days = scheduleWeek(
      {
        slots: [slot({ id: 1, weekday: 1, title: 'Матан' }), slot({ id: 2, weekday: 4, title: 'Физика' })],
        events: [event({ id: 3, day: '2026-10-27', title: 'Созвон' }), event({ id: 4, day: '2026-11-02', title: 'Другая неделя' })],
        deadlines: [deadline({ id: 5, day: '2026-10-29', title: 'Лаба' })],
      },
      dates,
      TODAY,
    )
    const titles = (i: number) => days[i]!.items.map((x) => x.title)
    expect(titles(0)).toEqual(['Матан'])
    expect(titles(1)).toEqual(['Созвон'])
    expect(titles(2)).toEqual([])
    expect(titles(3)).toEqual(['Физика', 'Лаба'])
    expect(days.flatMap((d) => d.items).some((x) => x.title === 'Другая неделя')).toBe(false)
  })

  it('repeats a weekly slot in every week it is asked for', () => {
    const next = scheduleWeek({ slots: [slot({ id: 1, weekday: 1 })], events: [], deadlines: [] }, weekDates(TODAY, 1), TODAY)
    expect(next[0]!.date).toBe('2026-11-02')
    expect(next[0]!.items.map((x) => x.id)).toEqual([1])
    expect(next[0]!.isToday).toBe(false)
  })

  it('sorts items by start with untimed last (stable: slots, events, deadlines)', () => {
    const days = scheduleWeek(
      {
        slots: [slot({ id: 1, weekday: 4, title: 'Слот 10', start: '10:00' }), slot({ id: 2, weekday: 4, title: 'Слот без времени', start: null })],
        events: [event({ id: 3, title: 'Событие 09', start: '09:00' }), event({ id: 4, title: 'Событие без времени', start: null })],
        deadlines: [deadline({ id: 5, title: 'Дедлайн' })],
      },
      dates,
      TODAY,
    )
    expect(days[3]!.items.map((x) => x.title)).toEqual([
      'Событие 09',
      'Слот 10',
      'Слот без времени',
      'Событие без времени',
      'Дедлайн',
    ])
  })

  it('builds item fields and sub labels', () => {
    const days = scheduleWeek(
      {
        slots: [
          slot({ id: 1, weekday: 4, start: '10:00', end: '11:30', title: 'Матан', area: 'study', place: 'А-101' }),
          slot({ id: 2, weekday: 4, start: '12:00', title: 'Смена', area: 'work', place: null }),
        ],
        events: [
          event({ id: 3, start: '09:00', end: '09:30', title: 'Созвон', area: 'work', done: true }),
          event({ id: 4, title: 'Врач', area: 'other' }),
          event({ id: 5, title: 'Экзамен', area: 'study' }),
        ],
        deadlines: [
          deadline({ id: 6, title: 'Лаба', course: 'Физика', done: true }),
          deadline({ id: 7, title: 'Эссе', course: null }),
        ],
      },
      dates,
      TODAY,
    )
    const items = days[3]!.items
    const by = (id: number, kind: string) => items.find((x) => x.id === id && x.kind === kind)
    expect(by(1, 'slot')).toEqual({ kind: 'slot', id: 1, title: 'Матан', start: '10:00', end: '11:30', sub: 'Учёба · А-101', done: false })
    expect(by(2, 'slot')!.sub).toBe('Работа')
    expect(by(3, 'event')).toEqual({ kind: 'event', id: 3, title: 'Созвон', start: '09:00', end: '09:30', sub: 'План · Работа', done: true })
    expect(by(4, 'event')!.sub).toBe('План · Личное')
    expect(by(5, 'event')!.sub).toBe('План · Учёба')
    expect(by(6, 'deadline')).toEqual({ kind: 'deadline', id: 6, title: 'Лаба', start: null, end: null, sub: 'Дедлайн · Физика', done: true })
    expect(by(7, 'deadline')!.sub).toBe('Дедлайн')
    expect(by(7, 'deadline')!.done).toBe(false)
  })

  it('does not mutate the view lists', () => {
    const slots = [slot({ id: 1, weekday: 4, start: '12:00' }), slot({ id: 2, weekday: 4, start: '09:00' })]
    scheduleWeek({ slots, events: [], deadlines: [] }, dates, TODAY)
    expect(slots.map((s) => s.id)).toEqual([1, 2])
  })
})

describe('formValues', () => {
  it('a new item gets the kind defaults', () => {
    expect(formValues('slots', null).area).toBe('study')
    expect(formValues('events', null).area).toBe('other')
    expect(formValues('chores', null).everyDays).toBe('7')
    expect(formValues('projects', null)).toMatchObject({ kind: 'own', status: 'active', name: '' })
    expect(formValues('tasks', null)).toEqual({ title: '', tag: '', projectId: '' })
  })

  it('a new item takes caller defaults over the kind defaults', () => {
    expect(formValues('events', null, { day: '2026-10-30', area: 'work' })).toMatchObject({ day: '2026-10-30', area: 'work', title: '' })
    expect(formValues('tasks', null, { projectId: '4' }).projectId).toBe('4')
    expect(formValues('chores', null, { everyDays: '3' }).everyDays).toBe('3')
  })

  it('an edit stringifies the values; null becomes ""', () => {
    const e = event({ id: 9, day: '2026-10-30', start: '09:00', end: null, title: 'Созвон', area: 'work', projectId: 5, remindBefore: 0, note: null })
    expect(formValues('events', e)).toEqual({
      title: 'Созвон',
      area: 'work',
      day: '2026-10-30',
      start: '09:00',
      end: '',
      remindBefore: '0',
      projectId: '5',
      note: '',
    })
    expect(formValues('slots', slot({ weekday: 3 })).weekday).toBe('3')
    expect(formValues('chores', chore({ everyDays: 14, lastDone: null }))).toMatchObject({ everyDays: '14', lastDone: '' })
  })

  it('an edit joins a tags array with ", " and ignores defaults', () => {
    const n = { ...noteHead({ title: 'Идеи', tags: ['a', 'b'] }), body: '# x' }
    expect(formValues('notes', n, { title: 'ignored' })).toEqual({ title: 'Идеи', body: '# x', tags: 'a, b', projectId: '' })
    expect(formValues('notes', { ...n, tags: [] }).tags).toBe('')
    // an edit does not take the kind defaults either
    expect(formValues('chores', chore({ everyDays: 3 }), { everyDays: '9' }).everyDays).toBe('3')
  })
})

describe('formBody: new item', () => {
  const newSlot = { title: 'Матан', area: 'study', weekday: '3', start: '10:00', end: '', place: '', note: '' }

  it('sends typed filled fields and omits empty optional ones', () => {
    expect(formBody('slots', newSlot, null)).toEqual({ body: { title: 'Матан', area: 'study', weekday: 3, start: '10:00' }, error: null })
    expect(formBody('slots', { ...newSlot, end: '11:30', place: ' А-101 ' }, null).body).toEqual({
      title: 'Матан',
      area: 'study',
      weekday: 3,
      start: '10:00',
      end: '11:30',
      place: 'А-101',
    })
  })

  it('trims text values', () => {
    expect(formBody('tasks', { title: '  Резюме  ', tag: ' работа ', projectId: '' }, null).body).toEqual({ title: 'Резюме', tag: 'работа' })
  })

  it('reports a missing required field with its label', () => {
    expect(formBody('tasks', { title: '', tag: '', projectId: '' }, null)).toEqual({ body: {}, error: 'Заполни «Задача»' })
    expect(formBody('tasks', { title: '   ' }, null).error).toBe('Заполни «Задача»')
    expect(formBody('slots', { ...newSlot, start: '' }, null).error).toBe('Заполни «Начало»')
    expect(formBody('slots', { ...newSlot, weekday: '' }, null).error).toBe('Заполни «День недели»')
    expect(formBody('events', { title: 'x', area: 'other', day: '' }, null).error).toBe('Заполни «Дата»')
    expect(formBody('chores', { title: 'Уборка', everyDays: '' }, null).error).toBe('Заполни «Раз в сколько дней»')
  })

  it('rejects a malformed time', () => {
    expect(formBody('slots', { ...newSlot, start: '9:5' }, null)).toEqual({ body: {}, error: '«Начало»: время ЧЧ:ММ' })
    expect(formBody('slots', { ...newSlot, start: '25:00' }, null).error).toBe('«Начало»: время ЧЧ:ММ')
    expect(formBody('slots', { ...newSlot, start: '10:00:00' }, null).error).toBe('«Начало»: время ЧЧ:ММ')
    expect(formBody('slots', { ...newSlot, end: '9:30' }, null).error).toBe('«Конец»: время ЧЧ:ММ')
  })

  it('rejects a malformed date', () => {
    expect(formBody('events', { title: 'x', area: 'other', day: '30.10.2026' }, null)).toEqual({ body: {}, error: '«Дата»: дата ГГГГ-ММ-ДД' })
    expect(formBody('chores', { title: 'x', everyDays: '7', lastDone: '2026-1-5' }, null).error).toBe('«Последний раз»: дата ГГГГ-ММ-ДД')
  })

  it('weekday must be an integer from 1 to 7', () => {
    for (const bad of ['0', '8', '-1', '1.5', 'abc']) {
      expect(formBody('slots', { ...newSlot, weekday: bad }, null)).toEqual({ body: {}, error: '«День недели»: число от 1 до 7' })
    }
    expect(formBody('slots', { ...newSlot, weekday: '1' }, null).body.weekday).toBe(1)
    expect(formBody('slots', { ...newSlot, weekday: '7' }, null).body.weekday).toBe(7)
  })

  it('int fields respect their range (chores everyDays 1..365)', () => {
    const c = { title: 'Уборка', everyDays: '7', lastDone: '' }
    expect(formBody('chores', c, null)).toEqual({ body: { title: 'Уборка', everyDays: 7 }, error: null })
    expect(formBody('chores', { ...c, everyDays: '0' }, null)).toEqual({ body: {}, error: '«Раз в сколько дней»: число от 1 до 365' })
    expect(formBody('chores', { ...c, everyDays: '366' }, null).error).toBe('«Раз в сколько дней»: число от 1 до 365')
    expect(formBody('chores', { ...c, everyDays: '2.5' }, null).error).toBe('«Раз в сколько дней»: число от 1 до 365')
    expect(formBody('chores', { ...c, everyDays: 'x' }, null).error).toBe('«Раз в сколько дней»: число от 1 до 365')
    expect(formBody('chores', { ...c, everyDays: '365' }, null).body.everyDays).toBe(365)
  })

  it('remindBefore 0 is sent, a negative or too large one is rejected', () => {
    const e = { title: 'x', area: 'other', day: TODAY }
    expect(formBody('events', { ...e, remindBefore: '0' }, null).body).toEqual({ title: 'x', area: 'other', day: TODAY, remindBefore: 0 })
    expect(formBody('events', { ...e, remindBefore: '-5' }, null).error).toBe('«Напомнить за, мин»: число от 0 до 1440')
    expect(formBody('events', { ...e, remindBefore: '1441' }, null).error).toBe('«Напомнить за, мин»: число от 0 до 1440')
    expect(formBody('events', { ...e, remindBefore: '' }, null).body).not.toHaveProperty('remindBefore')
  })

  it('project id must be a positive integer, without an upper bound in the message', () => {
    expect(formBody('tasks', { title: 'x', projectId: '5' }, null).body).toEqual({ title: 'x', projectId: 5 })
    expect(formBody('tasks', { title: 'x', projectId: '0' }, null)).toEqual({ body: {}, error: '«Проект»: число' })
    expect(formBody('tasks', { title: 'x', projectId: 'abc' }, null).error).toBe('«Проект»: число')
  })

  it('splits, trims and dedupes tags; empty tags are omitted', () => {
    const base = { title: 'Идеи', body: '', tags: '', projectId: '' }
    expect(formBody('notes', { ...base, tags: ' a, b ,a,, c ,b' }, null).body).toEqual({ title: 'Идеи', tags: ['a', 'b', 'c'] })
    expect(formBody('notes', { ...base, tags: ' , ,' }, null).body).toEqual({ title: 'Идеи' })
    expect(formBody('notes', base, null).body).toEqual({ title: 'Идеи' })
  })

  it('textarea keeps inner line breaks and indentation, trimming the outer blank space', () => {
    const base = { title: 'Заметка', body: '', tags: '', projectId: '' }
    expect(formBody('notes', { ...base, body: '\n\n# Title\n  - item\n\ntext  \n\n' }, null).body).toEqual({
      title: 'Заметка',
      body: '# Title\n  - item\n\ntext',
    })
    expect(formBody('notes', { ...base, body: '  \n \n' }, null).body).toEqual({ title: 'Заметка' })
  })

  it('treats a missing value key as empty', () => {
    expect(formBody('tasks', { title: 'x' }, null)).toEqual({ body: { title: 'x' }, error: null })
  })

  it('reports an end before the start', () => {
    const e = { title: 'Созвон', area: 'other', day: TODAY }
    expect(formBody('events', { ...e, start: '11:00', end: '10:00' }, null)).toEqual({ body: {}, error: 'Конец раньше начала' })
    expect(formBody('events', { ...e, start: '10:00', end: '10:00' }, null).error).toBe('Конец раньше начала')
    expect(formBody('events', { ...e, start: '10:00', end: '10:01' }, null).error).toBeNull()
    expect(formBody('events', { ...e, start: '', end: '10:00' }, null).error).toBeNull()
    expect(formBody('events', { ...e, start: '10:00', end: '' }, null).error).toBeNull()
  })
})

describe('formBody: stored empty values', () => {
  it('an untouched edit of a note with an empty body sends nothing', () => {
    const r = formBody('notes', { title: 't', body: '', tags: '', projectId: '' }, { title: 't', body: '', tags: [], projectId: null })
    expect(r).toEqual({ body: {}, error: null })
  })

  it('renaming a note with an empty body sends only the title', () => {
    const r = formBody('notes', { title: 'new', body: '', tags: '', projectId: '' }, { title: 't', body: '', tags: [], projectId: null })
    expect(r).toEqual({ body: { title: 'new' }, error: null })
  })
})

describe('formBody: edit', () => {
  const orig = event({ id: 9, day: '2026-10-30', start: '10:00', end: '11:00', title: 'Созвон', area: 'work', projectId: 5, remindBefore: 15, note: 'заметка' })
  const values = () => formValues('events', orig)

  it('an unchanged form gives an empty body', () => {
    expect(formBody('events', values(), orig)).toEqual({ body: {}, error: null })
  })

  it('an unchanged form with nulls and empty tags gives an empty body', () => {
    const bare = event({})
    expect(formBody('events', formValues('events', bare), bare)).toEqual({ body: {}, error: null })
    const n = { ...noteHead({ tags: [] }), body: 'text' }
    expect(formBody('notes', formValues('notes', n), n)).toEqual({ body: {}, error: null })
    const n2 = { ...noteHead({ tags: ['a', 'b'], projectId: 2 }), body: '# x\n- y' }
    expect(formBody('notes', formValues('notes', n2), n2)).toEqual({ body: {}, error: null })
  })

  it('sends only the changed keys', () => {
    expect(formBody('events', { ...values(), title: 'Новый' }, orig).body).toEqual({ title: 'Новый' })
    expect(formBody('events', { ...values(), area: 'study', remindBefore: '20' }, orig).body).toEqual({ area: 'study', remindBefore: 20 })
    expect(formBody('events', { ...values(), title: '  Созвон  ' }, orig).body).toEqual({})
  })

  it('clearing an optional field sends null', () => {
    expect(formBody('events', { ...values(), note: '' }, orig).body).toEqual({ note: null })
    expect(formBody('events', { ...values(), projectId: '', remindBefore: '' }, orig).body).toEqual({ projectId: null, remindBefore: null })
    expect(formBody('events', { ...values(), end: '' }, orig).body).toEqual({ end: null })
  })

  it('clearing a required field is an error', () => {
    expect(formBody('events', { ...values(), title: '' }, orig)).toEqual({ body: {}, error: 'Заполни «Что»' })
  })

  it('setting a field that was null sends the value', () => {
    const o = event({ note: null, projectId: null, remindBefore: null })
    expect(formBody('events', { ...formValues('events', o), note: 'x', projectId: '3', remindBefore: '0' }, o).body).toEqual({
      note: 'x',
      projectId: 3,
      remindBefore: 0,
    })
  })

  it('tags: only changes are sent, clearing sends []', () => {
    const n = { ...noteHead({ tags: ['a', 'b'] }), body: 'text' }
    const v = formValues('notes', n)
    expect(formBody('notes', { ...v, tags: 'b, a' }, n).body).toEqual({ tags: ['b', 'a'] })
    expect(formBody('notes', { ...v, tags: 'a, b, c' }, n).body).toEqual({ tags: ['a', 'b', 'c'] })
    expect(formBody('notes', { ...v, tags: '' }, n).body).toEqual({ tags: [] })
    expect(formBody('notes', { ...v, tags: 'a,b' }, n).body).toEqual({})
  })

  it('end before start when both change', () => {
    expect(formBody('events', { ...values(), start: '12:00', end: '11:30' }, orig)).toEqual({ body: {}, error: 'Конец раньше начала' })
  })

  it('end before the original start when only `end` changes', () => {
    expect(formBody('events', { ...values(), end: '09:30' }, orig)).toEqual({ body: {}, error: 'Конец раньше начала' })
    expect(formBody('events', { ...values(), end: '10:00' }, orig).error).toBe('Конец раньше начала')
    expect(formBody('events', { ...values(), end: '12:00' }, orig)).toEqual({ body: { end: '12:00' }, error: null })
  })

  it('start after the original end when only `start` changes', () => {
    expect(formBody('events', { ...values(), start: '11:30' }, orig)).toEqual({ body: {}, error: 'Конец раньше начала' })
    expect(formBody('events', { ...values(), start: '09:00' }, orig)).toEqual({ body: { start: '09:00' }, error: null })
  })

  it('clearing the start does not compare against the old start', () => {
    expect(formBody('events', { ...values(), start: '', end: '09:00' }, orig)).toEqual({ body: { start: null, end: '09:00' }, error: null })
    expect(formBody('events', { ...values(), start: '' }, orig)).toEqual({ body: { start: null }, error: null })
  })

  it('clearing the end does not compare either', () => {
    expect(formBody('events', { ...values(), start: '23:00', end: '' }, orig)).toEqual({ body: { start: '23:00', end: null }, error: null })
  })

  it('an untouched time pair is not re-validated', () => {
    const odd = event({ start: '11:00', end: '10:00' })
    expect(formBody('events', { ...formValues('events', odd), title: 'Новое' }, odd)).toEqual({ body: { title: 'Новое' }, error: null })
  })

  it('validates edited values like a new item', () => {
    expect(formBody('events', { ...values(), start: '9:5' }, orig).error).toBe('«Начало»: время ЧЧ:ММ')
    const c = chore({ everyDays: 7 })
    expect(formBody('chores', { ...formValues('chores', c), everyDays: '0' }, c).error).toBe('«Раз в сколько дней»: число от 1 до 365')
    expect(formBody('chores', { ...formValues('chores', c), everyDays: '3' }, c).body).toEqual({ everyDays: 3 })
  })
})

describe('safeHref', () => {
  it('accepts http and https only', () => {
    expect(safeHref('https://example.com')).toBe('https://example.com')
    expect(safeHref('http://example.com/a?b=1#c')).toBe('http://example.com/a?b=1#c')
    expect(safeHref('HTTPS://EXAMPLE.COM')).toBe('HTTPS://EXAMPLE.COM')
    expect(safeHref('  https://example.com  ')).toBe('https://example.com')
  })

  it('rejects every other scheme, relative and malformed URLs', () => {
    for (const bad of [
      'javascript:alert(1)',
      'JavaScript:alert(1)',
      ' javascript:alert(1)',
      'data:text/html,<script>alert(1)</script>',
      'vbscript:x',
      'file:///etc/passwd',
      'ftp://example.com',
      'mailto:a@b.c',
      '/relative/path',
      '//evil.example.com',
      'example.com',
      'https:example.com',
      'https://',
      'https://a b',
      'https://a\nb',
      '',
    ]) {
      expect(safeHref(bad)).toBeNull()
    }
  })
})

describe('parseInline', () => {
  const txt = (text: string): MdInline => ({ t: 'text', text })

  it('plain text stays one text node', () => {
    expect(parseInline('hello world')).toEqual([txt('hello world')])
    expect(parseInline('')).toEqual([])
  })

  it('parses `code`, keeping markers inside it literal', () => {
    expect(parseInline('use `npm i` now')).toEqual([txt('use '), { t: 'code', text: 'npm i' }, txt(' now')])
    expect(parseInline('`a**b** *c*`')).toEqual([{ t: 'code', text: 'a**b** *c*' }])
  })

  it('parses **bold** and *italic*', () => {
    expect(parseInline('**bold** and *it*')).toEqual([
      { t: 'bold', children: [txt('bold')] },
      txt(' and '),
      { t: 'italic', children: [txt('it')] },
    ])
  })

  it('nests italic inside bold', () => {
    expect(parseInline('**a *b* c**')).toEqual([{ t: 'bold', children: [txt('a '), { t: 'italic', children: [txt('b')] }, txt(' c')] }])
  })

  it('leaves lone asterisks around spaces as text', () => {
    expect(parseInline('2 * 3 * 4')).toEqual([txt('2 * 3 * 4')])
    expect(parseInline('a * b')).toEqual([txt('a * b')])
  })

  it('parses [text](https://x) links', () => {
    expect(parseInline('see [docs](https://x.com/a) now')).toEqual([
      txt('see '),
      { t: 'link', href: 'https://x.com/a', children: [txt('docs')] },
      txt(' now'),
    ])
    expect(parseInline('[**b**](http://x.com)')).toEqual([{ t: 'link', href: 'http://x.com', children: [{ t: 'bold', children: [txt('b')] }] }])
  })

  it('turns a bare https URL into a link and keeps trailing punctuation outside', () => {
    expect(parseInline('see https://example.com/a, ok')).toEqual([
      txt('see '),
      { t: 'link', href: 'https://example.com/a', children: [txt('https://example.com/a')] },
      txt(', ok'),
    ])
    expect(parseInline('go to https://x.com.')).toEqual([
      txt('go to '),
      { t: 'link', href: 'https://x.com', children: [txt('https://x.com')] },
      txt('.'),
    ])
    expect(parseInline('really https://x.com?!')).toEqual([
      txt('really '),
      { t: 'link', href: 'https://x.com', children: [txt('https://x.com')] },
      txt('?!'),
    ])
  })

  it('keeps punctuation inside a URL and stops at brackets and quotes', () => {
    expect(parseInline('http://x.com/?a=1&b=2')).toEqual([
      { t: 'link', href: 'http://x.com/?a=1&b=2', children: [txt('http://x.com/?a=1&b=2')] },
    ])
    expect(parseInline('(https://x.com)')).toEqual([txt('('), { t: 'link', href: 'https://x.com', children: [txt('https://x.com')] }, txt(')')])
  })

  it('does not make a link of [x](javascript:alert) and keeps only its label as text', () => {
    expect(parseInline('[x](javascript:alert)')).toEqual([txt('x')])
    expect(parseInline('[click me](JavaScript:alert)')).toEqual([txt('click me')])
  })

  it('does not make a link of [x](javascript:alert(1))', () => {
    const nodes = parseInline('[x](javascript:alert(1))')
    expect(nodes.every((n) => n.t === 'text')).toBe(true)
    expect(JSON.stringify(nodes)).not.toContain('"link"')
    expect(nodes[0]).toMatchObject({ t: 'text' })
    expect((nodes[0] as { text: string }).text.startsWith('x')).toBe(true)
  })

  it('does not make a link of data: or relative targets', () => {
    expect(parseInline('[x](data:text/html,hi)')).toEqual([txt('x')])
    expect(parseInline('[x](/admin)')).toEqual([txt('x')])
    expect(parseInline('[x](//evil.example.com)')).toEqual([txt('x')])
    expect(parseInline('[x](example.com)')).toEqual([txt('x')])
  })

  it('keeps HTML as literal text', () => {
    expect(parseInline('<script>alert(1)</script>')).toEqual([txt('<script>alert(1)</script>')])
    expect(parseInline('<img src=x onerror=alert(1)>')).toEqual([txt('<img src=x onerror=alert(1)>')])
  })

  it('HTML around a URL never becomes part of the href', () => {
    const nodes = parseInline('<a href="https://x.com">go</a>')
    const links = nodes.filter((n) => n.t === 'link')
    expect(links).toEqual([{ t: 'link', href: 'https://x.com', children: [txt('https://x.com')] }])
    expect(nodes.map((n) => n.t).every((t) => t === 'text' || t === 'link')).toBe(true)
  })

  it('does not link inside code', () => {
    expect(parseInline('`https://x.com`')).toEqual([{ t: 'code', text: 'https://x.com' }])
  })
})

describe('parseMarkdown', () => {
  const txt = (text: string): MdInline => ({ t: 'text', text })

  it('is empty for an empty or blank source', () => {
    expect(parseMarkdown('')).toEqual([])
    expect(parseMarkdown('  \n\n \n')).toEqual([])
  })

  it('headings, with the level clamped to 3', () => {
    expect(parseMarkdown('# H1\n## H2\n### H3\n#### H4\n###### H6')).toEqual([
      { t: 'h', level: 1, inline: [txt('H1')] },
      { t: 'h', level: 2, inline: [txt('H2')] },
      { t: 'h', level: 3, inline: [txt('H3')] },
      { t: 'h', level: 3, inline: [txt('H4')] },
      { t: 'h', level: 3, inline: [txt('H6')] },
    ])
  })

  it('heading: closing hashes dropped, inline parsed, "#" without space is a paragraph', () => {
    expect(parseMarkdown('## Title ##')).toEqual([{ t: 'h', level: 2, inline: [txt('Title')] }])
    expect(parseMarkdown('# **Hi**')).toEqual([{ t: 'h', level: 1, inline: [{ t: 'bold', children: [txt('Hi')] }] }])
    expect(parseMarkdown('#nospace')).toEqual([{ t: 'p', lines: [[txt('#nospace')]] }])
    expect(parseMarkdown('####### seven')).toEqual([{ t: 'p', lines: [[txt('####### seven')]] }])
  })

  it('paragraph: consecutive lines stay together, a blank line splits', () => {
    expect(parseMarkdown('a\nb')).toEqual([{ t: 'p', lines: [[txt('a')], [txt('b')]] }])
    expect(parseMarkdown('a\n\nb')).toEqual([
      { t: 'p', lines: [[txt('a')]] },
      { t: 'p', lines: [[txt('b')]] },
    ])
    expect(parseMarkdown('  indented text  ')).toEqual([{ t: 'p', lines: [[txt('indented text')]] }])
  })

  it('paragraph lines get inline parsing', () => {
    expect(parseMarkdown('**a** and `b`')).toEqual([{ t: 'p', lines: [[{ t: 'bold', children: [txt('a')] }, txt(' and '), { t: 'code', text: 'b' }]] }])
  })

  it('unordered lists are grouped across -, * and +', () => {
    expect(parseMarkdown('- a\n* b\n+ c')).toEqual([
      {
        t: 'ul',
        items: [
          { checked: null, inline: [txt('a')] },
          { checked: null, inline: [txt('b')] },
          { checked: null, inline: [txt('c')] },
        ],
      },
    ])
  })

  it('ordered lists are grouped across "1." and "2)"', () => {
    expect(parseMarkdown('1. a\n2) b\n12. c')).toEqual([
      {
        t: 'ol',
        items: [
          { checked: null, inline: [txt('a')] },
          { checked: null, inline: [txt('b')] },
          { checked: null, inline: [txt('c')] },
        ],
      },
    ])
  })

  it('switching between ul and ol starts a new list', () => {
    const blocks = parseMarkdown('- a\n1. b\n- c')
    expect(blocks.map((b) => b.t)).toEqual(['ul', 'ol', 'ul'])
  })

  it('a blank line ends a list', () => {
    const blocks = parseMarkdown('- a\n\n- b')
    expect(blocks.map((b) => b.t)).toEqual(['ul', 'ul'])
  })

  it('a paragraph line ends a list and a list line ends a paragraph', () => {
    expect(parseMarkdown('- a\ntext').map((b) => b.t)).toEqual(['ul', 'p'])
    expect(parseMarkdown('text\n- a').map((b) => b.t)).toEqual(['p', 'ul'])
    expect(parseMarkdown('- a\ntext\n- b').map((b) => b.t)).toEqual(['ul', 'p', 'ul'])
  })

  it('task list items carry checked flags', () => {
    expect(parseMarkdown('- [ ] a\n- [x] b\n- [X] c')).toEqual([
      {
        t: 'ul',
        items: [
          { checked: false, inline: [txt('a')] },
          { checked: true, inline: [txt('b')] },
          { checked: true, inline: [txt('c')] },
        ],
      },
    ])
    expect(parseMarkdown('1. [x] done')).toEqual([{ t: 'ol', items: [{ checked: true, inline: [txt('done')] }] }])
  })

  it('a malformed checkbox is plain item text', () => {
    const [list] = parseMarkdown('- [] a\n- [x]b') as { t: 'ul'; items: { checked: boolean | null; inline: MdInline[] }[] }[]
    expect(list!.items.map((i) => i.checked)).toEqual([null, null])
  })

  it('a number with four digits is not an ordered list', () => {
    expect(parseMarkdown('2026. was a year')).toEqual([{ t: 'p', lines: [[txt('2026. was a year')]] }])
  })

  it('"*italic* text" is a paragraph, not a list', () => {
    expect(parseMarkdown('*italic* text')).toEqual([{ t: 'p', lines: [[{ t: 'italic', children: [txt('italic')] }, txt(' text')]] }])
  })

  it('quotes group consecutive lines', () => {
    expect(parseMarkdown('> a\n> **b**\n>c')).toEqual([
      { t: 'quote', lines: [[txt('a')], [{ t: 'bold', children: [txt('b')] }], [txt('c')]] },
    ])
    expect(parseMarkdown('> a\n\n> b').map((b) => b.t)).toEqual(['quote', 'quote'])
    expect(parseMarkdown('> q\n- a').map((b) => b.t)).toEqual(['quote', 'ul'])
  })

  it('fenced code keeps its inner text verbatim, including ** and <b>', () => {
    expect(parseMarkdown('```\n**x** <b>y</b>\n  indented\n\n# not a heading\n```')).toEqual([
      { t: 'code', text: '**x** <b>y</b>\n  indented\n\n# not a heading' },
    ])
  })

  it('fence with a language tag and ~~~ fences', () => {
    expect(parseMarkdown('```js\nlet a = 1\n```')).toEqual([{ t: 'code', text: 'let a = 1' }])
    expect(parseMarkdown('~~~\nx\n~~~')).toEqual([{ t: 'code', text: 'x' }])
    expect(parseMarkdown('```\n```')).toEqual([{ t: 'code', text: '' }])
  })

  it('a fence is closed only by its own marker', () => {
    expect(parseMarkdown('```\na\n~~~\nb\n```')).toEqual([{ t: 'code', text: 'a\n~~~\nb' }])
  })

  it('an unterminated fence takes the rest of the source', () => {
    expect(parseMarkdown('before\n```\na\n- b\n\nc')).toEqual([
      { t: 'p', lines: [[txt('before')]] },
      { t: 'code', text: 'a\n- b\n\nc' },
    ])
    expect(parseMarkdown('```')).toEqual([{ t: 'code', text: '' }])
  })

  it('blocks after a fence continue normally', () => {
    expect(parseMarkdown('```\na\n```\nafter').map((b) => b.t)).toEqual(['code', 'p'])
    expect(parseMarkdown('- a\n```\nx\n```\n- b').map((b) => b.t)).toEqual(['ul', 'code', 'ul'])
  })

  it('"---" is a horizontal rule, not a list', () => {
    expect(parseMarkdown('---')).toEqual([{ t: 'hr' }])
    expect(parseMarkdown('***')).toEqual([{ t: 'hr' }])
    expect(parseMarkdown('___')).toEqual([{ t: 'hr' }])
    expect(parseMarkdown('- - -')).toEqual([{ t: 'hr' }])
    expect(parseMarkdown('a\n---\nb').map((b) => b.t)).toEqual(['p', 'hr', 'p'])
    expect(parseMarkdown('- a\n---\n- b').map((b) => b.t)).toEqual(['ul', 'hr', 'ul'])
  })

  it('"--" and "**bold**" are not rules', () => {
    expect(parseMarkdown('--').map((b) => b.t)).toEqual(['p'])
    expect(parseMarkdown('**bold**').map((b) => b.t)).toEqual(['p'])
  })

  it('reads CRLF and lone CR line endings like LF', () => {
    const crlf = 'a\r\nb\r\n\r\n- x\r\n- y\r\n\r\n```\r\nc\r\nd\r\n```'
    const lf = 'a\nb\n\n- x\n- y\n\n```\nc\nd\n```'
    expect(parseMarkdown(crlf)).toEqual(parseMarkdown(lf))
    expect(parseMarkdown(crlf)).toEqual([
      { t: 'p', lines: [[txt('a')], [txt('b')]] },
      { t: 'ul', items: [{ checked: null, inline: [txt('x')] }, { checked: null, inline: [txt('y')] }] },
      { t: 'code', text: 'c\nd' },
    ])
    expect(parseMarkdown('a\rb')).toEqual([{ t: 'p', lines: [[txt('a')], [txt('b')]] }])
  })

  it('keeps HTML as literal text in every block', () => {
    expect(parseMarkdown('<script>alert(1)</script>')).toEqual([{ t: 'p', lines: [[txt('<script>alert(1)</script>')]] }])
    expect(parseMarkdown('# <b>x</b>')).toEqual([{ t: 'h', level: 1, inline: [txt('<b>x</b>')] }])
    expect(parseMarkdown('- <i>x</i>')).toEqual([{ t: 'ul', items: [{ checked: null, inline: [txt('<i>x</i>')] }] }])
  })

  it('unsafe links inside blocks are not links either', () => {
    const [p] = parseMarkdown('[x](javascript:alert)') as { t: 'p'; lines: MdInline[][] }[]
    expect(p!.lines).toEqual([[txt('x')]])
    const [list] = parseMarkdown('- [x](data:text/html,hi)') as { t: 'ul'; items: { inline: MdInline[] }[] }[]
    expect(list!.items[0]!.inline).toEqual([txt('x')])
  })
})

describe('markDone', () => {
  const base = () => parseView(sample)!

  it('flips a task and stamps doneAt, back to null on undone', () => {
    const v = view({ tasks: [{ id: 4, title: 'Резюме', tag: null, projectId: null, done: false, doneAt: null }] })
    const id = 4
    const done = markDone(v, 'tasks', id, true)
    expect(done.tasks.find((t) => t.id === id)!.done).toBe(true)
    expect(done.tasks.find((t) => t.id === id)!.doneAt).not.toBeNull()
    const undone = markDone(done, 'tasks', id, false)
    expect(undone.tasks.find((t) => t.id === id)).toMatchObject({ done: false, doneAt: null })
    expect(v.tasks[0]!.done).toBe(false) // the input view is not mutated
  })

  it('marks a plan and its agenda point together', () => {
    const v = view({
      events: [{ id: 2, day: TODAY, start: '09:00', end: null, title: 'Созвон', area: 'work', projectId: null, remindBefore: null, done: false, note: null }],
      agenda: [{ kind: 'event', id: 2, title: 'Созвон', start: '09:00', end: null, area: 'work', place: null, done: false, overdueDays: 0 }],
    })
    const ev = v.agenda.find((a) => a.kind === 'event')!
    const next = markDone(v, 'events', ev.id, true)
    expect(next.events.find((e) => e.id === ev.id)!.done).toBe(true)
    expect(next.agenda.find((a) => a.kind === 'event' && a.id === ev.id)!.done).toBe(true)
  })

  it('a chore done today is due again in everyDays and leaves the agenda', () => {
    const v = base()
    const ch = v.agenda.find((a) => a.kind === 'chore')!
    const next = markDone(v, 'chores', ch.id, true)
    const chore = next.chores.find((c) => c.id === ch.id)!
    expect(chore.lastDone).toBe(TODAY)
    expect(chore.left).toBe(chore.everyDays)
    expect(next.agenda.some((a) => a.kind === 'chore' && a.id === ch.id)).toBe(false)
    expect(markDone(v, 'chores', ch.id, false)).toBe(v)
  })
})
