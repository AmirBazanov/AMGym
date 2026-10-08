# Управление программой и днём из чата

Дата: 2026-10-09. Статус: план, код не написан. Автор: architect.

## Цель

Амир пишет боту свободным текстом (или голосом, тот же `process_text`), что поменять. Бот показывает «Что изменю: …» с кнопками «✅ Применить» / «Отмена». После нажатия правка идёт через те же сервисы, что и API, а мини-апп обновляется сам по SSE.

Примеры и куда они ложатся:

| Команда | Действие | Этап |
|---|---|---|
| «убери французский жим из дня рук» | `remove` | 1 |
| «добавь подтягивания в день спины 3×8» | `add` | 1 |
| «вместо жима в Смите поставь жим гантелей» | `replace` | 1 |
| «на разгибания 4 подхода по 10–12» | `prescribe` | 1 |
| «поставь на сгибания 30 кг» | вес на ближайший день с этим упражнением (`WeightOverride`) | 1 |
| «поменяй местами руки и спину на этой неделе» | `move_day` (обмен) на текущей неделе | 2 |
| «сделай сегодня ноги вместо рук» | `move_day` (обмен сегодняшнего дня с днём ног) на текущей неделе | 2 |
| «перенеси тренировку на завтра» | `move_day` сегодня → завтра (обмен, если завтра занято) | 2 |
| «следующая неделя — делоад» | `deload.start` с датой старта | 2 |
| «сегодня облегчённо, −20 %» | ручная поправка дня → вход плана | 3 (миграция) |

## Что есть на самом деле (проверено по коду)

- `services/program_editor.py`: `parse_ops`, `apply_ops`, `edit_program(session, user, up, slug, version, raw_ops, dry_run)`. Есть копия при первой правке, версия, `Conflict` (409) и `EditError` (422). Операции: `replace | prescribe | add | remove | reorder`. **`move_day`, `copy_week`, `rename` в `LATER_OPS`, не реализованы.**
- **Истории снимков или ревизий программ нет.** В `models.py` и миграции 0014 только `owner_user_id`, `based_on_id`, `version`, `focus`, `base_day_id`, `targets_json`. Спека редактора явно говорит «не делаем историю версий». Поэтому «источник chat в истории» писать некуда (см. «БД»).
- `services/chat_settings.py` + `handlers/chat_settings.py` — готовый образец всего потока: regex-гейт `is_settings_request` до парсера, отдельный `complete_json(purpose="settings")` со своей схемой (`structured.SETTINGS`), поштучная валидация `parse_actions`, `resolve` → `Plan` со строками превью, `SETTINGS[token]` в памяти с TTL, `sapply/sdrop`, `apply`, `live.publish(*plan.live_topics())`. `WeightAction` (вес на сегодня) уже там, но гейт требует слова «сегодня».
- `PATCH /api/programs/{slug}` (`api/app.py:316`): своя сессия, `dryRun` = rollback, после коммита `live.publish(uid, "program", "plan", "state")`. Мини-апп на `program` синхронизирует `/api/state`, видит новую `programVersion`/`programId` и перезагружает программу (`liveCore.routeTopics`, `store.applyServer` → `ensureProgram` → `storeProgram` → `rebuildPrepared`). **Правку своей копии из чата мини-апп подхватывает без доработок.** Исключение — **форк, сделанный не этим мини-аппом**: `switchedFrom` до мини-аппа не доходит, `afterEdit`/`retargetWorkouts` вызываются только из `requestEdit` (`store.ts:664`). `storeProgram(copy)` не пересобирает `active` со slug шаблона (`rebuildPrepared` смотрит на slug копии), `pending` тоже остаются на шаблоне. Сервер сохранение переживёт: `save_from_miniapp` знает шаблон активной копии. Но подготовленная тренировка и ✓ в `currentRun` могут разъехаться до перезапуска. Тот же пробел уже есть при правке с другого устройства; из чата он станет частым → задача 1.8.
- `saved_edits.detect` (вызывается в `process_text` **раньше** settings) ловит «убери/удали/исправь» и пропускает только `is_settings_request` и `_NOT_RECORDS` (план, программа, …). «Убери французский жим из дня рук» сейчас уйдёт в правку сохранённых записей.
- `deload.start(session, user_id, today, now)`: старт только сегодня. `plan.collect_inputs` читает `deload.active_until`.
- `DayPlan` — кеш по `inputs_hash`; писать в него ручные правки нельзя, их перезапишет пересчёт.
- Шаблон `arms_specialization_8w`: 8 недель × 3 дня (пн/пт «Руки и плечи», ср «База»), 128 позиций. «День спины/ног» может не совпадать с `focus`: модель сопоставляет по составу упражнений.

