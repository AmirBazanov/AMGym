# MCP Python SDK (пакет `mcp`) — справочник для агентов
Дата: 2026-10-07. Версия: в проекте **не установлена** (`bot/uv.lock` не содержит `mcp`); проверено на `mcp==2.3.0` (PyPI, релиз 2026-10-02, `requires-python >=3.10`; проект — `>=3.12`, совместимо). Источники:
- PyPI: https://pypi.org/project/mcp/ ; JSON API https://pypi.org/pypi/mcp/json (список релизов)
- Документация SDK v2 (стабильная линия): https://py.sdk.modelcontextprotocol.io/ , страницы `run/asgi/`, `run/authorization/`, `get-started/testing/`, `migration/` (https://py.sdk.modelcontextprotocol.io/v2/migration/)
- Репозиторий: https://github.com/modelcontextprotocol/python-sdk
- Установленный пакет (исходники `mcp/server/mcpserver/server.py`, `mcp/server/lowlevel/server.py`, `mcp/server/streamable_http_manager.py`, `mcp/server/transport_security.py`, `mcp/server/auth/*`, `mcp/client/*`) — в scratch-venv, см. «Что подтверждено запуском».
- Спецификация MCP 2025-11-25, Streamable HTTP: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- Claude Code: https://code.claude.com/docs/en/mcp
- Claude.ai custom connectors: https://claude.com/docs/connectors/custom/remote-mcp , https://claude.com/docs/connectors/building/authentication , https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
- Caddy: https://caddyserver.com/docs/caddyfile/directives/reverse_proxy

Примеры в разделе «Готовый пример» проверены на **копии `bot/` проекта** (scratch-каталог вне репозитория): с добавленным кодом `pytest` — 743 теста зелёные (в т.ч. 5 новых про MCP и все существующие), `ruff check src tests` чистый; Python 3.13, версии fastapi/starlette/uvicorn/pydantic/sqlalchemy — те же, что в `bot/uv.lock`. В самом проекте ничего не менялось.

## Главное в трёх строках
1. С 2026-07-28 последняя линия — **mcp 2.x**; `FastMCP` переименован в `MCPServer`, старый импорт `mcp.server.fastmcp` падает с `ModuleNotFoundError`. Большинство статей, ответов моделей и README в сети описывают v1 — не копировать их вслепую.
2. Встраивание в FastAPI: **свой lifespan с `mcp.session_manager.run()` обязателен**, `app.mount` запускает только маршруты, но не lifespan подприложения.
3. Для статического Bearer-токена встроенная авторизация SDK не годится (она про OAuth-resource-server); ставить **свой ASGI-middleware**, а Host-заголовок разрешать через `TransportSecuritySettings(allowed_hosts=...)`, иначе за Caddy будет 421.

## Как это относится к проекту
- `mcp` в зависимостях нет. Добавить в `bot/pyproject.toml`: `"mcp>=2.3,<3"`. `uv pip install --dry-run` с зависимостями проекта (aiogram, sqlalchemy[asyncio], fastapi, uvicorn[standard], pytest-asyncio…) + `mcp>=2.3,<3` резолвится без конфликтов; locked-версии starlette 1.7.0, pydantic 2.13.5, anyio 4.15.1, uvicorn 0.54.0 подходят. `mcp` 2.x **тянет `httpx2`, `mcp-types`, `sse-starlette`, `pyjwt[crypto]`, `python-multipart`, `jsonschema`, `opentelemetry-api`** (METADATA пакета); проектный `httpx` остаётся рядом, оба импортируются в одном venv (проверено). Линия 1.x жива (1.30.0 от 2026-09-07) — `mcp<2` оставляет `FastMCP`.
- Приложение: `bot/src/gymbot/api/app.py::create_app(settings, sessionmaker, llm, routers)` строит `FastAPI(...)` **без lifespan**; `routers` принимает только `APIRouter`, а мини-апп монтируется последним `app.mount("/", StaticFiles(...))` (строки ~440). Роуты MCP надо добавить **до** этого `mount("/")`, иначе их перекроет статика. Есть `@app.middleware("http") no_cache_index` (BaseHTTPMiddleware) — он оборачивает и ответы `/mcp` (добавляет `Cache-Control: no-cache`; с POST/JSON работает, проверено тестами; с SSE-потоком GET не проверялось).
- Процесс: `bot/src/gymbot/main.py` запускает `uvicorn.Server(uvicorn.Config(create_app(...), host=settings.api_host, port=settings.api_port, proxy_headers=True))` и сам вызывает `server.serve()`. Lifespan FastAPI uvicorn выполняет штатно (`lifespan="auto"`), так что `session_manager.run()` в lifespan приложения сработает.
- БД: `gymbot.db.session.make_engine` → `async_sessionmaker`. Инструменты MCP должны быть **`async def`** и брать сессию из замыкания на `sessionmaker` (как `get_session` в `create_app`). Синхронные `def`-инструменты SDK гонит в `anyio.to_thread` (видно в traceback) — для async SQLAlchemy это ошибка.
- Деплой: `deploy/Caddyfile` — `reverse_proxy 127.0.0.1:__PORT__` с `encode gzip` на весь домен; Host пробрасывается как есть (`PUBLIC_URL` = `https://домен`). `setup-ec2.sh` умеет и Cloudflare-tunnel (`EDGE=tunnel`, по умолчанию) — для него Host тоже публичный домен (не проверялось).
- Тесты: `bot/tests/conftest.py::make_client` строит **свежее** `create_app(...)` на каждый тест и ходит через `httpx.ASGITransport` с `base_url="http://t"` → Host `t`; ASGI-lifespan при этом **не запускается**. Следствия: (а) `StreamableHTTPSessionManager.run()` можно войти только один раз на экземпляр — `MCPServer` нельзя держать модульной глобалью, строить его внутри `create_app`; (б) в тестах lifespan надо входить руками; (в) Host `t` надо разрешить или получить 421.
- Settings (`gymbot/config.py`): нужен `mcp_token: str = ""` (пусто = MCP не монтируется) и использовать уже существующий `public_url` для `allowed_hosts`. Токен — только из `.env` (правило проекта про секреты).

