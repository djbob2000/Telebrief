# Oracle Cloud: бюджет и защита расходов

> Снимок настроек tenancy на 9 ноября 2026 года. Бюджеты, Free Tier и ограничения OCI меняются; перед созданием ресурсов проверяйте текущие значения в Oracle Console и официальной документации. Не записывайте сюда email получателей, OCID, платёжные данные или секреты.

## Текущее состояние

Tenancy работает в режиме Pay As You Go. Это не отменяет Always Free allowances, но позволяет появиться платным расходам при превышении лимитов.

В OCI создан активный monthly budget:

| Параметр | Настройка |
| --- | --- |
| Имя | `always-free-guardrail` |
| Scope | Compartment `djbob2000 (root)` |
| Сумма | €1 в месяц (валюта, доступная в форме OCI) |
| Расписание | Monthly, обработка с первого числа месяца |
| Получатель | Email основного OCI-пользователя; сам адрес хранится только в OCI |

Настроены четыре уведомления:

| Метрика | Порог | Смысл |
| --- | ---: | --- |
| Actual Spend | 10% | Раннее предупреждение при €0,10 |
| Actual Spend | 50% | Предупреждение при €0,50 |
| Actual Spend | 100% | Фактический расход достиг €1 |
| Forecast Spend | 100% | Прогноз на месяц достиг €1 |

При первом создании в `Spent` и `Forecast` отображалось `N/A`; это не подтверждает ни отсутствие расходов, ни наличие долга. После настройки проверяйте данные в Cost Analysis и страницах usage, а не только в карточке бюджета.

## Как заново создать бюджет и уведомления

1. В Oracle Console выберите нужную tenancy и домашний регион. Откройте **Billing & Cost Management → Budgets → Create Budget**.
2. Назовите бюджет, например `always-free-guardrail`, выберите корневой compartment (`root`), расписание **Monthly** и день начала `1`.
3. Введите сумму в валюте, которую показывает форма. В проверенной консоли сумма задавалась в EUR; бюджет €1 — приблизительный аналог $1, но это не точная фиксированная сумма в USD.
4. При создании добавьте первое правило **Actual Spend / Percentage of Budget / 10%** и адрес получателя. Добавьте сообщение с указанием tenancy и порога.
5. После создания откройте вкладку **Budget Alert Rules** и добавьте **50% Actual**, **100% Actual** и **100% Forecast**. В каждом правиле отдельно укажите получателя и сообщение.
6. Проверьте каждое сохранённое правило через **Actions → View/Edit**: метрика, процент, email и сообщение. Не копируйте адрес email в Git или документацию.

Официальная инструкция: [создание Budget](https://docs.oracle.com/en-us/iaas/Content/Billing/Tasks/create-budget.htm) и [настройка Budget Alert Rule](https://docs.oracle.com/en-us/iaas/Content/Billing/Tasks/create-alert-rule.htm).

## Что бюджет делает и чего не делает

**Бюджет OCI — сигнализация, не hard limit.** Он не выключает VM, не блокирует создание платных ресурсов и не гарантирует предел суммы списания. После уведомления нужно вручную найти потребляющий ресурс и остановить/изменить его. По документации OCI бюджетные правила проверяются примерно раз в 24 часа; forecast-алгоритму нужно минимум 3 дня потребления, а сам forecast пересчитывается ежечасно. Письма поэтому не заменяют регулярную проверку Cost Analysis. См. [обзор OCI budgets](https://docs.oracle.com/en-us/iaas/Content/Billing/Concepts/budgetsoverview.htm) и [правила оповещений](https://docs.oracle.com/en-us/iaas/Content/Billing/Tasks/create-alert-rule.htm).

## Текущие ресурсы Free Tier во Frankfurt

По последней проверке в `eu-frankfurt-1`:

- `telebrief-coolify`: работающая `VM.Standard.A1.Flex`, 2 OCPU / 12 GB RAM.
- `free-tier-a1`: остановленная `VM.Standard.E2.1.Micro`, 1 OCPU / 1 GB RAM. Это имя старой VM; она не является A1.
- Boot volumes: новый диск 150 GB и диск старой VM, который Console показывает как 47 GB. Сумма отображаемых размеров — 197 GB; оба volume были помечены в Console как Always Free. Отдельных Block Volumes и Boot Volume Backups в выбранном root compartment не отображалось.
- Пользовательских Quota Policies на момент проверки не было.

Официальные страницы OCI расходятся по A1 allowance: [Always Free resource table](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) описывает 1,500 OCPU-hours и 9,000 GB-hours в месяц (2 OCPU / 12 GB постоянно), тогда как [A1 compute guide](https://docs.oracle.com/en-us/iaas/Content/Compute/References/arm.htm) приводит более высокое значение. Для защиты бюджета используйте меньшее значение, пока лимит именно вашей tenancy не подтверждён в Console. Сейчас рабочая A1 использует эти 2 OCPU / 12 GB.

200 GB общего Always Free объёма для boot + block storage практически занят: 150 + 47 = 197 GB по значениям Console. Перед созданием или увеличением диска пересчитайте все boot/block volumes, включая unattached volumes, в домашнем регионе. Не считайте остановленную VM удалённой: её boot volume продолжает существовать.

## Почему не настроена жёсткая квота OCI

В этой tenancy не создана отдельная quota policy. Quota policies задают ограничения на OCI ресурсы по compartment и часто по availability domain. Их области действия не совпадают с общим Always Free allowance по региону/месячным usage: квота на каждый AD может разрешить больший суммарный объём или, если выставить её слишком низко, блокировать штатные операции. Это не заменило бы billing limit. Перед созданием quota policy проверьте точные scopes и последствия в [синтаксисе квот](https://docs.oracle.com/en-us/iaas/Content/Quotas/Concepts/quota_policy_syntax.htm), [Compute quotas](https://docs.oracle.com/en-us/iaas/Content/Quotas/Concepts/resourcequotas_topic-Compute_Quotas.htm) и [Block Volume quotas](https://docs.oracle.com/en-us/iaas/Content/Quotas/Concepts/resourcequotas_topic-Block_Volume_Quotas.htm).

## Проверка после изменений

- **Billing & Cost Management → Budgets:** статус Active, правильные scope, сумма и schedule; существуют все четыре правила.
- **Cost Analysis:** проверяйте фактическое потребление и группируйте его по service/compartment.
- **Compute → Instances:** сверяйте формы, состояния и регионы обеих VM.
- **Block Storage → Boot Volumes / Block Volumes / Backups:** считайте detached volumes и backups, а не только диски запущенных VM.
- **Governance & Administration → Limits, Quotas and Usage / Quota Policies:** сверяйте сервисные лимиты и пользовательские политики.

Любые уведомления в бюджете могут прийти позже самого потребления. При неожиданном расходе проверяйте Cost Analysis и удаляйте/останавливайте ресурс через Console после определения его назначения; не удаляйте production volumes вслепую.