## 1. Архитектура: «изменить» отдельно от «записать» и «спросить»

**Решение: отдельный гейт и отдельный вызов LLM со своей structured-схемой, как `chat_settings`. `ParseResult.kind="edit"` не вводим.**

Почему:
- Промпт парсера на пределе (`prompts.py:332`), а схема `PARSE` уходит в каждое сообщение. Вложенный дискриминированный список правок в ней усложнит грамматику и ухудшит разбор записей у fallback-моделей (Groq/OpenRouter без схемы).
- Контекст правки (неделя программы с днями и упражнениями) нужен только командам. Парсеру он стоил бы токенов на каждой записи еды.
- Отдельная схема для Claude (`output_config.format`) маленькая и строгая. Для fallback работают `extract_json(prefer="actions")` и поштучная валидация: битое действие отбрасывается с заметкой, остальные живут.
- Образец уже в проде и протестирован.

**Схема ответа модели — намерения с именами, а не сырые PATCH-ops.** Ops требуют `itemId`, `tempId`, `position`, а `prescribe` — полного предписания (без `repsMin` будет 422). Номер недели модель тоже не должна считать. Модель отдаёт:

```
{"actions": [Action], "summary": str}        # summary: одна фраза для превью
Action =
 | {type:"replace",   day:Day, exercise:str, new_name:str, scope:Scope}
 | {type:"remove",    day:Day, exercise:str,            scope:Scope}
 | {type:"add",       day:Day, name:str, sets:int, repsMin:int|null, repsMax:int|null,
                      dropReps:[int]|null, after:str|null, scope:Scope}
 | {type:"prescribe", day:Day, exercise:str, sets:int|null, repsMin:int|null, repsMax:int|null,
                      dropReps:[int]|null, intensity:"heavy"|"medium"|"light"|null, scope:Scope}
 | {type:"reorder",   day:Day, order:[str]}
 | {type:"weight",    said:str, exercise:str|null, weight_kg:float, date:"ГГГГ-ММ-ДД"|null}   # как в settings
 | {type:"swap_days", a:Day, b:Day, scope:Scope}                 # этап 2, move_day с обменом
 | {type:"move_day",  src:Day, dst:Day, scope:Scope}             # этап 2 («на завтра»)
 | {type:"deload",    start:"ГГГГ-ММ-ДД"}                        # этап 2
 | {type:"lighten",   weight_pct:int, sets_delta:int|null}       # этап 3, только сегодня
 | {type:"clarify",   question:str, options:[str]}               # неоднозначность
Day   = {"weekday": 1..7 | null, "focus": str | null, "when": "today"|"tomorrow"|null}
Scope = "this_week" | "from_this_week" | "all_weeks" | null      # null = по умолчанию (ниже)
```

Компиляция в ops делается **в коде** (`services/chat_edit.compile_ops`), а исполняет её существующий `program_editor`:
- `Day` → `(week, weekday)`: `when` считается от сегодняшней даты; `weekday` берётся как есть; `focus` — по `ProgramDay.focus` текущей недели или по совпадению упражнений («день рук» = день, где есть сгибания или французский жим). Ноль или несколько кандидатов → уточнение.
- `exercise` → `ProgramItem.id` только среди упражнений этого дня: `overrides.match(said, None, day_names)`, затем `baselines.match_exercise`. Не нашлось или больше одного кандидата → уточнение.
- `Scope` → `weeks`: порт `programEdit.scopeWeeks` (`week | from | all`) по `program_position(...).week`. Если scope не сказан, умолчание как `defaultScope` в мини-аппе: `replace` → все недели, остальное → только эта неделя. В превью scope всегда пишется словами.
- `prescribe`: незаданные поля берутся из текущего item (полное предписание для `_prescription`).
- `add.after` → `position`; без него — в конец. `tempId` = `"c1"`, `"c2"`…
- `add` без подходов и повторов («добавь подтягивания в день спины»): умолчание 3×8–12, и в превью явно «3×8–12 (по умолчанию)». Пользователь видит это до «Применить», а 422 из `_prescription` так не возникает.
- Имена полей схемы не совпадают с ключевыми словами Python (`new_name`, `src`, `dst`): без alias в pydantic.
- Шаблон активен → ops уходят на шаблон, `edit_program` сам делает форк.

