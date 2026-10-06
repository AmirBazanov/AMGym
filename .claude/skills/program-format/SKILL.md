---
name: program-format
description: Формат готовых программ тренировок из xlsx-таблички Амира и их импорт в JSON/БД. Используй при добавлении новой программы, ошибке импорта или изменении модели программ.
---

# Формат программ

Полное описание: `docs/program-format.md`. Кратко: лист с колонками «упражнения | интенсивность | подходы х повторения»; строки «неделя N» и дни недели — маркеры; предписание `6х8-12` или `дропсет 3х 12-6-6`.

## Добавить программу
1. Положи xlsx в `data/programs/source/` (латиница в имени файла, snake_case).
2. Конвертируй:
   ```bash
   PYTHONPATH=bot/src python -m gymbot.importers.xlsx_program data/programs/source/<file>.xlsx data/programs/<file>.json --name "Человеческое название"
   ```
3. Если упал `ValueError: unknown prescription format` — это новый формат записи. Добавь regex в `parse_prescription` и тест в `bot/tests/test_xlsx_program.py`, не правь исходную табличку.
4. Проверь список уникальных упражнений в JSON: опечатки и синонимы («тяжа» → «тяга») добавляй в `NAME_FIXES` или в `aliases` упражнения, чтобы одно упражнение не стало двумя и прогресс не разъехался.
5. Загрузка в БД (этап 2): JSON → `Program/ProgramWeek/ProgramDay/ProgramItem`, упражнения через get-or-create по нормализованному имени.

## Как читать программу в приложении
День тренировки = `user_programs.started_on` + номер недели и `weekday`. Для дропсета `drop_reps` = повторы основного подхода и каждого снижения; в журнале это сеты с одинаковым `set_index` и `drop_index` 0, 1, 2.
