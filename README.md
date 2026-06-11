# Тусы — Telegram-бот + Mini App

MVP сервиса тусовок: афиша, билеты с QR, free-вход за приведённых друзей (с антифродом), панель организатора со сканером, автонапоминания гостям.

## Что внутри

| Файл | Что делает |
|---|---|
| `bot.py` | Бот: /start, рефералки, напоминания (за 24 ч и с адресом за 3 ч), запуск сервера |
| `api.py` | API Mini App + проверка подписи Telegram (initData) |
| `db.py` | База SQLite: ивенты, билеты, рефералы |
| `webapp/index.html` | Mini App: лента, страница тусы, билеты QR, создание, панель организатора, сканер |
| `config.py`, `.env.example` | Настройки |

## Функции

**Гость:** лента тус → страница ивента → билет: free (за подписку и/или N друзей по рефссылке) или платный (ссылка на qtickets + «Я купил», организатор подтверждает) → QR-билет → напоминание за сутки и точный адрес за 3 часа до начала.

**Организатор:** создание тусы за 2 минуты (лимит мест, возраст, цена, рефералка, канал) → гостевой список → подтверждение оплат → сканер QR на входе (повторный вход по тому же билету не пройдёт).

**Антифрод рефералки:** засчитываются только новые пользователи бота; аккаунты-новореги (ID выше порога `NEW_ID_THRESHOLD`) не считаются; сам себя пригласить нельзя; один человек засчитывается один раз; в момент выдачи билета бот перепроверяет, что приглашённые подписаны на канал.

## Запуск за 30 минут

### 1. Создай бота
В Telegram открой @BotFather → `/newbot` → имя и юзернейм → получишь токен.

### 2. Сервер
Любой VPS (Timeweb/Beget, от ~300 ₽/мес), Ubuntu 22+:

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv
git clone <твой-репозиторий> tusa_bot && cd tusa_bot   # или загрузи файлы scp
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env   # вставь BOT_TOKEN и WEBAPP_URL
```

### 3. HTTPS-домен для Mini App (обязательное требование Telegram)
Самый простой путь — Caddy (автоматический сертификат):

```bash
sudo apt install -y caddy
echo "твой-домен.ру {
    reverse_proxy localhost:8080
}" | sudo tee /etc/caddy/Caddyfile
sudo systemctl restart caddy
```
Домен (~300 ₽/год) направь A-записью на IP сервера. Нет домена — на первое время подойдёт бесплатный туннель: `cloudflared tunnel --url http://localhost:8080` (даст https-адрес, вставь его в WEBAPP_URL).

### 4. Запуск
```bash
python bot.py
```
Для постоянной работы — systemd:
```bash
sudo tee /etc/systemd/system/tusa.service <<EOF
[Unit]
Description=Tusa bot
After=network.target
[Service]
WorkingDirectory=/root/tusa_bot
ExecStart=/root/tusa_bot/venv/bin/python bot.py
Restart=always
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now tusa
```

### 5. Проверка подписки на канал
Чтобы бот проверял подписку гостей — добавь бота **администратором** канала (права может иметь минимальные).

## Антифрод: настройка порога
`NEW_ID_THRESHOLD` — ID, выше которого аккаунт считается слишком свежим. Проверь: создай тестовый новый аккаунт, посмотри его ID (бот @userinfobot) и поставь порог чуть ниже. Раз в полгода подкручивай.

## Что дальше (бэклог)
- Свои платежи (ИП + ЮKassa в Bot Payments) вместо внешних ссылок — комиссия твоя
- Платный буст ивента в ленте — первая монетизация
- Фото ивентов, фильтры по дате/району
- Webhook вместо polling при росте нагрузки, PostgreSQL вместо SQLite
- Уведомление организатору о каждом новом госте