## 2. Поток в чате

```
process_text
  └─ (до saved_edits, вне открытого диалога) is_edit_command(text)?
       └─ handlers/chat_edit.stage_edit(...)          # 1 вызов LLM, purpose="edit"
            ├─ actions == [] / ошибка → None → обычный путь (saved_edits, settings, парсер)
            ├─ clarify / неоднозначность → вопрос с инлайн-кнопками вариантов → новый stage с выбранным
            └─ resolve → dry-run edit_program в своей сессии + rollback
                 → «Что изменю: …» + «✅ Применить» / «Отмена»   (EDITS[token], TTL 15 мин)
callback eapply:<token>
  → edit_program(..., version=снятая при превью) в новой сессии → commit
  → weights/deload/... в той же транзакции
  → live.publish(uid, "program", "plan", "state")   (+ "state" для весов)
  → edit_text «Готово: …» + кнопка «Открыть дневник» (handlers/plan.open_diary_kb)
```

- **Сообщение уходит ровно в один путь.** В отличие от settings, команда правки, по которой модель вернула хотя бы одно валидное действие, **поглощает** сообщение: парсер не запускается. Иначе «добавь подтягивания 3×8» даст ещё и превью тренировки. Если модель вернула `[]`, текст идёт дальше как обычно (saved_edits → settings → парсер).
- **Гейт `is_edit_command`** (`services/chat_edit.py`, консервативный regex, как `is_settings_request`). Срабатывает, если выполнена хотя бы одна из веток:
  - (а) повелительный глагол (`убери|удали|добавь|замени|поменяй|поставь|вместо|перенеси|сделай|верни`) **и** слово структуры (`день|дня|дне|программ|недел|местами|вместо|тренировк`);
  - (б) `поставь|выставь` + число + `кг` без признаков повторов (`chat_settings.REPS`): «поставь на сгибания 30 кг». Это `WeightA` в edit-схеме; гейт `chat_settings` не трогаем, «поставь сегодня жим 85» по-прежнему ловят оба, и побеждает edit (ниже);
  - (в) существительное-триггер без глагола: `делоад|разгрузк\w*|облегч\w*` + `недел|сегодня|завтра`;
  - (г) упражнение из каталога дня (`match_exercise`) + `\d+\s*подход\w*` или `\d+\s*[xх×]\s*\d+` без глагола в прошедшем времени: «на разгибания 4 подхода по 10–12».
  Исключения (всё равно не срабатывает): `?`; открытый диалог (`_dialog_open`); глагол в прошедшем времени — **только глаголы**, `сделал\w*|выполнил\w*|(?:по|вы|от)?жал(?!уйст)\w*|получил\w*|был[аио]?`. `chat_settings.REPS`/`DROP_REPS` для исключения брать нельзя: они ловят «3×8» и «4 подхода по 10», а это и есть содержимое правки предписания. «Вместо жима сделал гантели 30 на 8» уходит парсеру.
