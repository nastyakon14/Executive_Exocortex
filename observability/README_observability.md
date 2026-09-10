# Observability

## Назначение

Спецификация подсистемы сбора и отображения метрик LLM-пайплайна Exocortex.

Документ фиксирует:

- состав компонентов и границы ответственности;
- точки инструментирования в коде;
- полный каталог метрик, лейблов и формул;
- конфигурацию Prometheus и Grafana;
- статус интеграции Langfuse;
- процедуры запуска, проверки и диагностики пустых дашбордов.

## Область действия

| Процесс | Инструментирование LLM | Экспорт `/metrics` | Скрейп Prometheus |
|---|---|---|---|
| `web_app.py` | да | да (`GET /metrics`) | да, `host.docker.internal:8008` |
| `web_app_v2.py` | косвенно (те же `NoteAtomizer` / `GraphLinker` / `GraphRAG`) | нет | нет |
| `main.py` (Telegram) | да (те же классы) | нет | нет |

Метрики, записанные в процесс без HTTP-экспорта, в Grafana не попадают.

## Состав

| Компонент | Тип | Порт (хост) | Функция |
|---|---|---|---|
| `observability` (Python-пакет) | код | — | контекст, реестр метрик, callback LLM, оценка стоимости |
| `web_app.py` | код | `WEB_APP_PORT`, по умолчанию 8008 | middleware контекста, `GET /metrics`, продуктовые события |
| Prometheus | Docker `exocortex_prometheus` | 9090 | scrape, хранение time series |
| Grafana | Docker `exocortex_grafana` | 3001 | дашборды поверх Prometheus |
| Langfuse | Docker `langfuse_server` + `langfuse_postgres` | 3000 / 5432 | UI трейсов LLM; **к Python не подключён** |

Зависимости по данным:

```
web_app.py  --GET /metrics-->  Prometheus  -->  Grafana
Langfuse  (изолирован; метрики Grafana не использует и не наполняет)
```

---

## 1. Архитектура данных

### 1.1. Поток

1. HTTP-запрос попадает в `web_app.py`.
2. Middleware записывает `project` и `source` в `contextvars`.
3. Вызов LLM идёт через `observability.llm.make_chat_openai` или `invoke_chat_stream`.
4. По завершении вызывается `record_llm_call` → объекты `prometheus_client` в памяти процесса.
5. Продуктовые действия вызывают `record_app_event`.
6. Prometheus каждые 15 с делает `GET /metrics`.
7. Grafana читает Prometheus по `http://prometheus:9090` (сеть Docker).

### 1.2. Хранение

| Данные | Путь / место |
|---|---|
| Реестр метрик | RAM процесса `web_app.py`; обнуляется при рестарте процесса |
| TSDB Prometheus | `./storage/prometheus` |
| Grafana (sqlite и т.п.) | `./storage/grafana` |
| Postgres Langfuse | `./storage/langfuse/db` |

### 1.3. Разделение Prometheus / Grafana / Langfuse

| Система | Хранит | Не хранит |
|---|---|---|
| Prometheus | числа: счётчики, гистограммы | тексты промптов и ответов |
| Grafana | конфигурацию дашбордов (provisioning) | собственные метрики приложения |
| Langfuse (целевое назначение) | трейсы, промпты, спаны | — |

Текущее состояние Langfuse: сервис в `docker-compose.yml` поднят, запись трейсов из приложения **не реализована**.

---

## 2. Конфигурация приложения

### 2.1. Файлы пакета

| Файл | Назначение |
|---|---|
| `observability/context.py` | `contextvars`: `project`, `source` |
| `observability/metrics.py` | объявление метрик; `record_llm_call`; `record_app_event` |
| `observability/llm.py` | `LLMMetricsCallback`; `make_chat_openai`; `invoke_chat_stream` |
| `observability/pricing.py` | таблица USD / 1M токенов; `estimate_cost_usd` |
| `observability/__init__.py` | реэкспорт API |
| `observability/prometheus/prometheus.yml` | scrape job |
| `observability/grafana/provisioning/datasources/prometheus.yml` | datasource Grafana |
| `observability/grafana/provisioning/dashboards/dashboards.yml` | провайдер дашбордов |
| `observability/grafana/dashboards/*.json` | JSON-дашборды |

