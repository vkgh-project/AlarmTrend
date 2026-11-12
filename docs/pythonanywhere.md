# Розгортання саме на eu.pythonanywhere.com

Перевірено за офіційними сторінками **21.10.2026**. У всіх прикладах замініть `YOUR_USERNAME` на логін **європейського** облікового запису. Кінцева адреса — `https://YOUR_USERNAME.eu.pythonanywhere.com`. Docker не потрібен.

## 1. Тариф і перевірка кожні 5 хвилин

**Потрібен платний тариф.** За [європейським прайсом](https://eu.pythonanywhere.com/pricing/) Developer коштує **€10/місяць**, містить один Always-on task та необмежений вихідний доступ до інтернету. Безкоштовний Beginner обмежує зовнішні сайти й не має Always-on tasks. Безкоштовне джерело даних не означає безкоштовний хостинг із цими вимогами. Перевірте актуальну ціну перед оплатою.

Рекомендований механізм — **один окремий Always-on task**: `python -m alarmtrend worker --train`. Він запускає синхронізацію відразу, потім кожні 300 секунд, незалежно від відвідувачів, опрацьовує чергу ручних перерахунків і в окремому потоці навчає моделі за потреби. PythonAnywhere перезапускає такий процес після аварії. Це не планувальник у WSGI. [Документація Always-on](https://help.pythonanywhere.com/pages/AlwaysOnTasks/) описує налаштування і журнали.

[Scheduled Tasks](https://help.pythonanywhere.com/pages/ScheduledTasks/) підтримують щогодинні або щоденні запуски, а не вираз cron `*/5`. Альтернатива для платного акаунта: 12 щогодинних задач `sync` із хвилинами 0, 5, 10, …, 55, якщо доступна квота. Використовуйте **один** з механізмів; інструкція нижче використовує Always-on.

Інтервал є цільовим: технічні роботи хостингу, вичерпання CPU-квоти, таймаути й недоступність джерела можуть його порушити. Стежте за квотою на Account та за часом перевірки в панелі. Файлове блокування не дозволяє паралельні синхронізації; повторний `sync` пропускає зайнятий запуск.

## 2. Код і virtualenv

Увійдіть на **https://eu.pythonanywhere.com/**, відкрийте Consoles → Bash. Завантажте файли через Files або клонуванням власного репозиторію в `/home/YOUR_USERNAME/alarmtrend`. В корені мають бути `requirements.txt` і каталог `alarmtrend/`.

```bash
cd /home/YOUR_USERNAME/alarmtrend
python3.12 -m venv /home/YOUR_USERNAME/.virtualenvs/alarmtrend
source /home/YOUR_USERNAME/.virtualenvs/alarmtrend/bin/activate
python -m pip install -r requirements.txt
mkdir -p /home/YOUR_USERNAME/.config
mkdir -p /home/YOUR_USERNAME/.local/share/alarmtrend
mkdir -p /home/YOUR_USERNAME/backups/alarmtrend
chmod 700 /home/YOUR_USERNAME/.local/share/alarmtrend
chmod 700 /home/YOUR_USERNAME/backups/alarmtrend
cp .env.example /home/YOUR_USERNAME/.config/alarmtrend.env
chmod 600 /home/YOUR_USERNAME/.config/alarmtrend.env
```

Виберіть доступну Python 3.12+; версія virtualenv має точно збігатися з версією вебзастосунку. Якщо 3.12 немає в поточному system image, використайте доступну 3.13+ в обох місцях. Додаткових системних бібліотек, Redis, Celery чи Node.js не потрібно.

## 3. Спільна конфігурація та перший імпорт

Відредагуйте `/home/YOUR_USERNAME/.config/alarmtrend.env` через Files. Встановіть `DATA_DIR=/home/YOUR_USERNAME/.local/share/alarmtrend` з повним префіксом `ALARMTREND_`, справжній випадковий `SECRET_KEY` та `ADMIN_PASSWORD_HASH`. Для генерації:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
python -m alarmtrend password-hash
```

Вставте значення у відповідні рядки файлу. Це хеш пароля, не сам пароль. Залиште `ALARMTREND_SECURE_COOKIE=true`, `ALARMTREND_INTERVAL=300`. Секрети не комітьте. WSGI та worker використовують той самий файл, а не змінні з випадкової інтерактивної консолі.

```bash
export ALARMTREND_ENV_FILE=/home/YOUR_USERNAME/.config/alarmtrend.env
python -m alarmtrend init-db
python -m alarmtrend sync
python -m alarmtrend train
python -m alarmtrend predict
python -m alarmtrend report --directory docs
```

`init-db` має вивести `/home/YOUR_USERNAME/.local/share/alarmtrend/alarmtrend.sqlite3`. `sync` має завершитися з `"ok": true`. Це також **перевірка доступу до джерела саме з вашого EU-акаунта**. Доступ з локальної машини перевірено; запити з вашого PythonAnywhere-акаунта тут не виконувалися. Платний тариф прибирає allowlist-обмеження хостингу, але сайт джерела може окремо блокувати запити через Cloudflare. При 403/таймауті перевірте журнал, тариф і доступність джерела; не вимикайте перевірку TLS. Не вважайте налаштування завершеним, доки імпорт не успішний.

## 4. Web → Manual configuration → WSGI

У **європейській** панелі відкрийте Web → Add a new web app → Manual configuration → Python 3.12 (або вашу версію з кроку 2). [Офіційна інструкція Flask](https://help.pythonanywhere.com/pages/Flask/) також рекомендує Manual configuration для готового проєкту.

Вкажіть:

| Поле | Значення |
| --- | --- |
| Source code | `/home/YOUR_USERNAME/alarmtrend` |
| Working directory | `/home/YOUR_USERNAME/alarmtrend` |
| Virtualenv | `/home/YOUR_USERNAME/.virtualenvs/alarmtrend` |

Відкрийте посилання **WSGI configuration file** у Web. Повністю замініть його вміст файлом `deploy/pythonanywhere_wsgi.py`, замінивши `YOUR_USERNAME` у **двох шляхах**. WSGI-об’єкт називається `application`. Не додавайте `app.run()`, не запускайте worker з WSGI, не вмикайте debug.

## 5. Статичні файли і HTTPS

Web → Static files:

| URL | Directory |
| --- | --- |
| `/static/` | `/home/YOUR_USERNAME/alarmtrend/alarmtrend/static` |

Не додавайте мапінги для `DATA_DIR`, `.config`, каталогу резервних копій чи всього проєкту. SQLite та dotenv мають залишатися приватними файлами. Увімкніть Force HTTPS у налаштуваннях Web, натисніть **Reload**.

Відкрийте `https://YOUR_USERNAME.eu.pythonanywhere.com/` та `/login`. Перевірте завантаження CSS і вхід адміністратора. HTTP-кукі адміністратора з `Secure=true` працюють лише через HTTPS.

## 6. Незалежна фонова синхронізація

Tasks → **Always-on tasks** → Command (один рядок):

```bash
cd /home/YOUR_USERNAME/alarmtrend && ALARMTREND_ENV_FILE=/home/YOUR_USERNAME/.config/alarmtrend.env /home/YOUR_USERNAME/.virtualenvs/alarmtrend/bin/python -u -m alarmtrend worker --train
```

Натисніть Create, дочекайтеся Running. Відкрийте журнал завдання. Через 5–6 хвилин має з’явитися наступний запис `Sync HTTP=200` або `Sync HTTP=304`, а `/api/status` має отримати нове `at` і `last_success`.

На 304 отримана історія повторно не записується, проте горизонти та активна тривалість перераховуються. При мережевій помилці історія зберігається; `last_success` не змінюється, панель показує проблему. Не створюйте дублів worker після Reload: вебперезапуск на нього не впливає. Для оновлення коду worker використайте Pause/Restart у Tasks.

## 7. Журнали й обслуговування

- **Web → Log files:** access log, error log, server log; використовуйте посилання з вашої EU-панелі.
- **Tasks → Always-on → Log:** HTTP-результат, кількість змін, повідомлення помилки. `-u` вимикає затримку виводу.
- SQLite `sync_runs` зберігає історію спроб; `audit` — зміни джерела, ручні зміни й узгодження; `issues` — актуальні проблеми якості.
- `/api/status` показує свіжість даних; історія перевірок зберігається у SQLite `sync_runs`. Час останньої події та успішної перевірки — окремі поля.
- Архів прогнозів незмінний і не видаляється автоматично; історію `sync_runs`, `audit` та версії моделей також збережено. Контролюйте розмір БД, `DATA_DIR/models` та дискову квоту.
- Для оновлення залежностей активуйте virtualenv, виконайте `pip install -r requirements.txt`, Reload Web та перезапустіть Always-on. Перед оновленням зробіть резервну копію.

## 8. Резервне копіювання SQLite

Команда використовує SQLite Online Backup API та `PRAGMA integrity_check`, тому її можна виконувати під час роботи worker. Просте копіювання відкритого файлу не є способом створення узгодженої копії.

```bash
cd /home/YOUR_USERNAME/alarmtrend
ALARMTREND_ENV_FILE=/home/YOUR_USERNAME/.config/alarmtrend.env /home/YOUR_USERNAME/.virtualenvs/alarmtrend/bin/python -m alarmtrend backup /home/YOUR_USERNAME/backups/alarmtrend/backup-$(date -u +%Y%m%dT%H%M%SZ).sqlite3
```

За потреби додайте цю команду як одну щоденну Scheduled Task. Зберігайте декілька поколінь, завантажуйте копії поза хостинг через приватний Files/SSH, контролюйте вільне місце. SQLite містить історію змін та адреси спроб входу за останні 15 хвилин, а приватний dotenv містить секрети — не публікуйте ці файли. Окремо збережіть приватну конфігурацію в захищеному місці.

Для відновлення: Pause Always-on; тимчасово вимкніть Web через панель; збережіть поточну БД як окрему копію; перевірте `PRAGMA integrity_check` резервної копії; замініть `alarmtrend.sqlite3` у DATA_DIR; поверніть права на файл; увімкніть/Reload Web і Always-on. Після відновлення виконайте `sync` і перевірте `/api/status`. Не підміняйте БД у працюючого процесу.

Окремо резервуйте приватний каталог `DATA_DIR/models`: SQLite backup не включає JSON-файли моделей. Кожна версія самодостатня, `current.json` і `previous.json` — атомарні покажчики з контрольною сумою. За відсутнього/пошкодженого артефакту прогнозування переходить до історичного резерву.

## 9. Контроль після розгортання

1. Імпорт успішний, SQLite у приватному постійному каталозі.
2. Головна, CSS та `/api/status` відкриваються через HTTPS.
3. Без входу `/manual` перенаправляє на `/login`.
4. Always-on працює після закриття вкладки; час перевірки змінюється приблизно кожні 5 хв.
5. Одночасний ручний `sync` під час зайнятого блокування пропускається.
6. Резервна копія створюється та проходить integrity check.
7. На смартфоні 390×844 та зі збільшеним шрифтом перевірено читабельність і вертикальне прокручування. Візуальна перевірка в цьому середовищі не виконана через відсутність доступного браузера.

## 10. Оновлення до компонентних моделей (22.10.2026)

Зупиніть старий Always-on, зробіть SQLite backup новим іменем і копію каталогу моделей, завантажте оновлений код. У тій самій Python 3.12 virtualenv та з тим самим `ALARMTREND_ENV_FILE`:

```bash
python -m pip install -r requirements-lock.txt
python -m pip check
python -m alarmtrend init-db
python -m alarmtrend sync
python -m alarmtrend train
python -m alarmtrend predict
python -m alarmtrend report --directory docs
```

`init-db` застосовує ідемпотентну міграцію схеми 2: `model_runs`, `training_jobs`, тригери незмінності `forecasts`. Події, ручні записи та раніше випущені прогнози зберігаються. Міграція також застосовується при першому відкритті бази новим кодом. Перезапустіть усі процеси, щоб стара версія коду не працювала поруч із новою; Reload Web і Restart Always-on з командою `worker --train`.

У `/calculation` мають бути модель/резерв для кожного компонента, період навчання і метрики; `/api/status` містить `model_id`, `components` та `horizons`. `train` активує модель, `evaluate` лише оцінює, `predict` випускає прогноз без звернення до джерела. Окремий `trainer` можна запускати за наявності іншого механізму прогнозування; не потрібно додавати його, якщо є `worker --train`.

Ресурси перевірено локально на CPython 3.12.14 з NumPy 2.5.4, SciPy 1.18.1; точні сумісні версії — `requirements-lock.txt`. Навчання на 50 придатних добах (близько 14 тисяч зрізів, 342 тривоги) займає кілька секунд на локальній машині; фактичні час/CPU й обсяг масивів збережені у звіті. Це **не вимірювання PythonAnywhere**, а обсяг масивів не є повною RAM процесу з бібліотеками.

За [офіційною документацією Always-on](https://help.pythonanywhere.com/pages/AlwaysOnTasks/) ця можливість потребує платного акаунта; при вичерпанні CPU-квоти фоновий процес зупиняється. [Ліміт RAM](https://help.pythonanywhere.com/pages/RAMLimit/) — 3 GB на процес. Перевірте фактичні CPU-квоту/залишок на Account і статус/журнал Tasks; вони залежать від акаунта. За нестачі квоти дані можуть застарівати — журнал і панель це позначають. Навчання не запускається кожні 5 хвилин: черга перевіряється кожні 60 секунд, звичайні зміни об'єднуються з лімітом раз на 24 години; історичні виправлення можуть вимагати негайної спроби.

Доступу до вашого облікового запису EU PythonAnywhere у цьому середовищі немає. Тариф, залишок квоти, Linux virtualenv, реальна WSGI та Always-on там **не перевірялися**; завершення локальних тестів не вважається успішним фоновим запуском на хостингу. На локальному браузері 390×844 головну панель і сторінку пояснень перевірено; попередня примітка в кроці 9 стосувалася перевірки від 21.10.2026.
