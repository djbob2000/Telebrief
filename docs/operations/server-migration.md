# Настройка нового сервера Telebrief и перенос

> Пошаговая инструкция и снимок текущей схемы на 10 ноября 2026 года. Конфигурация OCI и Coolify меняется: перед любой операцией сверяйте её с живой консолью. Секреты, дампы БД, приватные SSH-ключи, email получателей и значения Telegram/OpenRouter в этот файл не записывайте.

Краткая инструкция по текущим релизам и rollback находится в [Production operations runbook](production.md). Настройка бюджета и проверка Free Tier описаны отдельно в [Oracle Cloud: бюджет и защита расходов](oracle-cloud-budget.md).

## Текущая схема

- Новый production host: `telebrief-coolify`, Oracle Cloud, `eu-frankfurt-1`, Ubuntu ARM64, `VM.Standard.A1.Flex`, 2 OCPU / 12 GB RAM, boot volume 150 GB.
- Старый host: `free-tier-a1` — это `VM.Standard.E2.1.Micro` (1 OCPU / 1 GB), а не A1. На момент последней проверки он остановлен; его boot volume около 47 GB оставлен для rollback. Не завершайте VM и не удаляйте диск без отдельного решения.
- Оба сервера и их данные нельзя запускать как два production источника с теми же Telegram credentials: будут конкурировать polling, workers и планировщики.
- Панель Coolify доступна на `https://githubify.duckdns.org`; прямые порты `8000`, `6001` и `6002` закрыты в OCI. SSH tunnel можно использовать для администрирования.
- В OCI открыты TCP/80 и TCP/443 для Coolify proxy, HTTPS и GitHub webhook. SSH TCP/22 ограничен административным IP `/32`.
- Источник релиза — ветка `dev`. `main` устарела для этого сервиса и не должна использоваться для деплоя.
- Бот и канал остались существующими: `@berdiansk_news_digest_bot` публикует в `@berdiansk_news`. При восстановлении конфигурации не создавайте замену этим назначениям.

## 1. Создание VM и сетевой доступ

1. В OCI Console выберите домашний регион tenancy и проверьте **Limits, Quotas and Usage**, список Boot Volumes и Block Volumes, включая отключённые диски.
2. Создайте VM с `VM.Standard.A1.Flex`, 2 OCPU, 12 GB RAM, Ubuntu LTS ARM64 и boot volume 150 GB. Продолжайте только если Console явно помечает выбранные shape, image и volume как Always Free eligible, а суммарный объём дисков укладывается в бесплатную квоту.
3. Подключите VNIC к подходящей VCN/subnet. На NSG оставьте SSH TCP/22 только с текущего административного IP (`/32`), а TCP/80 и TCP/443 разрешите для Coolify proxy и доставки GitHub webhook. Проверьте также все Security Lists subnet: более широкое правило может открыть доступ несмотря на узкий NSG. Не открывайте наружу TCP/8000, TCP/6001 и TCP/6002.
4. Узнайте текущий public IP, SSH username и нужный ключ в OCI Console. Не копируйте их из старых команд и не записывайте новый private key или IP в репозиторий.
5. Подключитесь:

   ```sh
   ssh -i "$OCI_SSH_KEY" ubuntu@"$NEW_VM_IP"
   ```

   Уже в shell VM проверьте `uname -m` (для Ubuntu ARM ожидается `aarch64`) и `df -h /`. Если фактический OCI login user отличается, используйте значение из карточки образа и документации образа. Docker проверяйте после установки Coolify.

## 2. Установка Coolify

Устанавливайте Coolify на свежую Ubuntu LTS VM официальным self-hosted installer от Coolify, запуская его с правами root/sudo:

```sh
curl -fsSL https://cdn.coollabs.io/coolify/install.sh | sudo bash
```