## v1 → v2: таблица соответствий (проверено по источникам и запуском)
| Что | v1 (1.30.0) | v2 (2.3.0) |
|---|---|---|
| Класс сервера | `from mcp.server.fastmcp import FastMCP` | `from mcp.server.mcpserver import MCPServer` (также `from mcp.server import MCPServer`). `mcp.server.fastmcp` → `ModuleNotFoundError` с подсказкой |
| `stateless_http`, `json_response`, `streamable_http_path`, `transport_security`, `host`… | параметры конструктора `FastMCP(...)` | параметры `mcp.streamable_http_app(...)` / `mcp.run(...)`; в `MCPServer.__init__` их нет |
| Клиент Streamable HTTP | `from mcp.client.streamable_http import streamablehttp_client` + `ClientSession(read, write)`; свой httpx через `httpx_client_factory=`, заголовки через `headers=` | `from mcp.client.streamable_http import streamable_http_client` (`url, *, http_client: httpx2.AsyncClient`), заголовки — в `httpx2.AsyncClient(headers=...)`; высокоуровневый `from mcp import Client` |
| HTTP-библиотека клиента | `httpx` | `httpx2` (`import httpx2`) |
| Контекст в инструменте | `mcp.get_context()` | параметр `ctx: Context` (`from mcp.server.mcpserver import Context`) |
| Декоратор | `@mcp.tool()` | `@mcp.tool()` — **скобки обязательны**, `@mcp.tool` без скобок даёт `TypeError` (проверено) |

Источник: https://py.sdk.modelcontextprotocol.io/v2/migration/ ; сверено с сигнатурами в установленных пакетах обеих линий. Пример на v1 (проверен на 1.30.0: `FastMCP("g", json_response=True, stateless_http=True, transport_security=...)`, `app.router.routes.extend(sub.routes)`, `lifespan` с `mcp.session_manager.run()`, клиент `streamablehttp_client` + `ClientSession`) отличается от v2-кода ниже только перечисленным. Дальше — **v2**.

## Объявление tools, resources, prompts
Все декораторы на `MCPServer` (`mcp/server/mcpserver/server.py`):
```python
mcp = MCPServer("gymapp", instructions="Личный дневник зала владельца.")

@mcp.tool()                        # name=, title=, description=, annotations=ToolAnnotations(read_only_hint=True), structured_output=
async def day_summary(day: str, limit: int = 10) -> Summary:   # pydantic-модель -> structuredContent + outputSchema
    """Сводка за день (YYYY-MM-DD)."""     # docstring -> description инструмента

@mcp.resource("gym://programs/{slug}")      # шаблон URI -> аргументы функции; возврат str | bytes | иное -> JSON
def program(slug: str) -> str: ...

@mcp.prompt()                                # возврат: str | Message | список
def review(week: int) -> str: ...
```
Проверено запуском (клиент `Client(...)`):
- JSON-схема аргументов строится из аннотаций: `{'properties': {'day': {'type': 'string'}, 'limit': {'default': 10, 'type': 'integer'}}, 'required': ['day']}`; docstring → `description`.
- Возврат pydantic-модели или `dict[str, int | str]` → `structured_content` = `{'day': '2026-10-07', 'total': 1}` и копия JSON в `content[0].text`. Возврат `str` → `structuredContent {'result': '...'}`; `list[str]` → `{'result': [...]}`; нетипизированный `dict` → только текст, `structured_content=None`.
- Ошибки: любое обычное исключение в инструменте → `is_error=True`, текст **только** `Error executing tool boom` (сообщение скрыто, в лог — traceback). Чтобы модель увидела причину, бросать `from mcp.server.mcpserver.exceptions import ToolError`: `raise ToolError("x must be >= 0")` → `is_error=True`, `Error executing tool f: x must be >= 0`. Неверные аргументы → `is_error=True` с текстом pydantic-валидации (не исключение у клиента).
- Для ресурсов: `ResourceError` / `ResourceNotFoundError` из того же модуля; иное исключение — обобщённая ошибка.
- Тип результата с `is_error` — это **результат**, а не исключение клиента: в тестах проверять `res.is_error`.
- Для read-only инструментов дневника ставить `annotations=ToolAnnotations(read_only_hint=True)` (`from mcp.types import ToolAnnotations`; проверено, поле доходит до клиента) — клиенты вроде Claude используют это для запросов подтверждения.