- **Порядок гейтов.** `is_edit_command` проверяется **до** `saved_edits.handle`. Кроме того, `saved_edits.detect` получает `or is_edit_command(text)` в раннем выходе (`saved_edits.py:187`). Пересечение с settings: «поставь сегодня жим 85» остаётся в settings (у edit-гейта нет слова структуры). Если сработали оба гейта, побеждает edit: его схема содержит `weight`, и смешанное «убери X, а на Y поставь 30» решается одним вызовом.
- **Версия и 409.** В pending хранятся `slug`, `version` на момент превью, скомпилированные ops и строки превью. При «Применить» `Conflict("version")` → «Программа изменилась (в мини-аппе или другой командой), повтори команду»; `Conflict("not_active")` → то же; `EditError` → его текст. Повторная компиляция без участия пользователя не делается: он подтверждал конкретные строки.
- **Форк.** Если активен шаблон, первая строка превью: «Создам твою копию «{имя} · моя», оригинал останется». Это текст confirm из мини-аппа.
- **Пропуски недель.** Берутся из `results[].skipped` dry-run: «Недели 3–5 пропущу: там другое упражнение». Формат недель — порт `formatWeeks`.
- **Уточнение.** Инлайн-кнопками (до 4 вариантов дня или упражнения + «Отмена»), callback `eclar:<token>:<n>`. Выбор подставляется в действие, и превью строится заново без нового вызова LLM. Это **сознательное отступление** от «уточняющего вопроса, как для записей»: диалоговое состояние парсера (`recent_exchange`, `_dialog_open` в `log_text.py`) сейчас меняет другой агент, а кнопки не дают модели ошибиться повторно. Если пользователь ответит текстом, а не кнопкой («пятничный»), сообщение снова идёт через гейт как новая команда. Обычно оно гейт не проходит и уходит парсеру, и тот ответит не по теме. Старое превью с кнопками остаётся живым до TTL. Если Амиру нужен текстовый ответ, в этапе 2 храним последнее уточнение в `EDITS` и склеиваем «команда + ответ» для повторного `stage_edit` (одна проверка в начале `process_text`).
- **raw_text.** Исходный текст сообщения (с `[voice]` для голоса) хранится в pending и пишется в лог `program edit from chat: user, slug, version, ops`. Таблица аудита — этап 3 (см. «БД»).
- **Формат.** Превью — простой текст (как в `chat_settings.render`), без `parse_mode`. После слияния `services/tg_html.py` один исполнитель переводит `render` на него, если default parse_mode станет HTML (иначе `<` в названиях сломает разметку). Это отдельная задача.

## 3. Контекст для модели

Системный промпт (`EDIT_SYSTEM_PROMPT`, ~1,5–2K токенов: действия, правила scope, «день X» → по составу, few-shot на 6–8 примеров из таблицы выше) стабилен. Он кладётся в `Purpose("edit", "low", 4096, 30.0, structured.EDIT, stable_system=EDIT_SYSTEM_PROMPT)` и кешируется (больше 512 токенов, TTL 5 минут; команды идут пачкой).

Пользовательская часть (`chat_edit.prompt_context`, ~600–900 токенов):
```
Сегодня 2026-10-09, пятница. Программа «…» (шаблон|моя), неделя 3 из 8.
Неделя 3:
 пн «Руки и плечи»: 1. жим в смите 4×8-10; 2. французский жим лёжа 3×10-12; …
 ср «База»: …
 пт «Руки и плечи»: … (сегодня)
Неделя 4 (следующая): только отличия от недели 3 или «как неделя 3»
Веса на сегодня: сгибания на бицепс 30 кг.
Каталог упражнений: … (baselines.catalog, как в settings)
```
- Номера недель и id модель не видит: недели считает код, упражнения сопоставляются по имени внутри дня.
- Все 8 недель в контекст не попадают: межнедельное сопоставление делает `apply_ops` (`weeks` + поиск по `exercise_id`).
- Факты, питание и самочувствие не нужны.

## 4. БД и миграции

- **Этап 1: без миграций.** Используются `programs.version`, форк, `WeightOverride`.
- **Этап 2: без миграций.** `move_day` меняет `ProgramDay.weekday` (столбец есть). Для `deload.start` добавляется параметр `start_on: date` (сейчас стартует только сегодня). `active()` уже корректно работает с будущим `started_on`; проверить `due`/`evaluate`, чтобы будущий делоад не предлагался повторно.
- **Этап 3: миграция 0015** (`down_revision="0014"`, сначала проверить `alembic heads`):
  - `day_adjustments(id, user_id FK, day Date, weight_factor Numeric(4,2), sets_delta Integer null, source String(16) chat|miniapp, raw_text Text, created_at)`, `UniqueConstraint(user_id, day)`. `plan.collect_inputs` читает строку на сегодня, `rule_draft` применяет её как делоад (берётся более лёгкое из всех поправок), `inputs_hash` меняется сам через `asdict`. Мини-апп уже применяет `weightFactor`/`sets` плана, доработки там не нужны. `PLAN_VERSION += 1`.
  - `program_edits(id, user_id FK, program_id FK, version_after Integer, source String(16) chat|miniapp, raw_text Text null, ops_json Text, created_at)` — аудит и основа для будущего «верни как было». Пишется в `edit_program` (аргументы `source`, `raw_text`), значит и из PATCH. Если Амиру не нужно, этап 3 делается без этой таблицы, остаётся только лог.

## 5. Задачи

### Этап 1 (первый коммит, MVP: правки дня программы + вес на день)

