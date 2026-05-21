# Mavis HR Bot

Telegram-бот для автоматизации воронки найма в Mavis Group.

## Что делает бот

- HR добавляет кандидата → бот сам ведёт его по воронке
- **Этап 1:** видео о компании + тест (7 вопросов, проходной 85%, 2 часа)
- **Мотивационный вопрос:** «Почему вам интересна позиция?» — ответ приходит HR
- **Этап 2:** видео о продуктах + материалы NotebookLM + тест (16 вопросов, 85%, 24 часа)
- **Этап 3:** бот выдаёт HR список кейсов для группового интервью
- **Финал:** HR ставит статус `offer_sent` / `rejected` — бот уведомляет кандидата

Напоминания каждый час, максимум 3 — после статус становится `no_response`.

---

## Шаг 1. Создание бота в Telegram

1. Откройте [@BotFather](https://t.me/BotFather) → команда `/newbot`
2. Придумайте имя и username (например `mavis_hr_bot`)
3. **Сохраните токен** — он выглядит как `123456:ABC-DEF...`

## Шаг 2. Узнайте свой Telegram ID

1. Откройте [@userinfobot](https://t.me/userinfobot) → `/start`
2. Запишите ваш ID (число, например `123456789`)

## Шаг 3. Деплой на Railway

### Вариант A — через GitHub (рекомендую)

1. Создайте репозиторий на GitHub, залейте туда все файлы (кроме `.env`!)
2. Зайдите на [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
3. Выберите ваш репозиторий
4. В разделе **Variables** добавьте переменные:

| Переменная | Значение |
|---|---|
| `BOT_TOKEN` | токен от BotFather |
| `HR_TELEGRAM_IDS` | ваш Telegram ID |
| `PASS_THRESHOLD` | `85` |
| `REMINDER_INTERVAL_HOURS` | `1` |
| `MAX_REMINDERS` | `3` |
| `TEST1_TIME_LIMIT_HOURS` | `2` |
| `TEST2_TIME_LIMIT_HOURS` | `24` |
| `DATABASE_URL` | `sqlite+aiosqlite:///./mavis_bot.db` |

5. **Settings → Volumes** → создайте volume и примонтируйте к `/app` — иначе БД будет теряться при перезапусках
6. Деплой запустится автоматически

### Вариант B — локально (для тестов)

```bash
git clone <ваш-репо>
cd mavis_hr_bot
python -m venv venv
source venv/bin/activate   # на Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# отредактируйте .env: вставьте BOT_TOKEN и HR_TELEGRAM_IDS
python bot.py
```

---

## Шаг 4. Первый запуск

1. Откройте вашего бота в Telegram → `/start`
2. Команда `/help` покажет все доступные команды HR
3. Команда `/myid` подтвердит, что бот вас распознаёт как рекрутера

---

## Использование

### Добавить кандидата

1. Попросите кандидата написать `/start` боту и прислать вам свой Telegram ID (через [@userinfobot](https://t.me/userinfobot))
2. У вас: `/add` → введите ID → ФИО → username → телефон
3. Бот сам напишет кандидату и поведёт его по воронке

### Команды HR

| Команда | Что делает |
|---|---|
| `/add` | добавить кандидата (через диалог) |
| `/list` | все кандидаты |
| `/active` | только активные |
| `/candidate ID` | полная карточка кандидата (тесты, мотивация, статус) |
| `/cases ID` | список кейсов для группового интервью |
| `/set_status ID статус` | изменить статус (см. ниже) |
| `/notify ID текст` | прямое сообщение кандидату |
| `/stats` | статистика по воронке |
| `/export` | выгрузить всех в CSV |
| `/myid` | мой Telegram ID |
| `/help` | этот список |

### Статусы кандидата

- `active` — в процессе
- `passed` — прошёл оба теста
- `failed` — провалил тест или вышло время
- `on_pause` — пауза
- `no_response` — не отвечает после 3 напоминаний
- `offer_sent` — отправлен оффер (кандидат получает уведомление)
- `rejected` — отказ (кандидат получает уведомление)

---

## Изменение контента

- **Тексты сообщений:** `data/texts.py`
- **Вопросы тестов:** `data/questions.py` (там же правильные ответы)
- **Видео и ссылки:** `data/videos.py`
- **Видео о компании:** замените файл `assets/MAVIS_GROUP.mp4`
- **Кейсы для этапа 3:** `data/texts.py` → константа `CASES_TEMPLATE`

После правок — закоммитьте в GitHub, Railway перезапустит автоматически.

---

## Структура проекта

```
mavis_hr_bot/
├── bot.py                 # точка входа
├── config.py              # настройки из .env
├── requirements.txt
├── Dockerfile
├── railway.toml
├── .env.example
├── handlers/
│   ├── candidate.py       # воронка кандидата
│   └── hr.py              # админ-команды
├── data/
│   ├── questions.py       # вопросы тестов
│   ├── texts.py           # все тексты
│   └── videos.py          # ссылки на материалы
├── db/
│   ├── models.py          # SQLAlchemy модели
│   └── database.py        # async-сессии
├── services/
│   └── reminders.py       # APScheduler — напоминания
└── assets/
    └── MAVIS_GROUP.mp4    # видео о компании
```
