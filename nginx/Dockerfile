# syntax=docker/dockerfile:1
FROM nginx:alpine

# Кэш apk между сборками — повторный build не тянет пакеты заново. certbot тянет python и много deps, первый раз долго.
RUN --mount=type=cache,target=/var/cache/apk \
  apk add --no-cache certbot openssl dcron \
  && mkdir -p /var/www/certbot

COPY nginx.conf.template /etc/nginx/conf.d/nginx.conf.template
# Удаляем дефолтный конфиг nginx (default.conf), entrypoint создаст свой
RUN rm -f /etc/nginx/conf.d/default.conf

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 80 443

ENTRYPOINT ["/entrypoint.sh"]