### 2.2. Контекст запроса

**Реализация:** `observability/context.py`.  
**Установка:** middleware в `web_app.py`.

| Поле | Правило | Ограничение длины |
|---|---|---|
| `source` | всегда `"web"` для HTTP | 64 |
| `project` | сегмент URL `/p/{slug}/...`; иначе `"hub"` | 64 |

Пустое значение заменяется на `"unknown"`.

Лейблы `project` и `source` добавляются ко всем LLM-метрикам через `get_obs_context()`.

**Распространение в потоки.** Обработка файла, папки и части ingest выполняется в `run_in_executor`. `contextvars` в worker-поток не копируются. Для таких вызовов типичные лейблы:

| Лейбл | Значение |
|---|---|
| `project` | `unknown` |
| `source` | `unknown` |

Метрики записываются. Привязка к slug проекта отсутствует.

**Telegram (`main.py`).** `set_obs_context` не вызывается. Экспорта `/metrics` нет.

### 2.3. Инструментирование LLM

Фабрика: `make_chat_openai(component=..., model_name=..., temperature=..., streaming=..., instrument=...)`.

| `component` | Модуль | Метод вызова | Снятие метрик |
|---|---|---|---|
| `atomizer` | `zettelkasten/atomizer.py` | `ChatOpenAI.invoke` | `LLMMetricsCallback`, `instrument=True` |
| `linker` | `zettelkasten/linker.py` | `ChatOpenAI.invoke` | `LLMMetricsCallback`, `instrument=True` |
| `graphrag` | `zettelkasten/graph_rag.py` | `llm.stream` через `invoke_chat_stream` | `instrument=False` на модели; учёт в `invoke_chat_stream` |

`instrument=False` у GraphRAG исключает двойной учёт (callback + ручной `record_llm_call`).

Параметры клиента: `LLM_API_KEY`, `LLM_BASE_URL` из окружения.

### 2.4. Алгоритм `record_llm_call`

Вход: `component`, `model`, `duration_s`, `input_tokens`, `output_tokens`, `ttft_s`, `cost_usd`, `status`.

Порядок:

1. Инкремент `exocortex_llm_requests_total` с `status` (`ok` или `error`).
2. Observation `exocortex_llm_latency_seconds` = `max(duration_s, 0)`.
3. При `input_tokens > 0` — инкремент `exocortex_llm_tokens_total{direction="input"}`.
4. При `output_tokens > 0` — инкремент `{direction="output"}`.
5. Если `cost_usd` не передан — `estimate_cost_usd(model, input_tokens, output_tokens)`.
6. При `cost_usd > 0` — инкремент `exocortex_llm_cost_usd_total`.
7. Если задан `ttft_s >= 0`:
   - observation TTFT и prefill = `ttft_s`;
   - decode = `max(duration_s - ttft_s, 0)`;
   - при `decode_s > 0` и `output_tokens > 0` — observation `output_tokens / decode_s`.

При `status="error"` токены и стоимость, как правило, не увеличиваются (usage в ответе отсутствует).

### 2.5. Измерение времени

| Показатель | Источник | Условие наличия |
|---|---|---|
| E2E latency | `perf_counter` от start до end | любой успешный или ошибочный вызов |
| TTFT | стрим: время до первого чанка; callback: `on_llm_new_token` | стрим GraphRAG; для `invoke` без streaming callback токенов обычно нет |
| Prefill | копия TTFT | то же, что TTFT |
| Decode | E2E − TTFT | задан TTFT |
| Decode tok/s | output_tokens / decode | заданы TTFT и `output_tokens` |

Следствие: панели TTFT / prefill / decode заполняются поиском (GraphRAG stream). Вызовы atomizer и linker (`invoke`) дают E2E, токены и стоимость; TTFT может отсутствовать.

### 2.6. Токены

Порядок чтения usage из ответа:

1. `response.usage_metadata`: `input_tokens`, `output_tokens`.
2. `response_metadata.token_usage` или `.usage`: `prompt_tokens` / `input_tokens`, `completion_tokens` / `output_tokens`.
3. `total_cost` или `cost` — если есть, используется как `cost_usd` без оценки по таблице.

Для стрима usage берётся с **последнего** чанка (`stream_usage=True`, если конструктор ChatOpenAI это принимает).