| # | Исполнитель | Файлы | Что |
|---|---|---|---|
| 1.1 | bot-backend | новый `bot/src/gymbot/services/chat_edit.py` | `is_edit_command`; pydantic-модели действий (`ReplaceA`, `RemoveA`, `AddA`, `PrescribeA`, `ReorderA`, `WeightA`, `ClarifyA`), `parse_actions` (поштучно, заметки об отброшенных); `load_snapshot` (активная программа через `users.active_program`, `load_program`, `program_position`, каталог, веса на сегодня); `prompt_context`; `resolve_day`, `resolve_item`, `scope_weeks`, `compile_ops` → `EditPlan(slug, version, ops, weights, lines, notes, clarify, forks: bool)`; `preview(session, user, up, plan)` = dry-run `edit_program` + rollback → строки пропусков; `apply(session, user, plan, tz, now)` = `edit_program` + `overrides.upsert`. Вес без даты → ближайший день программы (в пределах `advice.NEXT_DAY_SEARCH`), где есть упражнение. |
| 1.2 | bot-backend | новый `bot/src/gymbot/llm/prompts_edit.py` (не `prompts.py`: его сейчас меняют) | `EDIT_SYSTEM_PROMPT`, `build_edit_messages(text, context)` |
| 1.3 | implementer | `llm/structured.py`, `llm/claude.py` | `EditAnswer` в structured (зеркало моделей 1.1, все ключи обязательны, single-Literal → enum), `EDIT = schema_of(EditAnswer)`; одна строка `Purpose("edit", …, stable_system=EDIT_SYSTEM_PROMPT)` в `PURPOSES` |
| 1.4 | bot-backend | новый `bot/src/gymbot/handlers/chat_edit.py` | `stage_edit` (как `stage_settings`, `prefer="actions"`, `purpose="edit"`), `EDITS: dict[token, EditPending]`, `render`, клавиатуры `eapply/edrop/eclar`, коллбэки; после коммита `live.publish`; `Conflict`/`EditError` → понятный текст; повторное нажатие → `STALE` |
| 1.5 | bot-backend | `handlers/log_text.py` (одна точка, ~8 строк, согласовать с агентом HTML), `services/saved_edits.py:187`, регистрация роутера (там же, где `chat_settings.router`) | вызов `stage_edit` до `saved_edits.handle`; если вернулся staged → `send_edit` и `return`; исключение в `detect` |
| 1.6 | test-writer | `bot/tests/test_chat_edit.py`, `bot/tests/test_chat_edit_handler.py` | см. «Тесты» |
| 1.7 | docs-writer | `README.md` (команды чата), `ROADMAP.md`, таблица в `CLAUDE.md` (`services/chat_edit.py`) | после ревью |
| 1.8 | miniapp-frontend | `miniapp/src/store.ts` (`storeProgram`), тест в `programEdit.test.ts` или `store`-тестах | форк извне: когда загруженная программа — это `state.programId` и `program.basedOn` равен `active.programId` или `programId` в `pending`, применить `afterEdit(state, program, program.basedOn)` (как `applyEdit`), а `oldDay` взять из шаблона. Идемпотентно. |

Порядок: 1.1 и 1.2 параллельно (контракт действий зафиксирован выше) → 1.3 → 1.4 → 1.5 → 1.6 (тесты на `compile_ops` можно начинать сразу после 1.1) → reviewer → 1.7. Задача 1.8 независима, её можно делать параллельно с самого начала.
Ручная проверка: открытый мини-апп обновился после «Применить» (в том числе после форка: подготовленная тренировка на копии); открытый черновик редактора дня получает 409 (уже обрабатывается).

### Этап 2 (перенос, обмен дней, делоад)

| # | Исполнитель | Файлы | Что |
|---|---|---|---|
| 2.1 | bot-backend | `services/program_editor.py` | реализовать `move_day` из спеки редактора (обмен, не раньше текущей недели, `skipped`), убрать из `LATER_OPS` |
| 2.2 | miniapp-frontend | `miniapp/src/programEdit.ts`, `api.ts` (тип `MoveDayOp`) | только тип и обработка `move_day` в ответах/409; UI переноса дня — по фазе 3 спеки, не обязателен для чата |
| 2.3 | bot-backend | `services/deload.py` (`start(..., start_on)`), `chat_edit.py`, `prompts_edit.py`, `structured.py` | действия `swap_days`, `move_day` («сегодня ↔ день ног», «на завтра»: свободен — перенос, занят — обмен с предупреждением в превью), `deload`. Если день, который приедет на сегодня, уже отмечен ✓ на этой неделе, превью предупреждает. |
| 2.4 | test-writer | тесты move_day, swap «сегодня ноги вместо рук», делоад со следующего понедельника | |