**Как это относится к проекту:** писать инструменты как тонкие обёртки над существующими `gymbot/services/*` (`workouts`, `nutrition`, `plan`, `facts`…), а не дублировать SQL. Пользователь один (владелец): внутри инструмента брать пользователя по `settings.allowed_user_ids`/`gymbot.services.access`/`get_or_create_user`, а не из токена (статический токен идентифицирует организацию, не человека). Запись данных — только после явного подтверждения (правило CLAUDE.md про запись); для MCP это решает клиент (подтверждение вызова инструмента), но имена и описания инструментов-записей должны это отражать.

## Подключение к существующему FastAPI: маршрут, mount, lifespan
Реальные результаты (httpx ASGITransport, mcp 2.3.0, запрос `POST` initialize; Accept обязан содержать `application/json, text/event-stream`):

| Вариант | `POST /mcp` | `POST /mcp/` |
|---|---|---|
| `app.mount("/mcp", mcp.streamable_http_app(streamable_http_path="/"))` | **307** → `/mcp/` | доходит до MCP (в пробе 421 из-за Host, см. ниже) |
| `app.mount("/", mcp.streamable_http_app())` (путь по умолчанию `/mcp`) | работает | 307 → `/mcp` |
| `app.mount("/mcp", mcp.streamable_http_app())` | (из кода, запуском не проверялось) путь станет `/mcp/mcp` | — |
| **`app.router.routes.extend(sub.routes)`** (рекомендуется) | **работает точно на `/mcp`** | падает дальше в `StaticFiles` → 405 |

- 307 на POST — клиентам SDK ок (редирект в пределах origin с сохранением метода допускается, см. docstring `streamable_http_client`), но чужие клиенты (Claude.ai, прокси) могут не пойти за редиректом. Mount на `"/"` перехватывает **все** пути (по логике Starlette остаток не доходит до последующего `StaticFiles`, мини-апп сломается; именно эту связку запуском не проверял), поэтому для нашего приложения лучше добавить только маршруты подприложения.
- `sub = mcp.streamable_http_app(...)` возвращает `Starlette`; у него один `Route("/mcp")` (в варианте без SDK-авторизации). Список `sub.routes` вставляется в `app.router.routes` **до** `app.mount("/", StaticFiles)`. Проверено: `/api/health` и статика продолжают работать, `/mcp` отвечает.
- Документация SDK для Starlette (`run/asgi/`) показывает `Mount("/", app=mcp.streamable_http_app())`; наш вариант — осознанное отступление ради мини-аппа.

### Lifespan и session manager (обязательно)
- «A mounted sub-application's lifespan never runs» (docs `run/asgi/`). Без входа в `mcp.session_manager.run()` запрос падает: `RuntimeError: Task group is not initialized. Make sure to use run().` (воспроизведено).
- `mcp.session_manager` доступен только **после** вызова `mcp.streamable_http_app()` (иначе `RuntimeError`, docstring свойства), а `run()` можно войти **один раз** на экземпляр (docstring `StreamableHTTPSessionManager`). Поэтому: `MCPServer` строится внутри `create_app`, не на уровне модуля; тест с двумя последовательными `create_app` проходит (проверено).
- `create_app` сейчас не имеет lifespan; передать его конструктору: `FastAPI(..., lifespan=lifespan)` (`lifespan=None` допустим, когда MCP выключен).
- Если в будущем появится свой lifespan у приложения — объединить через `AsyncExitStack` (docs: «Multiple Servers»).

