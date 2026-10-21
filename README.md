# Тривога & Тренд — Київ

Українська мобільна панель на **Flask + Jinja2 + SQLite**. Дані беруться з реальної HTML-історії Київ Цифровий, початкова дата — 01.09.2026. Немає AI API чи платних джерел даних. Для розміщення з незалежним оновленням кожні 5 хвилин на PythonAnywhere потрібен платний хостинг.

**Розгортання:** [покрокова інструкція для eu.pythonanywhere.com](docs/pythonanywhere.md).

## Локальний запуск без Docker

Python 3.12 або новіший. У каталозі проєкту:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m alarmtrend init-db
python -m alarmtrend sync
python -m alarmtrend serve
```

Windows PowerShell: замість `source` використайте `.venv\Scripts\Activate.ps1` або запускайте `.venv\Scripts\python.exe` напряму. Адреса: http://127.0.0.1:5000. В окремому терміналі для фонових оновлень: `python -m alarmtrend worker`. Зупинка — Ctrl+C. Сервер `serve` призначений для локальної розробки; на хостингу працює WSGI.

SQLite за замовчуванням зберігається в `instance/alarmtrend.sqlite3`, поза `alarmtrend/static`. Ініціалізація/перезапуск базу не очищає. Не запускайте `sync` з кожного HTTP-запиту. Імпорт WSGI не створює фонових потоків чи планувальників.

## Конфігурація

Скопіюйте `.env.example` у приватний файл, замініть шляхи та секрети. Задайте `ALARMTREND_ENV_FILE` абсолютним шляхом до нього. Файл читають і CLI, і WSGI; задані змінні середовища мають пріоритет. Для ручного введення потрібні обидві змінні `ALARMTREND_SECRET_KEY` та `ALARMTREND_ADMIN_PASSWORD_HASH`. Без них публічна панель працює, адміністрування повертає 503.

```bash
python -c "import secrets; print(secrets.token_hex(32))"
python -m alarmtrend password-hash
```

У локальному конфігураційному файлі встановіть `ALARMTREND_SECURE_COOKIE=false` для HTTP. На PythonAnywhere залиште `true` та ввімкніть HTTPS. Дати користувача — Europe/Kyiv, у SQLite — ISO UTC. Ручний час допускає ISO-формат зі зміщенням; неоднозначні/неіснуючі локальні години не вгадуються.

| Змінна | Призначення |
| --- | --- |
| `ALARMTREND_ENV_FILE` | Абсолютний шлях приватного dotenv-файлу |
| `ALARMTREND_DATA_DIR` | Абсолютний шлях постійного каталогу SQLite і блокування |
| `ALARMTREND_START_DATE` | Початок аналізу, `2026-09-01` за замовчуванням |
| `ALARMTREND_SOURCE_URL` | URL історії Київ Цифровий |
| `ALARMTREND_SECRET_KEY` | Випадковий секрет підпису сесій |
| `ALARMTREND_ADMIN_PASSWORD_HASH` | Хеш пароля Werkzeug, пароль не зберігається |
| `ALARMTREND_SECURE_COOKIE` | `true` для HTTPS, `false` для локального HTTP |
| `ALARMTREND_INTERVAL` | Інтервал CLI worker у секундах, стандартно 300 |
| `ALARMTREND_TIMEOUT` | HTTP read timeout, стандартно 30 секунд; до 2 повторів |

## Команди та перевірки

```bash
python -m alarmtrend sync       # одне отримання та перерахунок
python -m alarmtrend worker     # незалежний процес з інтервалом 300 с
python -m alarmtrend evaluate   # послідовна перевірка та логістичний кандидат
python -m alarmtrend backup /absolute/private/path/backup.sqlite3
pip install -r requirements-dev.txt
python -m pytest -q
```

Мережеві збої не видаляють історію. На HTTP 304 та помилці горизонти перераховуються, а час успішної перевірки змінюється лише на успіху. CLI `sync` повертає код 1 при помилці, 0 при успіху/пропуску через блокування. Одне файлове блокування захищає синхронізацію і ручні зміни між процесами; ОС звільняє його після аварії. Не видаляйте lock-файл у працюючого процесу.

Тести перевіряють повторну синхронізацію, виправлення джерела, узгодження ручних подій, UTC/DST, північ, незавершені тривоги, мережеві помилки/304, конкурентний запуск, монотонність ймовірностей, відсутність майбутніх подій у прогнозі, нуль до кінця доби, авторизацію та CSRF.

Подробиці дослідження джерела та обмеження: [docs/source-and-model.md](docs/source-and-model.md). Фактично виконані перевірки: [docs/verification.md](docs/verification.md).