### 2.7. Оценка стоимости

Файл: `observability/pricing.py`.

Формула:

```
cost_usd = (input_tokens * P_in + output_tokens * P_out) / 1_000_000
```

`P_in`, `P_out` — USD за 1M токенов. Ключ таблицы = строка модели из settings.

| Модель | Input USD/1M | Output USD/1M |
|---|---|---|
| `google/gemini-2.5-flash` | 0.30 | 2.50 |
| `google/gemini-2.5-pro` | 1.25 | 10.00 |
| `openai/gpt-4o` | 2.50 | 10.00 |
| `openai/gpt-4o-mini` | 0.15 | 0.60 |
| `openai/gpt-4.1` | 2.00 | 8.00 |
| `anthropic/claude-haiku-4.5` | 1.00 | 5.00 |
| `anthropic/claude-sonnet-4.5` | 3.00 | 15.00 |
| отсутствует в таблице | 0.50 | 1.50 |

Значение — оценка для дашбордов, не счёт провайдера. При наличии `total_cost` в ответе LiteLLM оценка не используется.

### 2.8. Продуктовые события

Функция: `record_app_event(event, status)`.  
Метрика: `exocortex_app_events_total`.  
Вызовы: только `web_app.py`.

| `event` | `status` | Условие |
|---|---|---|
| `note_add` | `ok` | `save_user_note` завершился успешно |
| `note_add` | `error` | atomizer вернул строку ошибки |
| `search` | `ok` | GraphRAG `query` выполнен |

Ингест директории: одно событие `note_add` **на файл**, не на каталог.

---

## 3. Справочник метрик Prometheus

Префикс имён: `exocortex_`.  
Клиент: `prometheus_client` (default registry).  
Экспорт: `GET http://<host>:8008/metrics`.  
Тип создания series: lazy (серия появляется после первой записи с данным набором лейблов).

### 3.1. Общие лейблы LLM-метрик

| Лейбл | Источник | Пример |
|---|---|---|
| `component` | аргумент `record_llm_call`, обрезка 48 | `atomizer`, `linker`, `graphrag` |
| `model` | имя модели, обрезка 80 | `google/gemini-2.5-flash` |
| `project` | contextvar | slug, `hub`, `unknown` |
| `source` | contextvar | `web`, `unknown` |

### 3.2. Счётчики

| Имя | Extra-лейблы | Единица | Правило инкремента |
|---|---|---|---|
| `exocortex_llm_requests_total` | `status=ok\|error` | вызовы | +1 |
| `exocortex_llm_tokens_total` | `direction=input\|output` | токены | +N |
| `exocortex_llm_cost_usd_total` | — | USD | +cost |
| `exocortex_app_events_total` | `event`, `project`, `source`, `status` | события | +1 |

Свойства Counter:

- монотонный рост в пределах жизни процесса;
- после рестарта `web_app.py` — новые series с нуля;
- в Grafana использовать `increase()` или `rate()` по окну, не «сырое» значение как итог за всё время продукта.

### 3.3. Гистограммы

Prometheus экспортирует `{name}_bucket`, `{name}_sum`, `{name}_count`.

| Имя | Единица | Buckets |
|---|---|---|
| `exocortex_llm_latency_seconds` | с | 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 60 |
| `exocortex_llm_ttft_seconds` | с | 0.05, 0.1, 0.2, 0.4, 0.8, 1.5, 3, 6, 12, 24 |
| `exocortex_llm_prefill_seconds` | с | те же, что TTFT |
| `exocortex_llm_decode_seconds` | с | 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32 |
| `exocortex_llm_decode_tokens_per_second` | токен/с | 5, 10, 20, 40, 80, 120, 200, 400, 800 |

Квантиль (шаблон PromQL):

```promql
histogram_quantile(0.95, sum by (le) (rate(exocortex_llm_latency_seconds_bucket[5m])))
```

---

## 4. Метрики по этапам пайплайна

### 4.1. Добавление заметки (текст, файл, Confluence, файл из папки)

| Шаг | LLM | Метрики |
|---|---|---|
| Извлечение текста PDF/TXT / Confluence | нет | нет |
| Atomizer, 1..N чанков | да, `invoke` | `component=atomizer`: requests, latency, tokens, cost |
| Linker, до 1 `invoke` на карточку | да | `component=linker`: то же |
| Успех / ошибка atomizer | нет | `note_add` ok \| error |

