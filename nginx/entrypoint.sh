#!/bin/sh
set -e

DOMAIN="${MINI_APP_DOMAIN:-}"
EMAIL="${CERTBOT_EMAIL:-}"

if [ -z "$DOMAIN" ]; then
  echo "MINI_APP_DOMAIN не задан. Пропуск получения сертификата."
  exit 1
fi

CERT_DIR="/etc/letsencrypt/live/${DOMAIN}"
WEBROOT="/var/www/certbot"
mkdir -p "$WEBROOT"

# Подставляем домен в конфиг nginx
export MINI_APP_DOMAIN="$DOMAIN"
envsubst '${MINI_APP_DOMAIN}' < /etc/nginx/conf.d/nginx.conf.template > /etc/nginx/conf.d/default.conf

# Первый запуск: получаем сертификат (standalone, порт 80)
if [ ! -f "${CERT_DIR}/fullchain.pem" ]; then
  if [ -z "$EMAIL" ]; then
    echo "Для первого получения сертификата задайте CERTBOT_EMAIL в .env"
    exit 1
  fi
  echo "Получение SSL-сертификата для ${DOMAIN}..."
  certbot certonly --standalone \
    -d "$DOMAIN" \
    --email "$EMAIL" \
    --agree-tos \
    --non-interactive \
    --preferred-challenges http
  echo "Сертификат получен."
fi

# Cron для продления (каждый день в 03:00)
echo "0 3 * * * certbot renew --webroot -w ${WEBROOT} --quiet && nginx -s reload 2>/dev/null" | crontab -

# Запуск cron в фоне и nginx
crond -b
exec nginx -g "daemon off;"
