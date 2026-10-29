# Ассистент «Зал + Дела» — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** «Дела» (расписание, планы по датам, дедлайны, задачи, дела по дому, проекты, заметки) живут в базе бота, правятся словами (текст и голос) и в мини-аппе, бот напоминает и шлёт утреннюю сводку; зал не меняется.

**Architecture:** Новые таблицы `life_*` и сервис `services/life.py` (единственный писатель) — контракт для трёх независимых потоков: мозг бота (`life_gate`, модель «Дел», `handlers/life.py`, `kind=life` в разборщике, `ДЕЛА:` в ответе), напоминания (`services/life_reminders.py` в цикле `reminders`), мини-апп (раздел «Дела» на `/api/life/*`).

**Tech Stack:** Python 3.12, aiogram 3, FastAPI, SQLAlchemy 2 async + Alembic, pytest (xdist); React 19 + Vite + TS, vitest.

**Spec:** `docs/superpowers/specs/2026-10-29-life-assistant-design.md`

## Global Constraints

- Секреты только в `.env` через `gymbot.config.Settings`; никаких токенов в коде, логах, коммитах.
- Запись из свободного текста — только после кнопки; исходный текст логируется с применёнными действиями.
- Модели БД переносимые (SQLite → Postgres); изменение схемы = Alembic-миграция; миграции только добавляют.
- Даты в `TIMEZONE` (на проде `Asia/Tashkent`), моменты в UTC.
- Пользовательские тексты по-русски, код и комментарии по-английски.
- Проверки: `cd bot && ruff check src tests && pytest -q && pytest -q --shift-days 5`; `cd miniapp && npm run typecheck && npm run build && npm test`.
- **WSL ~7 ГБ:** агенты гоняют только свои тесты с `-n 2`; полный прогон и сборку мини-аппа делает оркестратор, не больше двух тяжёлых процессов одновременно.
- Зал не трогаем: все существующие тесты остаются зелёными без правки их ожиданий (кроме тестов `hub`, которые переезжают).

## Review Focus

1. Еда и тренировка со словами из гейта («купил продукты на ужин: курица 300 г», «позанимался в зале, жим 3×8») не должны уходить в «Дела»; модель «Дел» вернула 0 действий → обычный путь. Тест в задаче 2.3.
2. Дата «в субботу» в субботу, «завтра» после полуночи по Ташкенту: модель получает сегодняшнюю дату и день недели в `TIMEZONE`, код отбрасывает даты дальше ±400 дней. Тест в задаче 2.1.
3. Двойное нажатие «Применить» и устаревший черновик (рестарт) — ничего не создаётся дважды. Тест в задаче 2.3.
4. Напоминание не уходит дважды при двух тиках в одну минуту и после «+30 мин» уходит снова. Тест в задаче 3.1.
5. Перенос из `data.js` при повторном старте не дублирует записи; отсутствие `data.js` или владельца — не ошибка. Тест в задаче 1.2.

---

## Часть 1. Данные (последовательно, до остальных частей)

### Task 1.1: Таблицы и миграция

**Files:**
- Modify: `bot/src/gymbot/db/models.py` (в конец: `LifeSlot`, `LifeEvent`, `LifeDeadline`, `LifeTask`, `LifeChore`, `LifeProject`, `LifeNote`; в `User`: `life_remind_before: int = 30`, `life_summary_minute: int = 480`, `life_summary_enabled: bool = True`, `life_summary_sent_on: date | None`, `life_imported_at: datetime | None`)
- Create: `bot/migrations/versions/0017_life.py` (revision `0017`, down `0016`)
- Test: `bot/tests/test_life_service.py::test_migration_0017_round_trip`

**Interfaces — Produces:** ORM-классы с полями из спецификации, раздел 1. Общий миксин `LifeBase`-полей: `id`, `user_id` (FK users CASCADE, index), `source: str(16) = "me"`, `external_id: str(128) | None`, `created_at`, `updated_at`, `deleted_at | None`; `UniqueConstraint("user_id", "source", "external_id")` в каждой таблице. Строковые времена — `String(5)` "ЧЧ:ММ". `LifeNote.tags` — `JSON` список.