Число linker-запросов на документ ≈ число корневых карточек, для которых нашлись векторные кандидаты (без кандидатов LLM не вызывается).

### 4.2. Поиск

| Шаг | LLM | Метрики |
|---|---|---|
| Embedding + Neo4j | нет | нет в этом наборе |
| GraphRAG generate (stream) | да | `component=graphrag`: requests, latency, TTFT, tokens, cost |
| Успех | нет | `search` ok |

---

## 5. Prometheus

### 5.1. Конфигурация scrape

Файл: `observability/prometheus/prometheus.yml`.

| Параметр | Значение |
|---|---|
| `scrape_interval` | 15s |
| `evaluation_interval` | 15s |
| `job_name` | `exocortex-web` |
| `metrics_path` | `/metrics` |
| `targets` | `host.docker.internal:8008` |

Контейнер: образ `prom/prometheus:v2.55.1`, имя `exocortex_prometheus`.  
`extra_hosts`: `host.docker.internal:host-gateway` (доступ с контейнера к приложению на хосте).

Команда: `--config.file=/etc/prometheus/prometheus.yml`, `--storage.tsdb.path=/prometheus`.

### 5.2. Согласование порта

Target зашит как `8008`. При `WEB_APP_PORT≠8008` необходимо изменить `prometheus.yml` и перезапустить контейнер Prometheus.

### 5.3. Критерии работоспособности scrape

| Проверка | Ожидание |
|---|---|
| `curl -s http://localhost:8008/metrics` | HTTP 200, текст exposition format |
| `curl -s http://localhost:8008/metrics \| grep exocortex_` | строки метрик после хотя бы одного LLM-вызова |
| http://localhost:9090/targets job `exocortex-web` | state **UP** |
| Prometheus Graph: `exocortex_llm_requests_total` | series либо пусто до первого вызова при UP |

State **DOWN** у target: Grafana не получает ряды `exocortex_*`.

---

## 6. Grafana

### 6.1. Сервис

| Параметр | Значение |
|---|---|
| Образ | `grafana/grafana:11.4.0` |
| Контейнер | `exocortex_grafana` |
| Порт хоста | 3001 → контейнер 3000 |
| Пользователь | `admin` / `admin` |
| Анонимный доступ | включён, роль Viewer |
| Home dashboard | `/var/lib/grafana/dashboards/llm-overview.json` |

Порт 3001 выбран, чтобы не пересекаться с Langfuse (`3000`).

### 6.2. Datasource

Файл: `observability/grafana/provisioning/datasources/prometheus.yml`.

| Поле | Значение |
|---|---|
| name | Prometheus |
| type | prometheus |
| uid | `prometheus` |
| access | proxy |
| url | `http://prometheus:9090` |
| isDefault | true |

URL резолвится **внутри Docker-сети**, не `localhost:9090` браузера.

Дашборды привязаны к `uid: prometheus`. Сторонний datasource с другим uid панели не заполняет.

### 6.3. Провайдер дашбордов

Файл: `observability/grafana/provisioning/dashboards/dashboards.yml`.

| Поле | Значение |
|---|---|
| folder | `Exocortex` |
| path в контейнере | `/var/lib/grafana/dashboards` |
| bind хоста | `./observability/grafana/dashboards` |
| `updateIntervalSeconds` | 15 |

### 6.4. Template variables (общие)

Запросы вида:

```promql
label_values(exocortex_llm_requests_total, project)
label_values(exocortex_llm_requests_total, model)
label_values(exocortex_llm_requests_total, component)
```

| Свойство | Значение |
|---|---|
| includeAll | true |
| allValue | `.*` |
| multi | true |
| refresh | on time range change |

Фильтр в панелях: `project=~"$project"` и аналогично для model/component. При выбранном All матчится любой лейбл, включая `unknown`.

### 6.5. Дашборд `Exocortex / LLM Overview`

Файл: `llm-overview.json`. UID: `exocortex-llm-overview`. Refresh: 10s. Default range: now-6h.

