# Бесплатные vision-модели для оценки еды по фото — справочник для агентов
Дата: 2026-10-08. Версия: фиксированной версии нет (это живые API Groq и OpenRouter; клиент проекта — `httpx>=0.27`, `bot/pyproject.toml`). Каталоги моделей сняты 2026-10-08. Источники:
- Groq, vision: https://console.groq.com/docs/vision ; модель: https://console.groq.com/docs/model/qwen/qwen3.8-27b
- Groq, лимиты: https://console.groq.com/docs/rate-limits ; данные: https://console.groq.com/docs/your-data
- Groq, живой каталог: `GET https://api.groq.com/openai/v1/models` (с ключом из `.env`, ключ не печатался)
- OpenRouter, каталог: `GET https://openrouter.ai/api/v1/models` и `GET https://openrouter.ai/api/v1/models/{id}/endpoints` (без ключа)
- OpenRouter, лимиты: https://openrouter.ai/docs/api-reference/limits , https://openrouter.ai/docs/faq
- OpenRouter, картинки в чате: https://openrouter.ai/docs/guides/overview/multimodal/image-understanding.md
- OpenRouter, structured outputs: https://openrouter.ai/docs/guides/features/structured-outputs
- Gemini API: https://ai.google.dev/gemini-api/docs/openai , https://ai.google.dev/gemini-api/docs/pricing , https://ai.google.dev/gemini-api/terms , https://ai.google.dev/gemini-api/docs/available-regions
- Cloudflare Workers AI: https://developers.cloudflare.com/workers-ai/platform/pricing/ , https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/
- GitHub Models (закрыт): https://docs.github.com/en/github-models/use-github-models/prototyping-with-ai-models

Живые вызовы сделаны скриптом вне репозитория (scratch), с тестовыми фото из Wikimedia Commons (свободные лицензии) и сгенерированной картинкой. Ключей в выводе нет.

## Вывод для спешащих

| Роль | Маршрут | Почему |
|---|---|---|
| Основной | Groq `qwen/qwen3.8-27b` | единственная vision-модель на Groq; уже первая в `GROQ_MODELS`; ключ есть; 1.1–1.3 с на фото; `json_object` с картинкой работает (проверено); данные не хранятся по умолчанию |
| Запасной 1 | OpenRouter `google/gemma-4-31b-it:free` (затем `google/gemma-4-26b-a4b-it:free`) | vision + `response_format` по каталогу; обслуживает Google AI Studio. **В тесте 6 из 6 запросов — 429 «upstream shared pool»** (5 с фото, 1 текстовый) — как запасной годится только с коротким таймаутом |
| Запасной 2 | OpenRouter `dots-studio/dots-3-note-preview:free` | работает (проверено), но **без `reasoning: {"enabled": false}` отвечает 69 с** и тратит 5.2K токенов; препросмотр, срок жизни до 2026-12-31 |
| Позже, при желании | Gemini API напрямую (`gemini-3.8-flash`, free tier) | OpenAI-совместимый, но не проверен (нет ключа), на free tier Google читает и обучается на данных; доступность зависит от региона сервера |

Оценка на основе n=2 фото: плов с лепёшкой и салатом, 17 пельменей. Это не бенчмарк.

## Как это относится к проекту