### Этап 3 (облегчённый день, аудит)

| # | Исполнитель | Файлы | Что |
|---|---|---|---|
| 3.1 | bot-backend | `db/models.py`, `migrations/versions/0015_day_adjustments.py`, `services/plan.py` (`collect_inputs`, `rule_draft`, `PLAN_VERSION`), `chat_edit.py` (`lighten`) | см. «БД» |
| 3.2 | bot-backend | `program_edits` + `edit_program(source, raw_text)` + `api/app.py` (`source="miniapp"`) | опционально |
| 3.3 | test-writer | миграция upgrade/downgrade/upgrade, план с поправкой, хеш меняется | |

## Тесты (этап 1)

Unit (`test_chat_edit.py`, без LLM, `now` всегда закреплённый: есть `--shift-days`):
- `is_edit_command`: «да» — все 9 примеров из таблицы целей (гейт общий для всех этапов; действия этапов 2–3 до их реализации модель не вернёт, тогда текст уходит дальше, или код отвечает «пока не умею»), по примеру на ветки (а)–(г) и «поставь сегодня жим 85» (оба гейта → edit); «нет» — «вместо жима сделал гантели 30 на 8», «жим 85 на 8», «сделал подтягивания 3×8», «что поменять в дне рук?», «убери самсу», «съел 2 самсы».
- `parse_actions`: невалидное действие отброшено с заметкой, остальные живы; мусорный JSON → `[]`.
- `resolve_day`: по `weekday`, `when=today/tomorrow`, `focus`, по составу («день рук»); два кандидата → clarify.
- `resolve_item`: точное имя, алиас/`SHORT_NAMES` («французский» → «французский жим лёжа»), нет в дне → clarify.
- `compile_ops`: каждый тип → ожидаемый op; `prescribe` дополняется текущим предписанием; scope → weeks; `add.after` → position.
- Применение на БД из фикстуры шаблона: превью dry-run не пишет ничего (нет копии, версия та же); apply на шаблоне делает форк и правки; `replace` на всех неделях даёт пропуски 3–5 в строках превью; устаревшая версия → `Conflict` → «повтори команду»; вес без даты → ближайший день с упражнением.

Интеграционные (`test_chat_edit_handler.py`, мок `llm.complete_json`, по образцу тестов `chat_settings`):
- «убери французский жим из дня рук» → превью, `saved_edits` не вызван, парсер не вызван; `eapply` → упражнения нет, `live.publish` получил `program, plan, state`; повторное нажатие → `STALE`.
- Модель вернула `[]` → парсер вызван как обычно.
- Clarify → кнопки → выбор → превью без второго вызова LLM.
- Изменение версии между превью и «Применить» (PATCH из «мини-аппа» в тесте) → «изменилась, повтори».
- Чужой `user_id` в callback не применяет.

## Риски

- **Ложное срабатывание гейта съест запись.** Защита: поглощаем сообщение только при валидном действии, гейт отсекает прошедшее время и `?`, у превью есть «Отмена». Тесты на негативные примеры обязательны.
- **Неверный день или упражнение молча изменят программу.** Защита: в превью точные имена из БД, неделя и scope словами; при неоднозначности уточнение, а не догадка.
- **«На все недели» сплющивает волны предписаний.** Умолчание для `prescribe` — только эта неделя, и превью это пишет.
- **Пересечение с агентом HTML** (`log_text.py`, `prompts.py`, `answer.py`, `tg_html.py`): свой промпт в отдельном модуле, в `log_text.py` одна вставка после их слияния, превью plain text.
- **Опечатки размножают упражнения** (`get_or_create_exercise` создаёт новое). В превью для `replace/add` с именем не из каталога: «новое упражнение, истории нет».
- **Pending в памяти**: рестарт → «Устарело», как в settings. Допустимо.
- **Этап 2:** `move_day` посреди начатой тренировки — она остаётся на своём `programDayId` (спека редактора). Обмен с уже сделанным днём переносит ✓ — предупреждение в превью.
- **Этап 3:** миграция 0015 строго после 0014, проверка на копии прод-базы.
