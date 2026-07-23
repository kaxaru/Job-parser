# Наблюдаемость — Grafana, Loki, Promtail, MinIO

**Конфиги:** `observability/` — всё как код, поднимается вместе со стеком, без кликов в UI.

## Поток

```
контейнеры проекта
      │  docker_sd
      ▼
   Promtail          сбор, парсинг уровня в метку level
      │
      ▼
    Loki             хранение; чанки -> MinIO (S3), retention + лимиты
      │
      ▼
   Grafana  ◄──── hh-postgres (БД airflow): статусы и длительности прогонов
```

Два источника в одном месте: логи всего стека из Loki и метрики оркестратора прямо
из метаданных Airflow.

## Файлы

- `grafana-datasources.yml` — Loki и Postgres (БД `airflow`), с фиксированными `uid`
- `grafana-dashboards.yml` — провайдер дашбордов
- `dashboards/pipeline-health.json` — дашборд «здоровье пайплайна», 6 панелей
- `loki-config.yaml` — прод-конфиг: S3 (MinIO), retention, лимиты
- `promtail.yml` — `docker_sd`, фильтр по проекту, `pipeline_stages` для разбора `level`

Фиксированные `uid` в datasources нужны, чтобы дашборд ссылался на источник по
стабильному идентификатору — иначе после пересоздания стека панели теряют привязку.

## Дашборд «здоровье пайплайна»

`http://localhost:3001/d/hh-pipeline-health` (`admin` / `DwhDemo2026!`)

Показывает статус и длительность прогонов Airflow (из БД `airflow`), логи по уровням
во времени и сырой поток логов.

## Ad-hoc логи

`http://localhost:3001/explore` -> datasource **Loki**:

```logql
{level="ERROR"}                                            # ошибки по всему стеку
{container="hh-postgres"}                                  # логи Postgres
{container="hh-mssql"}                                     # логи MS SQL
sum by (level) (count_over_time({project="hh-dwh"}[5m]))   # логи по уровням
```

Promtail собирает логи **всех** контейнеров проекта автоматически через `docker_sd` —
mssql и minio тоже, без отдельной настройки. `level` парсится под формат каждого сервиса.

## MinIO как S3 для Loki

`http://localhost:9003` (`minioadmin` / `minioadmin`), бакет `loki` — там физически
лежат чанки.

**Бакет создаёт `minio-init`**, одноразовый сервис: Loki сам бакет не создаёт и без него
молча не пишет.

## Крайние случаи

- **Чанки не появляются сразу** — flush по таймеру, idle 30 минут. Форсировать:
  `curl -XPOST localhost:3100/flush`
- **«entry too old» или смена схемы Loki** — сбросить том `loki-data`,
  см. [`deployment.md`](deployment.md)
- **Дашборд не обновился** — provisioning перечитывает файл за ~10 секунд; либо бампнуть
  `"version"` в JSON, либо `docker compose restart grafana`
- **Панели метрик пустые** — Airflow ещё не делал прогонов, либо БД `airflow` не создана
  (скрипт `postgres/init/` отрабатывает только при первом старте тома)

## Почему конфигурация в коде

Кликнутая в UI настройка живёт в томе и теряется при `docker compose down -v`.
Provisioning-файлы переживают сброс и видны в диффе — тот же принцип, что и для дашбордов
Metabase.

## Проверка

```bash
curl -s localhost:3100/ready                 # Loki готов
curl -s localhost:3001/api/health            # Grafana
docker compose logs promtail --tail 20       # сборщик видит контейнеры
```

Ожидаемо: Loki `ready`, Grafana `{"database":"ok"}`, в Explore по `{project="hh-dwh"}`
идут строки.