| ID | Тип | Заголовок | PromQL (суть) |
|---|---|---|---|
| 1 | stat | Стоимость за период, USD | `sum(increase(exocortex_llm_cost_usd_total{...}[$__range]))` |
| 2 | stat | Входные токены | `increase` tokens `direction="input"` |
| 3 | stat | Выходные токены | `increase` tokens `direction="output"` |
| 4 | stat | LLM-запросы / ошибки | `increase` requests `status="ok"` и `"error"` |
| 5 | timeseries | Стоимость, USD/мин | `sum(rate(...cost...[5m])) by (component) * 60` |
| 6 | timeseries | Токены в секунду | `sum(rate(...tokens...[5m])) by (direction)` |
| 7 | timeseries | Стоимость по моделям | `rate(cost) by (model)` |
| 8 | piechart | Токены по этапам | `increase(tokens) by (component)` |
| 9 | timeseries | Продуктовые события | `rate(exocortex_app_events_total[5m]) by (event, status)` |

### 6.6. Дашборд `Exocortex / By Model and Pipeline`

Файл: `by-model.json`. UID: `exocortex-by-model`. Default range: now-24h. Variable: `model`.

| ID | Заголовок | PromQL (суть) |
|---|---|---|
| 1 | Стоимость по моделям | `rate(cost) by (model)` |
| 2 | Стоимость по этапам | `rate(cost) by (component)` |
| 3 | Error rate по моделям | `rate(requests{status="error"}) / rate(requests)` |
| 4 | USD на 1k выходных токенов | cost / (output tokens / 1000) за окно |

### 6.7. Дашборд `Exocortex / By Project`

Файл: `by-project.json`. UID: `exocortex-by-project`. Variable: `project`.

| ID | Тип | Заголовок | Содержание |
|---|---|---|---|
| 1 | table | Стоимость и токены по проектам | instant `increase` cost, input, output, requests; merge + rename колонок |
| 2 | timeseries | Стоимость по проектам | `rate(cost) by (project)` |
| 3 | timeseries | Поиск и добавление | `rate(app_events) by (project, event)` |
| 4 | timeseries | E2E p95 по проектам | `histogram_quantile(0.95, ... latency ...)` by project |

Ингест из директории часто агрегируется в `project=unknown` (см. §2.2).

### 6.8. Дашборд `Exocortex / Latency TTFT Prefill Decode`

Файл: `latency.json`. UID: `exocortex-llm-latency`. Variables: project, model, component.

| ID | Заголовок | Метрика |
|---|---|---|
| 1 | E2E p95 | `exocortex_llm_latency_seconds` |
| 2 | TTFT p95 | `exocortex_llm_ttft_seconds` |
| 3 | Prefill p95 | `exocortex_llm_prefill_seconds` |
| 4 | Decode tok/s p50 | `exocortex_llm_decode_tokens_per_second` |
| 5 | E2E p50 / p95 / p99 | latency histogram |
| — | TTFT / decode timeseries, heatmap E2E | соответствующие `_bucket` |

Панели 2–4 и связанные timeseries пусты, если за окно не было стриминговых вызовов GraphRAG.

---

## 7. Langfuse

### 7.1. Назначение (целевое)

Трассировка LLM: тело промпта, тело ответа, вложенные спаны atomizer → linker → RAG, токены на спан.

Prometheus эту информацию не хранит.

### 7.2. Сервисы Compose

| Сервис | Образ | Порт хоста | Данные |
|---|---|---|---|
| `langfuse-db` | `postgres:15` | 5432 | `./storage/langfuse/db` |
| `langfuse` | `langfuse/langfuse:2` | 3000 | зависит от `langfuse-db` |

Переменные контейнера Langfuse: `DATABASE_URL`, `NEXTAUTH_SECRET`, `SALT`, `NEXTAUTH_URL=http://localhost:3000`, `TELEMETRY_ENABLED=false`.

### 7.3. Состояние интеграции с приложением

| Элемент | Статус |
|---|---|
| Контейнеры в `docker-compose.yml` | описаны |
| `LANGFUSE_SECRET_KEY` / `LANGFUSE_PUBLIC_KEY` в `.env.example` | объявлены |
| Чтение ключей в Python | нет |
| Langfuse SDK / LangChain CallbackHandler | нет |
| Отправка спанов | нет |