- Клиент: `bot/src/gymbot/llm/openrouter.py` (`OpenRouterClient`, класс назван исторически, алиас `LLMClient`). Маршрут `Route(provider, base_url, api_key, model)`; `routes_from(settings)` строит порядок: все `GROQ_MODELS`, затем `OPENROUTER_MODEL` + `OPENROUTER_FALLBACK_MODELS`. `_over_routes` обходит маршруты, на 429 держит маршрут в `RATE_LIMIT_COOLDOWN` (60 с), 400/401/402/403/404/413/429 переводят на следующий маршрут.
- Настройки: `bot/src/gymbot/config.py:22-37`. `groq_models = ["qwen/qwen3.8-27b", "openai/gpt-oss-20b", "openai/gpt-oss-120b"]`, `openrouter_model = "inclusionai/ling-3.1-flash"`, fallback `ling-3.0-flash-sante:free`, `apodex-1.1-mini:free`. Ключ Groq: `groq_key` (`GROQ_API_KEY`, иначе `STT_API_KEY`, если STT идёт в Groq) — в этом окружении `.env` содержит `STT_API_KEY` и `OPENROUTER_API_KEY`.
- **Текущие текстовые маршруты картинки не примут.** Проверено: Groq `openai/gpt-oss-20b` на запрос с `image_url` отвечает `400 messages[1].content must be a string`; OpenRouter `inclusionai/ling-3.1-flash` — `404 No endpoints found that support image input`. Оба статуса уже в `NEXT_ROUTE_STATUSES`, но каждый такой маршрут — зря потраченный запрос, поэтому нужен **отдельный список vision-маршрутов**.
- `_complete(...)` типизирован `messages: list[dict[str, str]]` (`openrouter.py:132-140`): для vision `content` — список частей, тип надо расширить (`list[dict[str, Any]]`).
- Результат: `ParseResult` / `ParsedFood` (`bot/src/gymbot/llm/schemas.py:25-50, 109-170`): `description, grams, kcal, protein_g, fat_g, carbs_g`. `ParsedFood._kcal_from_macros` пересчитывает kcal по макросам (4/9/4), если расхождение >15 % — это уже защищает от типичной ошибки бесплатных моделей, на vision тоже. `_foods_without_numbers` отправляет блюда с `kcal=null` в `unknown_terms` → `food_lookup`.
- Превью с кнопкой подтверждения: `bot/src/gymbot/handlers/log_text.py` (`process_text` с `raw_text`/`prefix`, ~стр. 660-840; `llm.parse_message` вызывается на стр. ~703). Голос уже делает ровно тот же приём: `handlers/voice.py` скачивает файл, получает текст и вызывает `process_text(..., raw_text="[voice] ...", prefix="Распознал: «...»")`. Для фото нужен аналог: `handlers/photo.py`.
- Роутеры подключаются в `bot/src/gymbot/main.py:174-183`, `log_text.router` последним («ловит любой текст»). Фото-роутер — перед ним, рядом с `voice.router`.
- Расхождений с документацией два: (1) проект считает «картинка = 2048 токенов» по доке, но реально `prompt_tokens` в тесте 882–1913 (см. ниже); (2) в `config.py` комментарий «1000 requests/day and 8000 tokens/min per model» верен для free-плана (подтверждено таблицей лимитов), но про vision там ничего нет, и квота у qwen общая с текстом.

## Что есть в каталогах (2026-10-08)

### Groq (живой `GET /openai/v1/models`, 11 моделей)

Картинки принимает ровно одна: `qwen/qwen3.8-27b` (`input_modalities: [text, image]`). Остальные — текст/аудио (`openai/gpt-oss-*`, `allam-2-7b`, whisper, orpheus, prompt-guard). Поля модели: `context_window 131072`, `max_completion_tokens 16384`, `supported_features: [tools, json_mode, reasoning]`.

Поле `pricing` в каталоге ненулевое (`prompt 0.0000008`, `completion 0.000004`, `image "0"`) — это цена платного плана. На free-плане модель доступна (подтверждено запросом с ключом и таблицей лимитов), не путать.

### OpenRouter: бесплатные + image на входе (фильтр по `pricing.prompt==0 && completion==0` и `image in input_modalities`; 467 моделей в каталоге, 11 прошли)

| id | Решение | Причина |
|---|---|---|
| `google/gemma-4-31b-it:free` | кандидат | endpoint Google AI Studio; `response_format` есть; uptime 1d ≈ 99 %; на тесте 429 upstream |
| `google/gemma-4-26b-a4b-it:free` | кандидат | то же, MoE 3.8B активных; на тесте тоже 429 |
| `dots-studio/dots-3-note-preview:free` | запасной | AtlasCloud fp8; `response_format` + `structured_outputs`; `expiration_date 2026-12-31`; работает, но думает долго |
| `thinkingmachines/inkling:free`, `inkling-small:free` | отброшены | нет `response_format` в `supported_parameters` |
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | отброшена | нет `response_format`; uptime 30 мин 54 %, 1 д 73 %, status -5; на тесте `ResourceExhausted ... 16/16` |
| `nvidia/nemotron-3.5-content-safety:free` | отброшена | классификатор безопасности, не оценщик еды |
| `google/lyria-3-*-preview` | отброшены | музыкальные модели |
| `openrouter/free` | отброшена | роутер, выбирает случайную бесплатную модель; vision-маршрут им не закрепить |