- [ ] Тест round-trip миграции (по образцу `tests/test_hub_state.py::test_migration_0016_round_trip`): upgrade head → все 7 таблиц и 5 колонок users есть; downgrade `0016` → таблиц нет, `hub_state` на месте; upgrade head.
- [ ] Модели + миграция (autogenerate-стиль как `0016_hub_state.py`, `server_default` для новых колонок users: `'30'`, `'480'`, `true`).
- [ ] Проверка расхождений модель↔миграция: `alembic.autogenerate.compare_metadata` пусто (скрипт из `docs`-практики, как в 0016).
- [ ] Обновить `tests/test_hub_state.py::test_migration_0016_round_trip` и `test_day_adjustments.py` (множество «таблицы поздних ревизий» += `life_*`).
- [ ] Commit.

### Task 1.2: Сервис `life` — представление, повестка, перенос

**Files:**
- Create: `bot/src/gymbot/services/life.py`
- Test: `bot/tests/test_life_service.py`

**Interfaces — Produces:**

```python
AREAS_SLOT = ("work", "study"); AREAS_EVENT = ("study", "work", "other")
PROJECT_KINDS = ("work", "learning", "own"); PROJECT_STATUSES = ("active", "paused", "idea", "done")
KINDS = ("slot", "event", "deadline", "task", "chore", "project", "note")
MODELS: dict[str, type]  # kind -> ORM class

@dataclass
class AgendaItem:
    kind: str            # "slot" | "event" | "deadline" | "chore"
    id: int
    title: str
    start: str | None    # "ЧЧ:ММ"
    end: str | None
    area: str | None     # work | study | other | None
    place: str | None
    done: bool
    overdue_days: int    # chores: days past due (0 = today); others 0

@dataclass
class LifeView:
    today: date
    slots: list[LifeSlot]; events: list[LifeEvent]; deadlines: list[LifeDeadline]
    tasks: list[LifeTask]; chores: list[LifeChore]; projects: list[LifeProject]; notes: list[LifeNote]  # notes: newest 30, body not needed

async def snapshot(session, user_id: int, today: date) -> LifeView
def chore_left(chore: LifeChore, today: date) -> int        # every_days - (today - last_done); 0 when never done
def day_agenda(view: LifeView, day: date) -> list[AgendaItem]  # sorted: timed by start, then untimed
def prompt_text(view: LifeView) -> str                      # compact text with ids for the models (section 2)
async def import_legacy(session, settings: Settings, user: User, now: datetime) -> int  # rows created; sets user.life_imported_at
```

- [ ] Тесты `day_agenda`: слоты по `weekday`, события дня, дедлайны дня, дела по дому со сроком ≤ 0 (`overdue_days`), сортировка по времени, удалённые (`deleted_at`) не видны.
- [ ] Тест `import_legacy` на реальном `data.js` (фикстура: копия `DATA_JS` из `tests/test_hub_tasks.py` + `lessons`, `study`, `projects` как в боевом файле): уроки → слоты `work` (place из `note`, `vld`), пары без `example` → `study`, примеры не переносятся, `todos` минус `removed` с `done`, `custom` → задачи, `chores` с `last_done`, проекты с `kind/status` по правилам спецификации; второй вызов → 0 строк; нет `data.js` → 0 и без ошибки, `life_imported_at` не ставится.
- [ ] Реализация. `import_legacy` берёт `hub_state` через `hub.get_state`, читалку `data.js` — из `hub_tasks._array` (вынести в `services/life_import.py` вместе с `_js_to_json`, чтобы не зависеть от удаляемого `hub_tasks`).
- [ ] Вызов `import_legacy` при старте в `main.py` после `sync_programs` (только если есть владелец и `life_imported_at IS NULL`).
- [ ] Commit.

### Task 1.3: Изменения через `life.apply` и API `/api/life`