UI http://localhost:3000 доступен после `docker compose up`. Трейсы Exocortex отсутствуют до подключения SDK.

Grafana не использует Langfuse как datasource.

---

## 8. Эксплуатация

### 8.1. Порты по умолчанию

| Сервис | Порт |
|---|---|
| `web_app.py` | 8008 |
| Prometheus | 9090 |
| Grafana | 3001 |
| Langfuse | 3000 |
| Neo4j HTTP / Bolt | 7474 / 7687 |

### 8.2. Запуск стека метрик

```bash
docker compose up -d prometheus grafana
python web_app.py
```

Langfuse для Grafana не требуется:

```bash
docker compose up -d langfuse langfuse-db
```

### 8.3. Процедура проверки

1. Приложение: процесс `web_app.py`, не `web_app_v2.py`.
2. Экспорт: `curl -s http://localhost:8008/metrics` — код 200.
3. Имена метрик: `curl -s http://localhost:8008/metrics | grep '^exocortex_'`.
4. Scrape: http://localhost:9090/targets — job `exocortex-web` = UP.
5. Нагрузка: один поиск и одна текстовая заметка в веб-UI.
6. Ожидание ≥ 30 с (два интервала scrape).
7. Grafana: http://localhost:3001 → папка Exocortex → LLM Overview, range Last 15 minutes, filters All.

### 8.4. Требования к `increase()` / `rate()`

Для ненулевого `increase(metric[$__range])` в TSDB нужны **не менее двух** точек scrape по series. Один scrape после первого LLM-вызова даёт пустую панель до следующего цикла (15 с).

---

## 9. Диагностика пустых дашбордов

Проверять цепочку последовательно. Разрыв на любом шаге даёт пустые панели.

### 9.1. Неверный процесс приложения

| Симптом | Причина | Действие |
|---|---|---|
| `/metrics` 404 или нет процесса на 8008 | запущен `web_app_v2.py` | запускать `web_app.py` |
| метрики есть, target DOWN | порт ≠ 8008 | выровнять `WEB_APP_PORT` и `prometheus.yml` |

### 9.2. Scrape DOWN

| Проверка | Действие при отказе |
|---|---|
| `docker compose ps` для `prometheus`, `grafana` | `docker compose up -d prometheus grafana` |
| `curl` с хоста на `:8008/metrics` | поднять `web_app.py`, bind `0.0.0.0` |
| Target в UI Prometheus | Linux: заменить `host.docker.internal` на IP docker-bridge (`172.17.0.1`) или `network_mode: host` |
| VPN / firewall | разрешить TCP с контейнера на хост:8008 |

### 9.3. Scrape UP, series нет

| Условие | Следствие |
|---|---|
| после старта `web_app.py` не было LLM | `exocortex_llm_*` ещё не созданы |
| рестарт приложения | реестр обнулён |
| Grafana range вне периода вызовов | выбрать Last 15 minutes |

### 9.4. Series есть, панели пустые

| Условие | Действие |
|---|---|
| variable `project` = конкретный slug, данные в `unknown` | All |
| datasource uid ≠ `prometheus` | использовать provisioned datasource |
| только дашборд Latency, был только ingest | ожидаемо; смотреть LLM Overview |

### 9.5. Частично пустые панели

| Заполнено | Пусто | Интерпретация |
|---|---|---|
| cost, tokens, requests | TTFT, prefill, decode tok/s | только `invoke` atomizer/linker |
| веб-метрики | активность Telegram | бот не скрейпится |
| Grafana | Langfuse traces | Langfuse не интегрирован (§7.3) |

### 9.6. Telegram

Процесс `main.py` пишет в свой реестр `prometheus_client`. Job Prometheus на него не настроен. В Grafana вызовы бота не отображаются.

---

## 10. Compose (фрагмент observability)

Сервисы в `docker-compose.yml`: `prometheus`, `grafana`; отдельно `langfuse`, `langfuse-db`.

Volumes Grafana: provisioning (ro), dashboards (ro), `./storage/grafana`.

Переменные Grafana: `GF_SECURITY_ADMIN_USER`, `GF_SECURITY_ADMIN_PASSWORD`, `GF_AUTH_ANONYMOUS_ENABLED`, `GF_AUTH_ANONYMOUS_ORG_ROLE`, `GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH`.