Каталог меняется быстро (в `config.py:32-34` уже записано, что free-модели ротируются): список перепроверять `curl`-фильтром ниже.

```bash
curl -s https://openrouter.ai/api/v1/models | python3 -c '
import json,sys
for m in json.load(sys.stdin)["data"]:
    p=m["pricing"]
    if "image" in m["architecture"]["input_modalities"] and float(p["prompt"] or 0)==0 and float(p["completion"] or 0)==0:
        print(m["id"], m.get("expiration_date"), "response_format" in m["supported_parameters"])'
```

## Карточки кандидатов

### 1. Groq `qwen/qwen3.8-27b` (основной)

- Провайдер/ID: Groq, `qwen/qwen3.8-27b` (Alibaba Qwen 3.8, 27B, мультимодальная; режимы thinking/instruct; ~450 ток/с по доке модели).
- Формат картинки: OpenAI-стиль `{"type":"image_url","image_url":{"url": ...}}`; URL или `data:image/jpeg;base64,...` (https://console.groq.com/docs/vision). **Проверено base64 data URL** (JPEG 169–277 КБ после ресайза до 1280 px по длинной стороне — как фото Telegram).
- Лимиты запроса по доке: до 3 картинок, 20 МБ на запрос с URL (больше → 400). Лимит для base64 в доке **не указан** (см. «Что не проверено»). Фото Telegram ≤1280 px / сотни КБ — далеко от любых лимитов.
- JSON: `json_mode` в `supported_features`; **`response_format: {"type":"json_object"}` вместе с картинкой дал 200 и чистый JSON** (проверено 4 раза). `content` без `<think>`, поле `reasoning` в ответе не приходило.
- Лимиты free-плана (https://console.groq.com/docs/rate-limits): **30 RPM, 1K RPD, 8K TPM, 200K TPD**. Заголовки живого ответа подтвердили: `x-ratelimit-limit-requests: 1000`, `x-ratelimit-limit-tokens: 8000`.
- Цена картинки в лимитах: по доке 2048 входных токенов за картинку. Наблюдение: после первого запроса (usage всего 1123 токена) `x-ratelimit-remaining-tokens` упал с 8000 до 5739, то есть лимитер списал ≈2261 токена; значит на 8K TPM помещается ≈3 фото в минуту и не больше, независимо от реального `prompt_tokens`. TPD 200K — порядка 80 фото в день, если бы квота была только под фото.
- **Квота общая с текстом.** `qwen/qwen3.8-27b` — первый текстовый маршрут (`config.py:29`): фото и обычные сообщения делят 8K TPM и 200K TPD. Если фото-запрос использует большой промпт из `llm/prompts.py` (~300 строк), он съест большую часть минуты, следующее текстовое сообщение получит 429 и уйдёт на gpt-oss. Отсюда — короткий vision-промпт.
- Данные (https://console.groq.com/docs/your-data): по умолчанию Groq не хранит входы/выходы инференса (картинка не исключение: в доке про картинки отдельных правил нет); логи могут храниться до 30 дней только для разбора сбоев/злоупотреблений, отключается Zero Data Retention в Data Controls; обучение на данных клиента по условиям Groq запрещено (последнее — из поисковой выдачи по DPA, первоисточник DPA не открывался).
- Качество (n=2, мои запросы, не бенчмарк): см. таблицу «Живые запросы». Плов с говядиной/нутом/морковью узнан, лепёшка и салат выделены отдельными позициями; пельмени узнаны, но порция завышена (350 г при ~16–17 штуках; с подсказкой «250 г» модель взяла граммы из подписи). Данных по узбекской/казахской кухне вне этих двух фото нет.

### 2. OpenRouter `google/gemma-4-31b-it:free` и `...-26b-a4b-it:free` (запасные)

- Провайдер: OpenRouter → единственный endpoint Google AI Studio (`/models/{id}/endpoints`), `quantization: unknown`; контекст 262144; `max_completion_tokens 32768`.
- `supported_parameters`: `reasoning, include_reasoning, max_tokens, temperature, top_p, seed, response_format, tools, tool_choice`. **`structured_outputs` нет**, только `response_format` (json_object-режим; JSON-схема не гарантируется). Работа `response_format` с картинкой **не проверена** (все запросы получили 429).
- Формат картинки: как у всех на OpenRouter — URL или base64 data URL, типы `image/png`, `image/jpeg`, `image/webp`, `image/gif`; текст лучше ставить перед картинкой (https://openrouter.ai/docs/guides/overview/multimodal/image-understanding.md). Лимит размера в доке не указан.
- Лимиты OpenRouter для `:free`: **20 RPM; 50 RPD без покупки кредитов, 1000 RPD при ≥10 купленных кредитов** (https://openrouter.ai/docs/api-reference/limits). Лимит общий на аккаунт для всех `:free` (и текстовых, и vision: делится с текущим текстовым fallback). `GET /api/v1/key` у этого ключа: `is_free_tier: true`, `free_model_daily_requests: {limit: 50}`. Новые ключи лимит не повышают («governed globally»).
- **Реальный тест:** 6 запросов за ~5 минут (4 с фото на 31b, 1 с фото на 26b, 1 текстовый «Say ok» на 31b) — все 429, `limit_source: upstream_provider_shared_pool`, `provider_name: Google AI Studio`, «temporarily rate-limited upstream». То есть это не наш лимит 50/сутки, а общий пул Google для free-эндпоинта. Ответ приходит за 0.5–0.9 с, так что провал дешёвый.
- Данные: OpenRouter сам промпты не логирует без opt-in (https://openrouter.ai/docs/faq). Но free-эндпоинт Gemma обслуживает Google AI Studio; условия бесплатного Gemini API разрешают Google использовать содержимое для улучшения продуктов и чтение людьми (https://ai.google.dev/gemini-api/terms). Для фото еды из личного дневника это приемлемо, но надо понимать: «бесплатно» = данные уходят Google. Настройки приватности OpenRouter аккаунта могут вообще отключать такие endpoints (404 «no endpoints matching your data policy») — в тесте этого не было, 429.
- Качество: не измерено (нет ни одного успешного ответа). Gemma 4 заявлена как мультимодальная 31B dense с thinking-режимом (описание модели в каталоге OpenRouter).

### 3. OpenRouter `dots-studio/dots-3-note-preview:free` (запасной 2, с оговорками)

- Dots3-Note Preview, MoE 280B/16B активных; endpoint AtlasCloud fp8; контекст 512000; `expiration_date 2026-12-31` (через ~12 недель исчезнет — не делать основной).
- `supported_parameters` включает `response_format` и `structured_outputs`.
- Тест 1 (по умолчанию, `response_format: json_object`): 200 за **68.8 с**; `completion_tokens 5191`, из них `reasoning_tokens 3906`; ответ в `content` (чистый JSON), ещё поле `reasoning` (14.6K символов) и `reasoning_details`. Таймаут `httpx.AsyncClient(timeout=60)` в `OpenRouterClient.__init__` такой запрос оборвёт.
- Тест 2 (`"reasoning": {"enabled": false}` в теле): 200 за **4.2 с**, `reasoning_tokens 0`. Но модель ответила по-английски и положила `confidence` внутрь каждой позиции — инструкции соблюдает хуже, плов оценила 600 г / 850 ккал (в режиме с рассуждением: 390 г / 500 ккал). Если использовать — только с отключением reasoning и тем же схемным валидатором.
- Цена: `cost: 0` в `usage`. Счётчик `free_model_daily_requests` после двух успешных вызовов не изменился (1 из 50) — возможно, считает с задержкой; не полагаться.
- Данные: провайдер AtlasCloud, его политика не изучалась.

## Живые запросы (2026-10-08)

Общая схема: `system` — короткий русский промпт (формат `{"foods":[{description,grams,kcal,protein_g,fat_g,carbs_g}],"confidence":"low|medium|high"}`, «числа, не диапазоны»), `user` — `[{text}, {image_url: data:image/jpeg;base64,...}]`, `temperature 0`, `response_format json_object`, изображение сжато до 1280 px, JPEG q=85.

| Модель | Фото | Статус | Латентность | prompt / completion tokens | Результат |
|---|---|---|---|---|---|
| Groq qwen3.8-27b | плов+лепёшка+салат (169 КБ) | 200 | 1.3 с | 882 / 241 | плов 350 г 525 ккал, лепёшка 100 г 260, салат 120 г 45 (3 позиции) |
| Groq qwen3.8-27b | 17 пельменей (277 КБ) | 200 | 1.2 с | 1906 / 89 | «Варёные пельмени» 350 г 595 ккал Б35 Ж18 У70 (завышено) |
| Groq qwen3.8-27b | то же + подпись «17 штук, вес порции 250 г» | 200 | 1.1 с | 1913 / 64 | 250 г 325 ккал Б18 Ж10 У40, `confidence: high` |
| Groq qwen3.8-27b | сгенерированный синий прямоугольник (3 КБ, не еда) | 200 | 0.7 с | 1906 / 11 | `{"foods": [], "confidence": "low"}` — пустой список, не выдумывает |
| Groq `openai/gpt-oss-20b` | та же картинка | 400 | 0.3 с | — | `messages[1].content must be a string` |
| OR `ling-3.1-flash` | та же картинка | 404 | 0.3 с | — | `No endpoints found that support image input` |
| OR gemma-4-31b / 26b `:free` | плов, 5 попыток (+1 текстовая) | 429 | 0.5–0.9 с | — | `upstream_provider_shared_pool`, провайдер Google AI Studio |
| OR dots-3-note `:free` | плов | 200 | **68.8 с** | 1319 / 5191 (reasoning 3906) | плов 390 г 500 ккал, лепёшка 80 г 210, салат 120 г 40 |
| OR dots-3-note `:free`, reasoning off | плов | 200 | 4.2 с | 1322 / 114 | английский текст, `confidence` внутри позиций, плов 600 г 850 ккал |

Сырая форма ответа Groq (одинакова для OpenAI-совместимых): `choices[0].message = {"role":"assistant","content":"{\n  \"foods\": [ ... ],\n  \"confidence\": \"medium\"}"}`, `finish_reason: "stop"`, `usage = {prompt_tokens, completion_tokens, total_tokens, queue_time, prompt_time, completion_time, total_time}`. Ключи `message` у Groq: `role, content`; у OpenRouter/dots: `role, content, refusal, reasoning, reasoning_details`.

Заметки по токенам: у `prompt_tokens` картинки нет фиксированного размера (882 для 1280×720, 1906 для 1280×980 и для картинки 320×240) — не вычислять расход по формуле, смотреть `x-ratelimit-remaining-tokens`.

## Как подключить в клиент с fallback по маршрутам

Идея: фото → один vision-вызов с коротким промптом → `ParseResult(kind="food", foods=[...])` → тот же `process_text`-путь превью с кнопкой подтверждения. Ничего в базе не меняется.

1. **Настройки** (`config.py`): новый список `vision_models` для Groq (по умолчанию `["qwen/qwen3.8-27b"]`) и `openrouter_vision_models` (по умолчанию `["google/gemma-4-31b-it:free", "google/gemma-4-26b-a4b-it:free"]`, переопределяется в `.env`, как `OPENROUTER_MODEL`; free-список ротируется). Нельзя брать маршруты из `routes_from` как есть — туда входят gpt-oss и ling, которые картинку отвергнут 400/404.
2. **Маршруты**: `vision_routes_from(settings)` по образцу `routes_from` (Groq первым, затем OpenRouter); `_over_routes` принимает список маршрутов параметром (сейчас берёт `self.routes`), поведение 429/cooldown/`NEXT_ROUTE_STATUSES` переиспользуется. `413` для большой картинки там уже есть.
3. **Тип сообщений**: `_complete(route, messages: list[dict[str, Any]], ...)`; для vision `content` = `[{"type":"text",...},{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}]`. Текст ставить **перед** картинкой (рекомендация OpenRouter).
4. **Метод**: `async def parse_photo(self, image_b64: str, caption: str, mime="image/jpeg") -> ParseResult` — строит сообщения коротким `VISION_SYSTEM` (в `llm/prompts.py`, отдельно от большого текстового промпта: иначе TPM Groq съедается), вызывает `_over_routes(call, json_mode=True, routes=vision_routes)`. Результат оборачивается: `ParseResult(kind="food", foods=data["foods"])` (`unknown_terms` — для названий, которые модель не опознала; ключ `confidence` в `ParseResult` не нужен — игнорировать или показать в превью).
5. **Таймаут**: в `OpenRouterClient.__init__` `timeout=60`. Для vision задавать на запрос `timeout=20` — Groq отвечает за ~1–2 с, зависший бесплатный endpoint (dots с reasoning — 69 с) не должен держать пользователя.
6. **`response_format`**: оставить `json_object` (как у текстовых вызовов); если маршрут вернёт 400 с «response_format/structured/json», `_rejects_json_mode` повторит без него — проверено на логике, что 400 про картинку (`must be a string`) под эти слова не попадает и сразу идёт к следующему маршруту.
7. **Хендлер** `handlers/photo.py`: `@router.message(F.photo)`; брать `message.photo[-1]` (самый большой размер, Telegram даёт до 1280 px), `await bot.download(photo)` → base64; подпись (`message.caption`) передавать как подсказку («250 г», «без масла» — проверено: граммы из подписи модель использует). Далее — как голос: `process_text`-подобный путь с `raw_text="[photo] <подпись>"` и префиксом «Вижу на фото: ...», чтобы пользователь видел распознанные блюда до подтверждения. Зарегистрировать `photo.router` в `main.py` рядом с `voice.router`, до `log_text.router`.
8. **Подтверждение обязательно** (CLAUDE.md: запись из свободного текста только после кнопки; `raw_text` сохраняется). Для фото оценка порции особенно неточна (пельмени 350 г вместо ~250 г) — в превью показывать граммы, дать кнопку/ответ-правку (механика правки превью уже есть в `log_text.py`: «исправь на 250 г» через историю диалога).
9. **Свободные бонусы без нового кода**: `ParsedFood._kcal_from_macros` исправляет kcal; `_foods_without_numbers` отправляет неузнанные блюда в `food_lookup`; для не-еды модель вернула `foods: []` → `ParseResult` с `kind="food"` и пустым `foods` не считается записью (см. `schemas.py` ~стр. 166: `bool(self.foods)`; не прогонялось) → можно ответить «не нашёл еду на фото».
10. **Тесты**: подменять `httpx` (как существующие тесты клиента); без живых вызовов.

## Шаблоны (проверен только сам запрос; код клиента и хендлера — набросок, не запускался)

Запрос, который прошёл на Groq (curl-эквивалент; `$GROQ_KEY` — ключ из `.env`, не вставлять в логи):

```bash
IMG=$(base64 -w0 plov.jpg)   # JPEG до 1280 px
curl -s https://api.groq.com/openai/v1/chat/completions \
  -H "Authorization: Bearer $GROQ_KEY" -H "Content-Type: application/json" \
  -d @- <<EOF
{"model":"qwen/qwen3.8-27b","temperature":0,"response_format":{"type":"json_object"},
 "messages":[
  {"role":"system","content":"<VISION_SYSTEM>"},
  {"role":"user","content":[
    {"type":"text","text":"Оцени порцию."},
    {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,$IMG"}}]}]}
EOF
```

Промпт `VISION_SYSTEM`, на котором получены все результаты выше (короткий, ~100 токенов; в проекте его стоит дополнить правилом «если на фото нет еды — `foods: []`», хотя пустой список модель вернула и без него):

```text
Ты оцениваешь еду по фото для дневника питания. Определи блюда на фото и примерный вес порции.
Ответь ТОЛЬКО JSON: {"foods":[{"description":str,"grams":number,"kcal":number,"protein_g":number,
"fat_g":number,"carbs_g":number}],"confidence":"low|medium|high"}.
Одна запись на блюдо или компонент. Числа, не диапазоны.
```

Набросок клиента (не запускался; имена `vision_routes`, `parse_photo` — предложение):

```python
async def parse_photo(self, image_b64: str, caption: str = "") -> ParseResult:
    messages = [
        {"role": "system", "content": VISION_SYSTEM},
        {"role": "user", "content": [
            {"type": "text", "text": caption or "Оцени порцию."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ]},
    ]

    async def call(route: Route, json_mode: bool) -> ParseResult:
        content = _THINK.sub("", await self._complete(route, messages, json_mode, use_reasoning=False))
        data = extract_json(content, prefer="foods")
        return ParseResult(kind="food", foods=data.get("foods", []))

    return await self._over_routes(call, json_mode=True, routes=self.vision_routes)
```

`extract_json(..., prefer="foods")` ищет первый объект с ключом `foods` (механика `prefer` уже есть, `openrouter.py:38`). `use_reasoning=False` обязателен: иначе при пустом `content` клиент подставит `reasoning` (цепочку рассуждений) как ответ.

Набросок хендлера (не запускался):

```python
@router.message(F.photo)
async def log_photo(message: Message, settings: Settings, sessionmaker: Sessionmaker, llm: OpenRouterClient) -> None:
    photo = message.photo[-1]
    buf = await message.bot.download(photo)           # BytesIO, JPEG, <= 1280 px
    b64 = base64.b64encode(buf.getvalue()).decode()
    try:
        result = await llm.parse_photo(b64, message.caption or "")
    except LLMError:
        await message.answer("Не получилось разобрать фото, напиши текстом.")
        return
    # then the same preview + confirm button as process_text, raw_text="[photo] <caption>"
```

## Подводные камни

- **Квота Groq общая.** Фото и текст делят `qwen/qwen3.8-27b`: 8K TPM, 200K TPD. Тест съел ≈2.3K TPM за запрос (вероятно 2048 за картинку + текст). 4 фото подряд за минуту → 429, и текстовые сообщения уйдут на gpt-oss.
- **OpenRouter free = 50 запросов в сутки на аккаунт** (у ключа проекта `is_free_tier: true`), общие с текстовым fallback; 1000 только после покупки ≥10 кредитов. Gemma free часть времени отвечает 429 из общего пула Google.
- **Reasoning-модели.** dots с reasoning: 69 с и 5K токенов. Для vision передавать `reasoning: {"enabled": false}` там, где модель его поддерживает; для Groq qwen `content` приходил чистым. Параметр `reasoning_effort` для Groq qwen в этой работе не проверялся.
- **Модель принимает текст-only маршрут как ошибку.** Не смешивать vision и text маршруты (см. 400/404 выше).
- **Оценка порции — главная неточность**, а не распознавание блюда: пельмени 350 г при ~250 г. Подпись с граммами заметно помогает. Вес в превью обязательно показывать.
- **Приватность.** Groq — не хранит по умолчанию (логи ≤30 дней для разбора сбоев; можно ZDR). OpenRouter free-endpoint Gemma — Google AI Studio, free-условия допускают использование данных для улучшения продуктов. Фото еды не секрет, но фото с людьми/документами в кадре отправлять не стоит; ключи и промпты проекта к фото не добавлять.
- **Размер.** Не слать оригинал: Telegram уже даёт ≤1280 px (`photo[-1]`); если файл больше (документом), ужать через Pillow до 1280 px, JPEG q≈85 (в тесте 170–280 КБ). Pillow в зависимостях проекта нет (`bot/pyproject.toml`) — для обычных фото он не нужен.
- **Каталог ротируется.** ID free-моделей на OpenRouter исчезают (`dots-3-note` — до 2026-12-31). Держать список в `.env`, при «all models failed» перепроверять curl-ом выше.
- **Региональная доступность.** Gemini API напрямую недоступен не везде: в списке регионов https://ai.google.dev/gemini-api/docs/available-regions есть Казахстан, Киргизия, Таджикистан, Узбекистан, России в списке нет. Работает ли он, зависит от региона сервера (EC2, `deploy/setup-ec2.sh` регион не фиксирует), а не от пользователя; на free tier EEA/Швейцария/Великобритания использовать только платный Gemini.

## Чеклист «когда что использовать»

- Обычное фото еды, пользователь сидит и ждёт ответа: Groq `qwen/qwen3.8-27b`, короткий промпт, подпись → подсказка.
- Groq ответил 429/недоступен: следующий vision-маршрут OpenRouter (`gemma-4-31b-it:free`, потом `26b`), таймаут короткий; если и они 429 — сказать пользователю «лимит разбора фото исчерпан, напиши текстом» (как `RATE_LIMITED` в `handlers/voice.py`).
- Нужен гарантированно структурированный JSON по схеме: у Groq qwen по доке `json_mode` (а схема в доке vision не заявлена); в OpenRouter смотреть `structured_outputs` в `supported_parameters` (у dots есть, у Gemma нет).
- Нужно больше 50 фото в сутки через OpenRouter: купить ≥10 кредитов (1000/сутки) — это уже не «ноль рублей» разово; либо отдельный платный ключ.
- Нужна более сильная модель/запас лимитов: добавить Gemini Flash (новый провайдер, ключ Google AI Studio, `https://generativelanguage.googleapis.com/v1beta/openai/`) — но сначала проверить регион и принять условия free tier.
- Не подставлять текстовые модели (`gpt-oss-*`, `ling-*`) в vision-маршруты.

## Другие провайдеры (кратко)

- **Gemini API напрямую** (`https://generativelanguage.googleapis.com/v1beta/openai/`): по документации поддерживает картинки как base64 data URL, структурированные выходы, модели `gemini-3.8-flash`, `3.7`, `3.6`, `3.5`, `2.5 Flash/Pro/Flash-Lite` и Gemma 4 с бесплатным уровнем (https://ai.google.dev/gemini-api/docs/pricing). Числа лимитов free-уровня в документации не публикуются (только в AI Studio, https://aistudio.google.com/rate-limit). Данные free tier используются Google и читаются людьми (https://ai.google.dev/gemini-api/terms). **Не проверено запуском: ключа в `.env` нет.** Если добавлять — это новый `Route("gemini", base_url, key, model)`: клиент менять почти не придётся (OpenAI-совместимая `/chat/completions`).
- **GitHub Models** — закрыт 2026-07-30 (https://docs.github.com/en/github-models/use-github-models/prototyping-with-ai-models). Не рассматривать.
- **Cloudflare Workers AI**: 10 000 neurons/сутки бесплатно; `@cf/meta/llama-3.2-11b-vision-instruct`; есть OpenAI-совместимый `/v1/chat/completions` (`https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1`). Нужен аккаунт Cloudflare и `account_id`; работу с картинками через этот endpoint и качество не проверял.
- **Mistral La Plateforme** (бесплатный «Experiment» tier, vision-модели): первичная документация не открылась (docs.mistral.ai/deployment/... вернул 404), данные только из сторонних статей (низкий RPM, обучение на данных на free-плане) — как указатель, не как факт.

## Что не проверено

- Успешный ответ `google/gemma-4-31b-it:free` / `26b` с картинкой и `response_format` (все 5 попыток с фото и 1 текстовая — 429 upstream). Качество Gemma на еде не измерено.
- Реальное поведение `response_format: json_object` на Gemma с картинкой.
- Максимальный размер base64-картинки на Groq (в доке — только 20 МБ для URL и 3 картинки). Не тестировал большие файлы.
- Параметр `reasoning_effort` на Groq qwen3.8-27b для vision (в карточке модели упомянут «tunable reasoning effort»).
- Лимиты free-уровня Gemini API (RPM/RPD/TPM) — нет в публичной документации; работа Gemini API с этим проектом (нет ключа) и доступность из региона сервера.
- Качество на узбекской/казахской/русской кухне: есть только мои 2 фото (плов и пельмени) и 1 не-еда; других данных/бенчмарков в первичных источниках не найдено.
- Mistral: лимиты и политика данных (404 на docs.mistral.ai, только вторичные источники).
- Cloudflare Workers AI: картинка через OpenAI-совместимый endpoint, лимит нейронов на фото, политика данных.
- Политика данных AtlasCloud (endpoint dots) и условия обучения на данных у Groq сверх `your-data` (DPA не открывался).
- Работа счётчика `free_model_daily_requests` OpenRouter: после двух успешных вызовов dots он остался «1 из 50» — возможно, лаг или отдельный учёт.
- Реальная интеграция: в код проекта ничего не добавлялось; фрагменты раздела «Как подключить» — проектное предложение, не прогнанное через `pytest`.