Перед запуском проверьте URL и используйте только официальный install guide: [Coolify self-hosted installation](https://coolify.io/docs/start-with-self-hosted). После завершения установщик сообщает адрес панели и результат установки.

Ограничьте доступ к регистрации до открытия страницы и сразу создайте первый административный аккаунт самостоятельно в браузере. Не передавайте пароль агенту и не записывайте его в файлы проекта. Официальные инструкции рекомендуют безопасно сохранить `/data/coolify/source/.env`: файл содержит ключи установки, нужные при восстановлении Coolify. Храните резервную копию только в защищённом хранилище.

Основной адрес панели — `https://githubify.duckdns.org`. Для прямого административного доступа можно использовать SSH tunnel; подставьте актуальный адрес и путь к ключу:

```sh
ssh -i "$OCI_SSH_KEY" -N \
  -L 8000:127.0.0.1:8000 \
  -L 6001:127.0.0.1:6001 \
  -L 6002:127.0.0.1:6002 \
  ubuntu@"$NEW_VM_IP"
```

Откройте `http://127.0.0.1:8000`. Пока tunnel работает, браузер может обращаться к панели, realtime updates и web terminal; OCI NSG продолжает закрывать эти порты из интернета. Список требований Coolify к firewall: [Coolify firewall](https://coolify.io/docs/core/infrastructure/servers/firewall).

После установки проверьте `docker --version`, `docker compose version` и состояние Coolify containers (`docker ps --filter name=coolify`). Не публикуйте вывод контейнерных environment или файлов установки.

## 3. Подготовка приложения и деплоя

### Архитектура деплоя

Coolify использует GitHub App с доступом только к `djbob2000/Telebrief` и webhook endpoint `https://githubify.duckdns.org`. В GitHub App оставлены только права и события, необходимые для чтения репозитория и доставки push-webhook; доступ к pull request updates, GitHub Actions runners и GitHub deployment statuses отключён.

У ресурса Telebrief настройте ветку `dev`, Docker Compose build strategy, Base Directory `/`, Docker Compose Location `/docker-compose.yml` и **Advanced → Deployment → Auto deploy → Deploy on push (webhooks)**. Coolify клонирует commit из `dev` и собирает контейнеры на VM. Не добавляйте ручной webhook или второй workflow, запускающий параллельный deploy.

Прежний workflow публикации dev-образов в GHCR удалён. Version-tagged workflow [`docker-publish.yml`](../../.github/workflows/docker-publish.yml) остаётся отдельным релизным артефактом и не участвует в production deploy.

### Compose и PostgreSQL

Текущий [`docker-compose.yml`](../../docker-compose.yml) собирает приложение из корневого `Dockerfile` через `build: .` и запускает:

- `postgres` (`pgvector/pgvector:pg18`) с постоянным volume `postgres_data`;
- одноразовый `telebrief-migrate` с `python scripts/migrate.py`;
- `telebrief-app` и три worker-сервиса: `telebrief-worker`, `telebrief-processing-worker`, `telebrief-authority-worker`;
- постоянный `telebrief_auth`, а также bind mount `config.yaml`, `sessions/`, `data/` и `logs/`.

Приложение и все workers ждут, пока PostgreSQL станет healthy и `telebrief-migrate` завершится с кодом успеха. Сохраняйте этот порядок при изменении Compose или параметров Coolify.

В текущей ветке `dev` (проверено на `67433f84`) для PostgreSQL установлены `shared_buffers=1GB`, `effective_cache_size=3GB`, `work_mem=4MB`, `maintenance_work_mem=128MB`, `autovacuum_work_mem=64MB`, `temp_buffers=4MB`, `max_connections=50`, параллельные worker-процессы выключены, `jit=off`, `autovacuum_max_workers=2`. Это конфигурация для общей VM, на которой Coolify собирает и запускает приложения; перед изменением сверяйте фактические параметры работающего PostgreSQL и свободную память. HNSW production-индекс не включён. Временный rehearsal PostgreSQL и его volume после эксперимента удалены.

## 4. Перенос конфигурации и состояния

Сначала зафиксируйте на старом host реально работающий revision, Compose/config, сервисы и все state paths. Старый каталог приложения был `/home/opc/Telebrief`; не считайте локальную копию репозитория автоматически эквивалентной работающей конфигурации.

### Секреты и конфигурация

- Не выводите `.env`, Telegram session, `telebrief_auth`, SSH private key и значения OpenRouter в терминал, логи CI, чат или Git.
- На новом host для Compose предусмотрен env-file путь `${TELEBRIEF_ENV_FILE:-.env}`. Используемый путь на host — `/etc/telebrief/telebrief.env`; права файла должны быть owner-only (`0600`).
- Среди обязательных значений приложения — `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_BOT_TOKEN` и `OPENROUTER_API_KEY`. В Coolify их следует хранить в защищённых Environment Variables resource и передавать Compose-контейнерам; здесь записаны только имена ключей, не значения.
- Переменные, необходимые Coolify для Compose interpolation и приложению, настраиваются в secret/environment fields соответствующего Coolify resource. Проверяйте обе стороны: абсолютный `TELEBRIEF_ENV_FILE` должен существовать на host, а Compose environment не должен содержать пустые или старые значения. Не меняйте секреты и не удаляйте один из источников, пока не проверили resolved Compose и фактическое окружение контейнера без печати значений.
- Сверьте хэши `config.yaml` и city profiles с работающей конфигурацией старого сервера. Если версии отличаются, сохраните активный production вариант и согласуйте конфликт; не заменяйте его случайной локальной копией.

### База данных и файлы

1. Для rehearsal делается PostgreSQL custom-format dump (`pg_dump -Fc`) из контейнера `postgres` старого Compose project. Архив передаётся на новый host по защищённому SSH потоку в файл с правами `0600`; не сохраняйте его в Git или на рабочем столе.
2. Сверьте SHA-256 исходного потока и файла на целевом VM. Проверьте читаемость через `pg_restore --list`, затем восстановите архив в отдельный временный PostgreSQL для репетиции. Сравните версию схемы и выбранные counts с источником. Не выполняйте `--clean` против единственной production-копии.
3. Файловые данные `data/` и нужные `logs/` переносите по согласованному списку. `sessions/` и volume `telebrief_auth` копируйте для финального переноса только после остановки старых процессов, которые могут писать в эти данные.
4. Перед cutover gracefully остановите на старом host приложение и три workers, оставив PostgreSQL доступным. Снимите финальный `pg_dump -Fc`, передайте и повторно проверьте checksum/`pg_restore --list`; восстановите его в production volume нового PostgreSQL. Сохраните старый database volume как rollback copy.
5. Общий порядок backup/restore см. в [Database Backup and Disaster Recovery](../database_backup.md). Не переносите физически старый PostgreSQL data volume в новый контейнер вместо логического dump/restore.

## 5. Cutover и проверка

Подготовка может идти параллельно, но production app и workers на новом сервере должны оставаться остановленными, пока старый host использует тот же Telegram bot/session.

После финального dump и restore:

1. Запустите deployment из Coolify. Убедитесь, что `telebrief-migrate` завершился успешно до старта приложения и workers, а PostgreSQL healthy.
2. Проверьте `docker compose ps`, image revision/architecture, startup logs с редактированием секретов и базовые schema/version checks.
3. Проверьте бота безопасной командой `/status`. Не запускайте production `/digest` и не публикуйте тестовый выпуск в канал только ради проверки сервера.
4. После успешной проверки старую VM оставьте **Stopped**, но не Terminated; её 47 GB boot volume сохраните до отдельного решения. Обновите runbook, если IP, пользователи, ветка, Compose или Coolify resource поменялись.

Если новая версия не прошла проверку до внешних production side effects, остановите новые app/workers и верните старую VM в работу. Если новая система уже обработала задачи или отправляла публикации, сначала сохраните её актуальную базу и продумайте синхронизацию перед rollback: возврат к старой базе может повторить уже выполненные действия в Telegram.

## 6. Обычный релиз после переезда

1. Изменения идут в `dev`; не используйте старую `main` как источник.
2. GitHub App webhook запускает deployment в Coolify для нового push. Следите за статусом и логами в Coolify.
3. Убедитесь, что Coolify собрал образ из ожидаемого commit, migration service завершился успешно, PostgreSQL healthy, а app и три worker-контейнера запущены.
4. Выполните безопасную проверку бота командой `/status`. Не запускайте реальную публикацию только ради проверки deployment.

Coolify builds выполняются на общей VM вместе с PostgreSQL и Telebrief. Если добавятся Next.js сайты, избегайте одновременных тяжёлых builds и следите за RAM/disk/CPU до изменения PostgreSQL лимитов.

## Дальнейшая справка

- [Production deployment, rollback, services and persistent state](production.md)
- [Oracle Cloud budget and Free Tier guardrail](oracle-cloud-budget.md)
- [Database backup and disaster recovery](../database_backup.md)
- [OCI Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [Coolify self-hosted installation](https://coolify.io/docs/start-with-self-hosted)
- [Coolify firewall](https://coolify.io/docs/core/infrastructure/servers/firewall)
- [Coolify GitHub App](https://coolify.io/docs/applications/sources/github/app)
- [Coolify GitHub auto deploy](https://coolify.io/docs/applications/sources/github/auto-deploy)
- [Coolify Docker Compose](https://coolify.io/docs/applications/builds/docker-compose)
