# Быстрый старт

## 1. Настройка

Скопируйте файл `.env.example` в `.env` и заполните:

```bash
cp .env.example .env
nano .env  # или используйте любой редактор
```

Заполните следующие переменные:
- `BOT_TOKEN` - токен бота от @BotFather
- `REMNAWAVE_API_URL` - URL API Remnawave (например: `https://api.example.com`)
- `REMNAWAVE_API_TOKEN` - токен авторизации для API
- `ALLOWED_MANAGER_IDS` - ID менеджеров через запятую (например: `123456789,987654321`)
- `MINI_APP_DOMAIN` - домен мини-приложения и webhook
- `USE_BUILTIN_NGINX=true|false` - использовать встроенный nginx с авто-SSL или внешний reverse proxy
- `CERTBOT_EMAIL` - email для Let's Encrypt, нужен только если `USE_BUILTIN_NGINX=true`

## 2. Запуск через Docker Compose (рекомендуется)

```bash
./compose.sh up -d --build
```

Если `USE_BUILTIN_NGINX=false`, мини-приложение будет доступно на `127.0.0.1:8080`, а пример внешнего конфига лежит в `nginx/external.conf.example`.

## 3. Запуск через Docker

```bash
docker build -t remnawave-manager-bot .
docker run -d --name remnawave-manager-bot --restart unless-stopped --env-file .env remnawave-manager-bot
```

## 4. Проверка работы

```bash
# Просмотр логов
docker logs remnawave-manager-bot
docker logs remnawave-mini-app

# Остановка
docker stop remnawave-manager-bot

# Перезапуск
docker restart remnawave-manager-bot
```

## 5. Использование

1. Найдите бота в Telegram
2. Отправьте `/start`
3. Отправьте Telegram ID (число) или username пользователя
4. Получите информацию

## Примеры запросов

- `123456789` - поиск по Telegram ID
- `@username` - поиск по username
- `username` - поиск по username (без @)
