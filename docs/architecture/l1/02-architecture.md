---
id: "l1-architecture"
level: "L1"
eyebrow: "Уровень 1 · Архитектурная схема"
nav: "Архитектурная схема"
---

## Архитектурная схема

Схема читается сверху вниз: люди → Telegram (источники, бот, канал) и Telegra.ph → сервер Telebrief → внешние сервисы ИИ. Номера на линиях — шаги из [карты процессов](#l2-interaction-map) (Уровень 2). Что делает каждый блок — в таблице [под схемой](#l1-components).

<figure class="l1-arch-fig">
            <div class="l1-arch-scroll">
              <svg class="l1-arch" viewBox="0 0 1120 760" role="img" aria-labelledby="l1-arch-title"><title id="l1-arch-title">Общая структурная схема Telebrief, уровень 1</title><defs>
<marker id="l1a-end" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="m" d="M0,0 L10,5 L0,10 z"></path></marker>
<marker id="l1a-start" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="m" d="M0,0 L10,5 L0,10 z"></path></marker>
<marker id="l1a-main" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path class="m main" d="M0,0 L10,5 L0,10 z"></path></marker>
</defs>
<rect class="g-box device" x="40" y="100" width="380" height="134" rx="14"></rect><text class="g-label" x="54" y="120" text-anchor="start">Источники</text>
<rect class="g-box client" x="430" y="100" width="670" height="134" rx="14"></rect><text class="g-label" x="444" y="120" text-anchor="start">Управление и публикация</text>
<rect class="g-box cloud" x="40" y="280" width="1060" height="290" rx="14"></rect><text class="g-label" x="54" y="300" text-anchor="start">Сервер Telebrief</text>
<rect class="g-box ext" x="40" y="620" width="1060" height="120" rx="14"></rect><text class="g-label" x="1086" y="640" text-anchor="end">Внешние сервисы ИИ</text>
<rect class="n actor" x="100" y="20" width="260" height="40" rx="20"></rect><text class="n-title" x="230" y="45">👥 Жители — авторы сообщений</text>
<rect class="n actor" x="446" y="20" width="174" height="40" rx="20"></rect><text class="n-title" x="533" y="45">👤 Владелец издания</text>
<rect class="n actor" x="700" y="20" width="384" height="40" rx="20"></rect><text class="n-title" x="892" y="45">📖 Читатели</text>
<rect class="n device" x="56" y="140" width="180" height="64" rx="10"></rect><text class="n-title" x="146" y="168">💬 Telegram</text><text class="n-sub" x="146" y="185">каналы и чаты города</text>
<rect class="n device" x="250" y="140" width="154" height="64" rx="10"></rect><text class="n-title" x="327" y="168">📘 Facebook</text><text class="n-sub" x="327" y="185">группы · выключен</text>
<rect class="n client" x="446" y="140" width="174" height="64" rx="10"></rect><text class="n-title" x="533" y="168">🤖 Бот издания</text><text class="n-sub" x="533" y="185">команды · уведомления</text>
<rect class="n client" x="700" y="140" width="180" height="64" rx="10"></rect><text class="n-title" x="790" y="168">📢 Telegram-канал</text><text class="n-sub" x="790" y="185">дайджест · анонс статьи</text>
<rect class="n client" x="900" y="140" width="184" height="64" rx="10"></rect><text class="n-title" x="992" y="168">📄 Telegra.ph</text><text class="n-sub" x="992" y="185">страница статьи</text>
<rect class="n gw" x="56" y="320" width="274" height="64" rx="10"></rect><text class="n-title" x="193" y="348">📥 Сбор</text><text class="n-sub" x="193" y="365">новые и изменённые сообщения</text>
<rect class="n svc" x="400" y="320" width="220" height="64" rx="10"></rect><text class="n-title" x="510" y="348">⏱️ Управление выпусками</text><text class="n-sub" x="510" y="365">расписание · готовность</text>
<rect class="n gw" x="700" y="320" width="384" height="64" rx="10"></rect><text class="n-title" x="892" y="348">📤 Доставка</text><text class="n-sub" x="892" y="365">пост в канал · страница статьи · повторы</text>
<rect class="n svc" x="56" y="430" width="274" height="64" rx="10"></rect><text class="n-title" x="193" y="458">🧠 Обработка знаний</text><text class="n-sub" x="193" y="475">фрагменты → сюжеты → фильтр</text>
<path class="n store" d="M400,434 a110,8 0 0 1 220,0 v56 a110,8 0 0 1 -220,0 z"></path><path class="n-cap store" d="M400,434 a110,8 0 0 0 220,0"></path><text class="n-title" x="510" y="462">🗄️ База данных</text><text class="n-sub" x="510" y="479">сообщения · сюжеты · выпуски</text>
<rect class="n svc" x="700" y="430" width="384" height="64" rx="10"></rect><text class="n-title" x="892" y="458">📰 Редакция</text><text class="n-sub" x="892" y="475">отбор → текст → проверка по источникам</text>
<rect class="n ext" x="56" y="656" width="180" height="64" rx="10"></rect><text class="n-title" x="146" y="684">🔢 Эмбеддинги</text><text class="n-sub" x="146" y="701">смысловые отпечатки</text>
<rect class="n ext" x="260" y="656" width="640" height="64" rx="10"></rect><text class="n-title" x="580" y="684">✨ Языковые модели</text><text class="n-sub" x="580" y="701">фильтр сюжетов · разбор · отбор · текст · правка</text>
<rect class="n ext" x="920" y="656" width="164" height="64" rx="10"></rect><text class="n-title" x="1002" y="684">🖼️ Обложки</text><text class="n-sub" x="1002" y="701">иллюстрация статьи</text>
<path class="e std" d="M146,60 L146,140" marker-end="url(#l1a-end)"></path>
<path class="e aux" d="M300,60 L300,140" marker-end="url(#l1a-end)"></path>
<path class="e std" d="M533,60 L533,140" marker-start="url(#l1a-start)" marker-end="url(#l1a-end)"></path>
<path class="e std" d="M790,140 L790,60" marker-end="url(#l1a-end)"></path>
<path class="e std" d="M992,140 L992,60" marker-end="url(#l1a-end)"></path>
<path class="e main" d="M146,204 L146,320" marker-end="url(#l1a-main)"></path>
<path class="e aux" d="M300,204 L300,320" marker-end="url(#l1a-end)"></path>
<path class="e std" d="M533,204 L533,320" marker-start="url(#l1a-start)" marker-end="url(#l1a-end)"></path>
<path class="e main" d="M790,320 L790,204" marker-end="url(#l1a-main)"></path>
<path class="e std" d="M992,320 L992,204" marker-end="url(#l1a-end)"></path>
<path class="e std" d="M400,352 L330,352" marker-end="url(#l1a-end)"></path>
<path class="e main" d="M193,384 L193,430" marker-end="url(#l1a-main)"></path>
<path class="e std" d="M510,384 L510,426" marker-end="url(#l1a-end)"></path>
<path class="e main" d="M330,462 L400,462" marker-end="url(#l1a-main)"></path>
<path class="e main" d="M620,462 L700,462" marker-end="url(#l1a-main)"></path>
<path class="e main" d="M892,430 L892,384" marker-end="url(#l1a-main)"></path>
<path class="e std" d="M146,494 L146,656" marker-end="url(#l1a-end)"></path>
<path class="e std" d="M290,494 L290,656" marker-end="url(#l1a-end)"></path>
<path class="e std" d="M760,494 L760,656" marker-end="url(#l1a-end)"></path>
<path class="e aux" d="M1002,494 L1002,656" marker-end="url(#l1a-end)"></path>
<rect class="chip " x="351.5" y="342" width="27" height="20" rx="10"></rect><text class="chip-t " x="365" y="356.5">①</text>
<rect class="chip main" x="132.5" y="252" width="27" height="20" rx="10"></rect><text class="chip-t main" x="146" y="266.5">②</text>
<rect class="chip " x="132.5" y="590" width="27" height="20" rx="10"></rect><text class="chip-t " x="146" y="604.5">③</text>
<rect class="chip " x="265.5" y="590" width="49" height="20" rx="10"></rect><text class="chip-t " x="290" y="604.5">⑤ ⑥</text>
<rect class="chip main" x="646.5" y="452" width="27" height="20" rx="10"></rect><text class="chip-t main" x="660" y="466.5">⑦</text>
<rect class="chip " x="735.5" y="590" width="49" height="20" rx="10"></rect><text class="chip-t " x="760" y="604.5">⑧ ⑨</text>
<rect class="chip main" x="776.5" y="252" width="27" height="20" rx="10"></rect><text class="chip-t main" x="790" y="266.5">⑩</text>
<rect class="chip " x="519.5" y="252" width="27" height="20" rx="10"></rect><text class="chip-t " x="533" y="266.5">⑪</text>
</svg>
            </div>
            <figcaption class="l1-arch-legend">
              <span><svg width="36" height="10" aria-hidden="true"><line x1="0" y1="5" x2="36" y2="5" stroke="var(--t-device)" stroke-width="3.5"></line></svg>Основной путь: сообщение → сюжет → выпуск</span>
              <span><svg width="36" height="10" aria-hidden="true"><line x1="0" y1="5" x2="36" y2="5" stroke="var(--mute)" stroke-width="1.5"></line></svg>Вызов или чтение</span>
              <span><svg width="36" height="10" aria-hidden="true"><line x1="0" y1="5" x2="36" y2="5" stroke="var(--mute)" stroke-width="1.5" stroke-dasharray="5 4"></line></svg>Выключено или необязательно</span>
              <span><svg width="36" height="18" aria-hidden="true"><rect x="1" y="1" width="34" height="16" rx="8" fill="var(--panel)" stroke="var(--mute)"></rect><text x="18" y="13" text-anchor="middle" font-size="10" font-weight="700" fill="var(--ink)">⑦</text></svg>Номер шага из карты процессов</span>
            </figcaption>
          </figure>

### Каналы связи

<div class="tw">

| Шаг | Кто → кому | Как | Зачем |
| --- | --- | --- | --- |
| **①** | Управление выпусками → Сбор | Внутри сервера, через очередь задач | За полчаса до выпуска запустить опрос всех источников издания |
| **②** | Telegram → Сбор | Интернет, личный аккаунт Telegram издания | Прочитать новые и отредактированные сообщения каналов и чатов |
| **③** | Обработка знаний → Эмбеддинги | Интернет, ключ провайдера | Получить смысловые отпечатки фрагментов, чтобы найти похожие |
| **⑤ ⑥** | Обработка знаний → Языковые модели | Интернет, ключ провайдера | Решить, касается ли сюжет города, и выписать факты с привязкой к источнику |
| **⑦** | База данных → Редакция | Внутри сервера | Взять замороженный снимок сюжетов для выпуска |
| **⑧ ⑨** | Редакция → Языковые модели | Интернет, ключ провайдера | Отобрать сюжеты, написать текст, исправить найденные проверкой ошибки |
| **⑩** | Доставка → Telegram-канал, Telegra.ph | Интернет, бот издания и аккаунт Telegra.ph | Опубликовать дайджест или статью |
| **⑪** | Владелец ↔ Бот ↔ Управление выпусками | Telegram, только для владельцев из списка | Запустить выпуск вручную, узнать статус, получить уведомление о сбое |
| **—** | Редакция → Обложки | Интернет, ключ провайдера | Нарисовать иллюстрацию к статье |
| **—** | Все блоки сервера → База данных | Внутри сервера | Сообщения, сюжеты, решения фильтра, выпуски и очередь задач |

</div>