## Stateless и json_response
- `streamable_http_app(json_response=True, stateless_http=True)` (в v2 — параметры метода). Источники: migration guide; сигнатура `MCPServer.streamable_http_app` (параметры: `streamable_http_path="/mcp"`, `json_response=False`, `stateless_http=False`, `event_store`, `retry_interval`, `max_request_body_size` (4 МиБ), `session_idle_timeout` (1800 с), `max_sessions` (10 000), `transport_security`, `host="127.0.0.1"`).
- Stateless: каждый запрос — новый транспорт, без `Mcp-Session-Id`; `tools/call` работает даже **без** предварительного `initialize` (проверено curl: ответ `200` с результатом). Подходит нам: один процесс, инструменты без server→client запросов (sampling/elicitation), нечего хранить между запросами, рестарт сервиса не рвёт «сессии».
- `json_response=True`: ответ на POST — один `application/json`, без SSE; не мешает прокси/Caddy и таймаутам. Server→client уведомления и запросы при этом недоступны — нам не нужны.
- Наблюдение: `GET /mcp` с `Accept: text/event-stream` даже в stateless-режиме отвечает `200 text/event-stream` и держит поток открытым (в тесте через ASGITransport это **зависло** без ответа — не делать GET в тестах); спецификация допускает вместо этого 405 (https://modelcontextprotocol.io/specification/2025-11-25/basic/transports, «Listening for Messages from the Server»). `DELETE /mcp` → 405.
- В заголовках SDK отдаёт `x-accel-buffering: no`, `cache-control: no-cache, no-transform` для SSE.

## Host/Origin: DNS-rebinding защита (421 за Caddy)
- `streamable_http_app(host="127.0.0.1")` по умолчанию, и **если `transport_security` не передан и `host` — localhost, включается защита с разрешёнными только `127.0.0.1:*`, `localhost:*`, `[::1]:*`** (`mcp/server/lowlevel/server.py`). Запрос с другим `Host` получает **421 `Invalid Host header`** (воспроизведено: `example.com`, `evil.com`, и `testserver` у httpx-теста).
- Поэтому за Caddy (Host = публичный домен) и в pytest (Host `t`) нужно явно: `TransportSecuritySettings(allowed_hosts=["127.0.0.1:*", "localhost:*", "gym.example.com"], allowed_origins=[])`. Шаблон `host:*` разрешает любой порт; значение без `:*` — точное совпадение с заголовком `Host` (с портом, если он есть). Проверено: `Host: gym.example.com` → 200, `Host: evil.com` → 421.
- `Origin` проверяется только если заголовок есть (браузер); не-браузерные клиенты (httpx, curl, SDK-клиент) его не шлют; для Claude Code и Claude.ai это предположение, не проверялось. Невалидный Origin → 403. Спецификация требует проверять Origin (MUST).
- Альтернатива: `TransportSecuritySettings(enable_dns_rebinding_protection=False)` — Bearer-токен всё равно закрывает доступ, но проверка Host — бесплатная защита; оставить включённой.
- `Content-Type` для POST валидируется всегда (`application/json`, иначе 400 `Invalid Content-Type header`).

## Авторизация по статическому Bearer-токену
### Вариант A (рекомендуется): свой ASGI-middleware
Чистый ASGI (не `BaseHTTPMiddleware`, не ломает стриминг), охраняет только `/mcp`, `hmac.compare_digest`, без токена — 401 с `WWW-Authenticate: Bearer`. Код — в разделе «Готовый пример». Проверено: нет токена / неверный токен → 401, верный → 200, `/api/health` и статика открыты, Host проверяется уже после авторизации.
- Подключать `app.add_middleware(BearerGuard, token=...)` до старта приложения. Порядок: последний добавленный `add_middleware` — внешний; вместе с существующим `@app.middleware("http") no_cache_index` всё работает (полный набор тестов проекта + `test_mcp.py` зелёный на копии).
- Важно: токен сравнивать в постоянное время, пустой токен в Settings = MCP не монтируется (иначе `Bearer ` с пустым значением совпал бы).

### Вариант B: встроенная авторизация SDK (для OAuth; для статики не рекомендуется)
- `MCPServer("name", token_verifier=Verifier(), auth=AuthSettings(issuer_url=..., resource_server_url=..., validate_token_resource=False))`. `TokenVerifier` — протокол с одним методом `async def verify_token(self, token: str) -> AccessToken | None`; `AccessToken(token, client_id, scopes, expires_at=None, resource=None, subject=None, claims=None)`. `token_verifier=` и `auth=` обязаны идти вместе (иначе `ValueError`). Источник: https://py.sdk.modelcontextprotocol.io/run/authorization/ («The SDK gives you the resource-server half: verify, advertise, refuse. It does not give you a login page, a consent screen, or a token.»).
- Запуск (проверено): 401 с `WWW-Authenticate: Bearer error="invalid_token", ..., resource_metadata="https://.../.well-known/oauth-protected-resource/mcp"`; приложение отдаёт `/.well-known/oauth-protected-resource/mcp` с `authorization_servers: [issuer_url]`, но реального authorization server нет (`/.well-known/oauth-authorization-server` → 404). Клиент, увидев `resource_metadata`, уйдёт в OAuth-discovery и не найдёт сервер авторизации.
- **Подвох:** `AuthenticationMiddleware` лежит в middleware самого Starlette-подприложения, поэтому при `app.router.routes.extend(sub.routes)` он **не применяется**: верный токен получил **401** (воспроизведено). Работает только при `app.mount("/", sub)` целиком (там получился 200) — а это ломает статику (см. выше).
- Если `AuthSettings(resource_server_url=...)` и `validate_token_resource` не задан — `MCPDeprecationWarning` (в 3.0 станет `True`).
- Вывод: для одного владельца и статического токена вариант A проще и надёжнее. B нужен, только если когда-нибудь появится настоящий OAuth.

## Как подключаются клиенты
### Claude Code (https://code.claude.com/docs/en/mcp)
```bash
claude mcp add --transport http gymapp https://<домен>/mcp --header "Authorization: Bearer <токен>"
# области: --scope local (по умолчанию, ~/.claude.json) | project (.mcp.json, в git!) | user
claude mcp list ; claude mcp get gymapp ; /mcp   # статус внутри Claude Code
```
- Для `.mcp.json` в репозитории токен хранить через подстановку окружения: `"headers": {"Authorization": "Bearer ${GYM_MCP_TOKEN}"}` (синтаксис `${VAR}` и `${VAR:-default}` из docs). Токен в git не коммитить.
- Таймауты (docs): `MCP_TIMEOUT` (запуск сервера, мс), `MCP_TOOL_TIMEOUT` (выполнение инструмента, мс), `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` (по умолчанию 5 мин для HTTP), в `.mcp.json` — `"timeout"` на сервер.
- Я **не** запускал `claude mcp add` (это меняет конфигурацию пользователя) — команда взята из документации и помечена как проверенная только по тексту. Проверено, что сам сервер отвечает на тот же заголовок через curl и клиент SDK.

### Claude.ai (custom connectors; https://claude.com/docs/connectors/custom/remote-mcp)
- Соединение идёт **из облака Anthropic**, а не с устройства: сервер должен быть доступен из интернета с диапазона `160.79.104.0/21` (https://platform.claude.com/docs/en/api/ip-addresses). Локальный сервер/VPN не подойдёт.
- Поддержка: **без авторизации (No sign-in), OAuth 2.0 (DCR или CIMD), и «Request headers» для фиксированных ключей — последний в бете и доступен ограниченному кругу организаций** (в диалоге нет раздела «Request headers», если организации не открыли доступ). Если доступен: «Claude sends the value exactly as you enter it. It doesn't add an authentication scheme» — в поле `Authorization` вводить `Bearer <токен>` целиком; до 4 заголовков; заголовок `authorization`, `x-api-key`, `x-auth-token` разрешены всем; изменить заголовки после создания нельзя (удалить и добавить заново).
- Токен в URL (`?token=`): Anthropic прямо запрещает серверам его принимать («Never accept it in the URL»); спецификация MCP запрещает access token в query-строке. Токен в **пути** URL документацией не описан — не использовать.
- Транспорт: Streamable HTTP поддерживается (SSE — устаревающий); URL вводится как `https://домен/mcp`. В поле URL вводится HTTPS-адрес endpoint (пример из docs: `https://mcp.example.com/mcp`); у нас — ровно `/mcp`, без завершающего слэша.
- OAuth-требования Claude (если когда-нибудь B): 401 с `resource_metadata`, `resource` в PRM равен URL точно, PKCE S256, callback `https://claude.ai/api/mcp/auth_callback`, ответы discovery/token ≤10 с (docs `building/authentication`).
- **Следствие для нас:** на личном плане Free/Pro/Max статический Bearer в Claude.ai может быть недоступен (бета). Если раздела «Request headers» нет — остаются «No sign-in» (небезопасно: любой, кто знает URL, получает доступ к дневнику) или OAuth. Claude Code со статическим заголовком работает точно.

## Тестирование из Python
Три уровня, все проверены запуском:
1. **В памяти, без HTTP**: `async with Client(mcp, raise_exceptions=True) as c:` — `Client` принимает сам `MCPServer` (docs `get-started/testing/`). `raise_exceptions=True` показывает настоящие тексты ошибок. Подходит для логики инструментов; проверка Bearer/Host не затрагивается. Нужна функция-фабрика `build_mcp(sessionmaker)`.
2. **HTTP через ASGITransport** (проверка 401/421, маршрута, lifespan): `httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")` + `async with app.router.lifespan_context(app):` — `ASGITransport` не запускает lifespan, его надо войти вручную.
3. **SDK-клиент поверх ASGI** (list_tools/call_tool как у реального клиента): `Client(streamable_http_client(url, http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=..., headers={"Authorization": ...})))`. Для v1 вместо этого — `streamablehttp_client(url, headers=..., httpx_client_factory=...)` + `ClientSession(read, write)` + `await session.initialize()`.

**Подвох pytest:** `session_manager.run()` держит anyio task group, её нельзя входить в async-генератор-фикстуре pytest-asyncio — на teardown `RuntimeError: Attempted to exit cancel scope in a different task than it was entered in` (воспроизведено: тесты проходят, но 4 ошибки teardown). Использовать `@asynccontextmanager`-хелпер внутри теста (код ниже).
Запрос без токена через SDK-клиент даёт `ExceptionGroup` из TaskGroup — для проверки 401 использовать обычный `httpx` POST, а не `Client`.
Тесты проекта: `asyncio_mode = "auto"` (`bot/pyproject.toml`), pytest-asyncio 1.4.0 — совместимо (проверено на копии, см. ниже). Хелпер `running(...)` — в `test_mcp.py`.

## Готовый пример для нашей структуры
Проверено на копии проекта: `pytest -q tests` → 743 passed (из них 5 — `tests/test_mcp.py`), `ruff check src tests` → All checks passed, `ruff format` применён. В репозитории файлы **не созданы** — справочник только описывает; внедрять будет следующий агент (`bot-backend`).

### 1. `bot/pyproject.toml` и `Settings`
- зависимость: `"mcp>=2.3,<3"`;
- `bot/src/gymbot/config.py` (диф к текущему файлу):
```diff
@@ -65,6 +65,8 @@
     # so a single HTTPS tunnel is enough for Telegram.
     api_host: str = "127.0.0.1"  # the tunnel connects locally; no need to listen on the LAN
     api_port: int = 8000
+    # Static Bearer token for the MCP endpoint /mcp; empty = MCP is not mounted.
+    mcp_token: str = ""
     miniapp_dist: Path = ROOT / "miniapp" / "dist"
     programs_dir: Path = ROOT / "data" / "programs"
     # Local browser testing only: requests without Telegram initData act as this Telegram user.
```
- `.env.example`: `MCP_TOKEN=` (пусто — MCP выключен; сгенерировать: `python -c "import secrets; print(secrets.token_urlsafe(32))"`). Значение — только в `.env`.

### 2. `bot/src/gymbot/mcp_server.py` (новый файл)
```python
"""MCP server (Streamable HTTP, stateless, static Bearer token) mounted into the FastAPI app."""

import hmac
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute
from starlette.types import ASGIApp, Receive, Scope, Send

from gymbot.db.models import Workout

MCP_PATH = "/mcp"


class BearerGuard:
    """Pure ASGI middleware: 401 unless `Authorization: Bearer <token>` matches (only under /mcp)."""

    def __init__(self, app: ASGIApp, token: str, prefix: str = MCP_PATH) -> None:
        self.app = app
        self.token = token.encode()
        self.prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and (path == self.prefix or path.startswith(self.prefix + "/")):
            auth = dict(scope["headers"]).get(b"authorization", b"")
            scheme, _, value = auth.partition(b" ")
            if scheme.lower() != b"bearer" or not hmac.compare_digest(value.strip(), self.token):
                response = JSONResponse(
                    {"error": "unauthorized"}, 401, headers={"WWW-Authenticate": "Bearer"}
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_mcp(sessionmaker: async_sessionmaker[AsyncSession]) -> MCPServer:
    mcp = MCPServer("gymapp", instructions="Personal gym diary of the owner.")

    @mcp.tool()
    async def workout_count() -> dict[str, int]:
        """Number of workouts saved in the diary."""
        async with sessionmaker() as session:
            n = (await session.execute(select(func.count()).select_from(Workout))).scalar_one()
        return {"workouts": n}

    return mcp


def mcp_http(
    sessionmaker: async_sessionmaker[AsyncSession], public_url: str
) -> tuple[list[BaseRoute], Callable[[Any], AbstractAsyncContextManager[None]]]:
    """Routes for /mcp and the lifespan that runs the MCP session manager.

    One MCPServer per FastAPI app: StreamableHTTPSessionManager.run() may be entered only once.
    """
    mcp = build_mcp(sessionmaker)
    hosts = ["127.0.0.1:*", "localhost:*"]
    if public_url:
        hosts.append(urlsplit(public_url).netloc)  # Caddy passes the original Host through
    sub = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=[]),
    )

    @asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        # A mounted sub-app's own lifespan never runs, so the host app enters the session manager.
        async with mcp.session_manager.run():
            yield

    return list(sub.routes), lifespan
```
Инструмент `workout_count` — демонстрационный; настоящие инструменты — тонкие обёртки над `gymbot.services.*`.

### 3. `bot/src/gymbot/api/app.py` (диф к текущему файлу)
```diff
@@ -17,6 +17,7 @@
 from gymbot.db.models import FoodEntry, Reminder, User, UserFact, UserProgram, WellbeingEntry
 from gymbot.db.session import Sessionmaker
 from gymbot.llm.openrouter import OpenRouterClient
+from gymbot.mcp_server import BearerGuard, mcp_http
 from gymbot.services import facts as fx
 from gymbot.services import nutrition as nut
 from gymbot.services import plan as day_plan
@@ -142,7 +143,10 @@
     `routers` are extra routes (the Telegram webhook); they go before the Mini App mount at "/",
     which would otherwise swallow them.
     """
-    app = FastAPI(title="GymAPP API", docs_url="/api/docs", openapi_url="/api/openapi.json")
+    mcp_routes, mcp_lifespan = mcp_http(sessionmaker, settings.public_url) if settings.mcp_token else ([], None)
+    app = FastAPI(
+        title="GymAPP API", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=mcp_lifespan
+    )
     tz = ZoneInfo(settings.timezone)
     clients: list[OpenRouterClient] = [llm] if llm is not None else []
 
@@ -437,6 +441,10 @@
     for router in routers:
         app.include_router(router)
 
+    app.router.routes.extend(mcp_routes)  # before the Mini App mount at "/", which swallows every path
+    if settings.mcp_token:
+        app.add_middleware(BearerGuard, token=settings.mcp_token)
+
     if settings.miniapp_dist.is_dir():  # last: the mount at "/" catches every path
         app.mount("/", StaticFiles(directory=settings.miniapp_dist, html=True), name="miniapp")
 
```

### 4. `bot/tests/test_mcp.py` (новый файл; использует существующие `conftest.make_settings` и фикстуру `db`)
```python
from contextlib import asynccontextmanager

import httpx
import httpx2
from conftest import make_settings
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from gymbot.api.app import create_app

TOKEN = "test-mcp-token"
BASE = "http://t"  # Host "t", like the other API tests
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    },
}
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ACCEPT = {"Accept": "application/json, text/event-stream"}


@asynccontextmanager
async def running(tmp_path, db, base_url=BASE):
    """The app plus an httpx client; ASGITransport does not run the ASGI lifespan, so enter it by hand.

    Deliberately not a pytest fixture: the MCP session manager owns an anyio task group, which has to be
    entered and exited in the same task (async-generator fixtures set up and tear down in different tasks).
    """
    settings = make_settings(tmp_path, mcp_token=TOKEN, public_url="https://t")
    app = create_app(settings, db)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url=base_url) as c,
    ):
        yield app, c


async def test_401_without_or_with_wrong_token(tmp_path, db):
    async with running(tmp_path, db) as (_, c):
        assert (await c.post("/mcp", json=INIT, headers=ACCEPT)).status_code == 401
        bad = {**ACCEPT, "Authorization": "Bearer nope"}
        assert (await c.post("/mcp", json=INIT, headers=bad)).status_code == 401
        assert (await c.get("/api/health")).status_code == 200  # the rest of the app stays open
        assert (await c.post("/mcp", json=INIT, headers={**ACCEPT, **AUTH})).status_code == 200


async def test_421_for_unknown_host(tmp_path, db):
    async with running(tmp_path, db, base_url="http://evil.example") as (_, c):
        assert (await c.post("/mcp", json=INIT, headers={**ACCEPT, **AUTH})).status_code == 421


async def test_list_and_call_tool(tmp_path, db):
    async with running(tmp_path, db) as (app, _):
        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE, headers=AUTH)
        async with http, Client(streamable_http_client(f"{BASE}/mcp", http_client=http)) as client:
            assert [t.name for t in (await client.list_tools()).tools] == ["workout_count"]
            res = await client.call_tool("workout_count", {})
            assert not res.is_error
            assert res.structured_content == {"workouts": 0}


async def test_each_create_app_gets_its_own_session_manager(tmp_path, db):
    for _ in range(2):
        async with running(tmp_path, db) as (_, c):
            assert (await c.post("/mcp", json=INIT, headers={**ACCEPT, **AUTH})).status_code == 200


async def test_empty_token_mounts_nothing(tmp_path, db):
    app = create_app(make_settings(tmp_path), db)  # mcp_token="" by default
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
        assert (await c.post("/mcp", json=INIT, headers=ACCEPT)).status_code in (404, 405)
```
Низкоуровневый клиент (путь из задачи, проверен на v2): в v2 `streamable_http_client` отдаёт **кортеж из 2 элементов** `(read, write)`, а не 3 как `streamablehttp_client` в v1:
```python
async with http, streamable_http_client(f"{BASE}/mcp", http_client=http) as (read, write):
    async with ClientSession(read, write) as s:        # from mcp import ClientSession
        init = await s.initialize()                    # protocol_version '2025-11-25', server_info name='gymapp'
        tools = await s.list_tools()                   # ['workout_count']
        res = await s.call_tool("workout_count", {})   # structured_content {'workouts': 0}
```

## Подводные камни
1. **v1/v2**: любой найденный в сети пример с `FastMCP`, `stateless_http=` в конструкторе или `streamablehttp_client` — для v1. В v2 это `ImportError`/`TypeError`. Пин `mcp>=2.3,<3` (в 3.0 по документации поменяются дефолты, напр. `validate_token_resource`).
2. **Lifespan** не запускается у подприложения; без `session_manager.run()` — `RuntimeError: Task group is not initialized`.
3. **`run()` один раз на экземпляр** → `MCPServer` на каждый `create_app`, не модульная глобаль (иначе второй тест падает).
4. **Host → 421** за Caddy/в тестах, пока не задан `allowed_hosts`. Caddy пробрасывает `Host` без изменений (docs: «By default, Caddy passes through incoming headers—including `Host`—to the backend»); с Caddy v2.11.0 `Host` подменяется только при проксировании на **HTTPS**-upstream — у нас upstream `127.0.0.1:PORT` по HTTP.
5. **Путь**: клиенту указывать ровно `https://домен/mcp`. `/mcp/` → 307 (если нет `StaticFiles`) или 405 (если есть).
6. **Статика**: `mount("/")` проглатывает всё не найденное — маршруты MCP добавлять раньше. И наоборот: `Mount("/", mcp_app)` проглотит мини-апп.
7. **Синхронные инструменты** выполняются в пуле потоков — для async SQLAlchemy использовать только `async def`; сессию открывать внутри инструмента (`async with sessionmaker() as s:`), коммитить явно.
8. **Исключения в инструменте скрыты** (`Error executing tool x`); осмысленные ошибки — через `ToolError`.
9. **CORS**: браузерным клиентам нужен `CORSMiddleware` с `allow_headers` ∋ `Mcp-Method`, `Mcp-Name`, `Mcp-Protocol-Version`, `Mcp-Session-Id` и `expose_headers=["Mcp-Session-Id"]` (docs `run/asgi/`). Наши клиенты (Claude Code, Claude.ai из облака) — не браузер, CORS не нужен. Наш `BearerGuard` отвечает 401 и на `OPTIONS` (проверено curl'ом) — если когда-либо понадобится CORS, пропускать preflight.
10. **GET /mcp** в stateless режиме открывает SSE и висит; в тестах не вызывать; на Caddy такой поток авто-сбрасывается без буфера (docs: ответы `text/event-stream` flush'атся сразу), у `reverse_proxy` нет таймаута чтения по умолчанию (`read_timeout`, `write_timeout`, `response_header_timeout` — нет значений по умолчанию; `dial_timeout` 3 с).
11. **Размер тела** по умолчанию 4 МиБ (`max_request_body_size`), превышение → 413.
12. **Токен**: не логировать, не класть в URL; в `.env` как `MCP_TOKEN`; сравнивать `hmac.compare_digest`. Утёкший токен = полный доступ к дневнику (и к инструментам записи) — делать запись-инструменты узкими.
13. **uvicorn `proxy_headers=True`** (уже включён в `main.py`) не влияет на `Host`; проверка Host смотрит заголовок как есть.

## Чеклист «когда что использовать»
- Нужен один владелец, свой Claude Code → статический Bearer (вариант A) + `claude mcp add --transport http ... --header`.
- Нужен Claude.ai на Free/Pro/Max → проверить, есть ли в диалоге «Request headers» (бета); иначе OAuth (вариант B + собственный authorization server — отдельный проект) или «No sign-in» (не рекомендуется).
- Логика инструментов → `Client(build_mcp(sessionmaker), raise_exceptions=True)`; авторизация/Host/маршрут → HTTP-тест с ручным `lifespan_context`; формат ответа реального клиента → `Client(streamable_http_client(..., http_client=httpx2...))`.
- Нужны уведомления/сэмплинг/elicitation → отказаться от `stateless_http`/`json_response` (не наш случай).
- Нет токена в Settings → MCP не монтируется вообще (лучше, чем пустой токен).
- Ошибка бизнес-логики для модели → `ToolError`; баг → обычное исключение (скрыто, в логе traceback).

## Что не проверено
- Реальное подключение `claude mcp add` и Claude.ai (запуск ограничен; нет доступа к аккаунту/публичному домену). Синтаксис Claude Code и требования Claude.ai — по документации. Доступность «Request headers» на личном плане Claude.ai не подтверждена (в документации — «limited set of organizations», бета).
- Лимит времени одного вызова инструмента у Claude.ai и Cloudflare tunnel (EDGE=tunnel): в первичных источниках не найден; для коротких JSON-ответов не должно быть критично.
- Совместная работа `no_cache_index` (BaseHTTPMiddleware) и `encode gzip` из `deploy/Caddyfile` с долгоживущим SSE-потоком `GET /mcp` (для `json_response=True` + POST проблем не видно; поведение Caddy `encode` на SSE по документации не проверялось).
- v1 (1.30.0): 421 по Host не проверялся; запуском проверены только импорт, сигнатуры, mount через `routes.extend` + lifespan и один сквозной вызов (`list_tools`, `call_tool`) через `streamablehttp_client` + `ClientSession`.
- Не проверялось поведение при нескольких воркерах uvicorn (проект запускает один процесс; stateless-режим при нескольких воркерах корректен по определению, но не тестировался).
- Не проверены OAuth-потоки (вариант B дальше 401/PRM не тестировался), `event_store`/резюмирование, `ctx.*` (логирование, прогресс), resources/prompts через реальный HTTP-сокет (проверены через in-memory `Client` и через `Client` поверх ASGITransport в scratch-пробе, но не в итоговом тесте и не в проекте).