**Files:**
- Create: `bot/src/gymbot/services/life_actions.py` (dataclass'ы действий, валидация словаря модели → действия; используется и API, и ботом)
- Modify: `bot/src/gymbot/services/life.py` (`apply`)
- Create: `bot/src/gymbot/api/life.py`
- Modify: `bot/src/gymbot/api/app.py` (подключить роутер до монтирования мини-аппа), `bot/src/gymbot/services/live.py` и `miniapp/src/liveCore.ts` (тема `life`)
- Test: `bot/tests/test_life_service.py`, `bot/tests/test_life_api.py`

**Interfaces — Produces:**

```python
@dataclass(frozen=True)
class Act:
    op: str                 # add | update | remove | done | undone | settings | write_note | edit_note
    entity: str | None      # one of life.KINDS (None for settings)
    id: int | None = None
    fields: dict[str, Any] = field(default_factory=dict)   # validated, typed values (date, "ЧЧ:ММ", str, int)

def parse(data: Any, view: LifeView, today: date) -> tuple[list[Act], list[str], dict | None]  # actions, notes, question
def validate_fields(entity: str, fields: dict, today: date, *, partial: bool) -> dict  # raises ValueError("…") in Russian
async def apply(session, user: User, acts: list[Act], now: datetime) -> list[str]   # human lines of what changed; commits; publishes "life"
def describe(acts: list[Act], view: LifeView, today: date) -> list[str]             # preview lines (spec section 2)
```

Поля по видам (ключи `fields`, всё необязательно для `update`):
- slot: `weekday` 1..7, `start`, `end`, `title`, `area`, `tag`, `place`, `vld`, `note`
- event: `day` (ГГГГ-ММ-ДД), `start`, `end`, `title`, `area`, `project_id`, `remind_before` 0..1440, `note`
- deadline: `day`, `title`, `course`, `project_id`
- task: `title`, `tag`, `project_id`
- chore: `title`, `every_days` 1..365, `last_done` (date)
- project: `name`, `kind`, `status`, `description`, `link`, `next_step`
- note: `title`, `body`, `tags` (list[str]), `project_id`
- settings: `remind_before`, `summary_time` "ЧЧ:ММ", `summary_enabled`

`done` для event/deadline/task ставит `done_at`, для chore — `last_done = fields.get("day", today)`; `undone` очищает. `remove` — мягкое (`deleted_at`). `write_note`/`edit_note` в `apply` не попадают — их тело пишет бот (часть 2) и превращает в `add`/`update` note.

API (`make_router(sessionmaker, settings, current_user: Callable[..., Awaitable[User]]) -> APIRouter`; `current_user` — зависимость из `create_app`, которая по initData находит/создаёт пользователя; владелец проверяется так же, как в остальном API, через `is_allowed`):
- `GET /api/life/view?date=` → `{today, slots, events, deadlines, tasks, chores, projects, notes, agenda}` (camelCase, у chores `left`).
- `POST /api/life/{kind}` (fields) → объект; `PATCH /api/life/{kind}/{id}` → объект; `DELETE /api/life/{kind}/{id}` → 204; `POST /api/life/{kind}/{id}/done` и `/undone` → объект; `GET /api/life/notes/{id}` → с `body`; `PUT /api/life/settings`.
- Ошибки валидации — 422 с русским текстом; чужой или удалённый id — 404.

**Точная форма JSON (контракт для мини-аппа, camelCase; даты "ГГГГ-ММ-ДД", время "ЧЧ:ММ", пустое = null):**

```json
{
  "today": "2026-10-29",
  "settings": {"remindBefore": 30, "summaryTime": "08:00", "summaryEnabled": true},
  "slots":     [{"id": 1, "weekday": 1, "start": "10:00", "end": null, "title": "ИЗ Математика", "area": "work", "tag": "math", "place": "Восход · куратор", "vld": "15:00", "note": null}],
  "events":    [{"id": 1, "day": "2026-10-29", "start": "11:00", "end": null, "title": "Позаниматься HTTP-сервером на C", "area": "study", "projectId": 3, "remindBefore": null, "done": false, "note": null}],
  "deadlines": [{"id": 1, "day": "2026-11-10", "title": "Лабораторная №1", "course": "Software Engineering", "projectId": null, "done": false}],
  "tasks":     [{"id": 1, "title": "Обновить резюме", "tag": "работа", "projectId": null, "done": false, "doneAt": null}],
  "chores":    [{"id": 1, "title": "Уборка", "everyDays": 7, "lastDone": "2026-10-25", "left": 3}],
  "projects":  [{"id": 1, "name": "AMGym", "kind": "work", "status": "active", "description": "…", "link": "https://…", "nextStep": null}],
  "notes":     [{"id": 1, "title": "Памятка по epoll", "tags": ["c"], "author": "bot", "projectId": 3, "updatedAt": "2026-10-29T10:00:00Z"}],
  "agenda":    [{"kind": "slot", "id": 1, "title": "ИЗ Математика", "start": "10:00", "end": null, "area": "work", "place": "Восход · куратор", "done": false, "overdueDays": 0}]
}
```

Тела `POST`/`PATCH` — те же ключи без `id` и вычисляемых (`done`, `doneAt`, `left`, `updatedAt`), например `POST /api/life/events {"day": "2026-10-30", "start": "15:00", "title": "Созвон", "area": "work"}`. `GET /api/life/notes/{id}` — объект заметки + `"body"`. `PUT /api/life/settings` — объект `settings` (частично). `POST …/{id}/done` у `chores` принимает необязательное `{"day": "…"}`.

- [ ] Тесты `parse`/`validate_fields`: неизвестный вид/поле отбрасывается с заметкой; дата дальше ±400 дней, «25:00», `weekday` 8 — ошибка; `update` с несуществующим id — заметка.
- [ ] Тесты `apply`: каждое действие на каждой таблице, мягкое удаление, `done` у chore с датой, публикация `life`.
- [ ] Тесты API: CRUD каждого вида, 401 без initData, 403 чужой пользователь при `ALLOWED_USER_IDS`, 404 чужой id, 422 кривое поле, `view.agenda` на дату.
- [ ] Реализация, `ruff`, свои тесты `-n 2`. Commit.

**Выкат части 1** (оркестратор): полный прогон, пуш в `main`, проверка миграции `0017` и переноса в журнале прода (`ssh gym-read`), `GET /api/life/view` без авторизации → 401.

---

## Часть 2. Мозг бота (агент, отдельное дерево)

### Task 2.1: Модель «Дел» и превью

**Files:**
- Create: `bot/src/gymbot/llm/prompts_life.py` (`LIFE_SYSTEM_PROMPT`, `build_life_messages(text, context, today_line) -> list[dict]`), `bot/src/gymbot/services/life_gate.py` (`is_life_command(text) -> bool`), `bot/src/gymbot/handlers/life.py`
- Delete: `bot/src/gymbot/handlers/hub.py`, `bot/src/gymbot/services/hub_tasks.py`, `bot/src/gymbot/llm/prompts_hub.py` (их тесты `tests/test_hub_tasks.py` переписать в `tests/test_life_bot.py`; `/api/hub/state` остаётся, его тесты не трогать)
- Modify: `bot/src/gymbot/handlers/log_text.py` (гейт `hub` → `life`), `bot/src/gymbot/main.py` (роутер `life`, команды `/life` и синоним `/hub`)
- Test: `bot/tests/test_life_bot.py`

**Interfaces — Consumes:** `life.snapshot`, `life.prompt_text`, `life_actions.parse/describe`, `life.apply`. **Produces:** `handlers.life.stage(message, text, raw, settings, sessionmaker, llm, *, forced=False) -> LifePending | None`, `handlers.life.send(message, pending, prefix="")`, `handlers.life.open_kb(settings, section="life")`.

Контекст модели: строка «Сегодня: 2026-10-29, четверг» в `TIMEZONE` + `life.prompt_text(view)`. Ответ модели — `{"actions": [{"op", "entity", "id", "fields"}], "question": null|{"text","options"}}`. Вопрос → сообщение с кнопками-вариантами; нажатие = повтор запроса с ответом.

- [ ] Тесты гейта (позитивные и негативные списки из спецификации; «купил протеин», «позанимался в зале», «съел гречку 200 г», «убрал жим из программы» — False).
- [ ] Тесты через `FakeLLM` (`tests/test_log_text.py`): «в учёбу добавь сегодня в 11 позаниматься HTTP-сервером на C» → превью «В учёбу: «Позаниматься HTTP-сервером на C» — сегодня в 11:00» → «Применить» → строка в `life_events`; второе нажатие — «Устарело»; «перенеси ИЗ Математику с понедельника на вторник» → update slot; модель вернула 0 действий → дальше разборщик; не владелец → None; дата «2099-01-01» отброшена с заметкой.
- [ ] Реализация (по образцу `handlers/hub.py` и `handlers/chat_edit.py`). Commit.

### Task 2.2: Памятки и заметки

**Files:** Modify `bot/src/gymbot/handlers/life.py`; Create `bot/src/gymbot/llm/prompts_note.py` (`build_note_messages(brief, draft: str | None, instruction: str | None) -> list[dict]`); Test `bot/tests/test_life_bot.py`.

- [ ] Тесты: «составь памятку по epoll» → `write_note` → второй запрос `complete_text` → превью «Памятка: …» с «Сохранить»; пока черновик открыт, «добавь пример» переписывает (третий запрос видит черновик и указание); «Сохранить» → `life_notes` с `author="bot"`; «запиши заметку: идея — статус прода рядом с задачами» → `add note` с телом как есть, `author="me"`; `edit_note` сохранённой заметки.
- [ ] Реализация (черновик в `LifePending.note_draft`; открытый черновик перехватывает следующее сообщение пользователя в `process_text` до гейтов, как открытый диалог разборщика). Commit.

### Task 2.3: Разборщик `kind=life`, ответ с «Делами», `ДЕЛА:`

**Files:** Modify `bot/src/gymbot/llm/schemas.py` (`kind` += `"life"`), `bot/src/gymbot/llm/prompts.py` (одна строка разборщику; правила ответа), `bot/src/gymbot/llm/structured.py` (схема Claude для `parse` — если там перечислены kind), `bot/src/gymbot/handlers/log_text.py` (`kind == "life"` → `life.stage(forced=True)`; `ДЕЛА:` под ответом), `bot/src/gymbot/services/answer.py` (`split_command` → `(text, command, kind)` где kind `edit|life`; блок «Дела» в `gather` через `life.day_agenda` на сегодня/завтра, открытые задачи, дедлайны ≤ 14 дней, проекты `learning` со `next_step`, заголовки заметок); Test `bot/tests/test_life_bot.py`, существующие `tests/test_log_text.py` и `tests/test_answer_*.py` не ломать.

- [ ] Тесты: разборщик отвечает `{"kind":"life"}` на сообщение без слов гейта → модель «Дел» → превью; 0 действий → «Не понял, что сделать в делах» с примером; ответ модели с последней строкой `ДЕЛА: добавь задачу купить билеты` → текст без строки + превью «Дел» под ним; `split_command("…\nПРАВКА: x")` по-прежнему `edit`; ответ на «что у меня завтра?» получает в контексте блок «Дела» (проверка тела запроса к модели).
- [ ] Отрицательные (Review Focus 1): «купил продукты на ужин: курица 300 г» (гейт ловит «купил продукты», модель «Дел» → 0 действий → запись еды), «позанимался в зале, жим 3×8».
- [ ] Реализация. Свои тесты `-n 2` + `tests/test_log_text.py -n 2`. Commit.

## Часть 3. Напоминания и сводка (агент, отдельное дерево)

### Task 3.1: Напоминания о планах и дедлайнах

**Files:** Create `bot/src/gymbot/services/life_reminders.py`, `bot/src/gymbot/handlers/life_reminders.py` (кнопки «✅ Сделано» `lrdone:{id}`, «+30 мин» `lrsnooze:{id}`); Modify `bot/src/gymbot/services/reminders.py` (`reminder_loop` вызывает `life_reminders.tick(bot, sessionmaker, settings, now)`), `bot/src/gymbot/main.py` (роутер); Test `bot/tests/test_life_reminders.py`.

**Interfaces — Produces:** `async def tick(send: Callable[[int, str, InlineKeyboardMarkup|None], Awaitable], sessionmaker, settings, now_utc) -> int` (отправлено); `def due_events(view, now_local, default_before) -> list[LifeEvent]`; `def summary_text(view, today, training_line: str | None) -> str` (HTML).

- [ ] Тесты (часы закреплены): событие 11:00, `remind_before` NULL → настройка 30 → в 10:30 одно сообщение, в 10:30 второй тик — ничего; «+30 мин» → `start` 11:30, `reminded_at` NULL → в 11:00 снова; «Сделано» → `done_at`; событие без времени не напоминается; дедлайн завтра → сообщение в 20:00 один раз (`reminded_on`).
- [ ] Реализация (отметка до отправки, как в `reminders.tick`). Commit.

### Task 3.2: Утренняя сводка

**Files:** Modify `bot/src/gymbot/services/life_reminders.py`; Test `bot/tests/test_life_reminders.py`.

- [ ] Тесты: в `life_summary_minute` локального времени — одна сводка в день (отметка — `users.life_summary_sent_on`, колонка из задачи 1.1); содержит повестку дня по времени, тренировку дня по программе («Руки и плечи» / «отдых»), дедлайны ≤ 3 дней, дела по дому со сроком, до 5 задач, следующий шаг проектов обучения; `life_summary_enabled=False` → ничего; пустой день → «Сегодня свободно» + тренировка.
- [ ] Реализация. Commit.

## Часть 4. Мини-апп (агент, отдельное дерево)

### Task 4.1: API-клиент и логика «Дел»

**Files:** Create `miniapp/src/life.ts` (типы ответа `/api/life/view`, функции API, `agendaLabel`, сортировки), `miniapp/src/life.test.ts`; Modify `miniapp/src/api.ts` (заголовок `X-App-Token` из localStorage, если нет initData), `miniapp/src/liveCore.ts` (тема `life`, если не добавлена в части 1).

- [ ] vitest: разбор ответа, группировка расписания по дням недели, «через N дней / просрочено» для дел по дому и дедлайнов, `agendaLabel`. Commit.

### Task 4.2: Экраны «Дела»

**Files:** Modify `miniapp/src/App.tsx` (переключатель «Зал | Дела» вверху, `?section=life`, запоминание в localStorage); Create `miniapp/src/screens/life/{LifeToday,LifeSchedule,LifeTasks,LifeProjects,LifeNotes}.tsx`, иконки в `components/icons.tsx`, стили в `styles.css` (переиспользовать существующие карточки, `Sheet`, `ConfirmHost`).

- [ ] Экраны по спецификации, раздел 4; добавление/правка через `Sheet`, удаление с подтверждением; обновление по live `life`.
- [ ] `npm run typecheck && npm test` (сборку делает оркестратор). Commit.

### Task 4.3: Вход с компьютера и выключение `/hub/`

**Files:** Modify `bot/src/gymbot/api/app.py` (`tg_user` принимает `X-App-Token`), Create `bot/src/gymbot/services/web_login.py` (`make_token(key, telegram_id, ttl=30*86400)`, `verify(key, token) -> int | None`, назначение `web-login`, ключ из `bot_token` как у `live`), команда `/web` в новом `bot/src/gymbot/handlers/web_login.py` (роутер в `main.py`); `/hub` и `/hub/` → 302 на `/?section=life` (в `app.py`, когда мини-апп собран); `miniapp`: экран «Открой /web в боте» вне Telegram без токена; Test `bot/tests/test_life_api.py`.

- [ ] Тесты: токен `/web` пускает в `/api/life/view` и `/api/state`; просроченный/поддельный — 401; `/hub/` → 302.
- [ ] Реализация. Commit.

## Интеграция и выкат (оркестратор)

- [ ] Слить ветки частей 2, 3, 4 (конфликты в `log_text.py`, `main.py`, `app.py` — по смыслу).
- [ ] Полный прогон: `ruff`, `pytest -q`, `pytest -q --shift-days 5`, затем `npm run typecheck && npm run build && npm test` (последовательно).
- [ ] Локальный стенд (временная база, `RUN_BOT=false`, без боевого токена) + Playwright: «Дела» в мини-аппе, вход по токену `/web`.
- [ ] Пуш в `main`, проверка выката (журнал, `/api/health`, `/api/life/view` → 401 без входа).
- [ ] README: команды `/life`, `/web`, раздел «Дела»; ROADMAP: этап «Ассистент».
