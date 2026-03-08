FROM python:3.11-slim

WORKDIR /app

# Копируем файлы зависимостей
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код бота, модуль ИИ и платежей
COPY bot.py ai_support.py ./
COPY payments/ ./payments/
COPY entrypoint.sh /entrypoint.sh

# Пользователь для запуска (том /data при старте переводим на него через entrypoint)
RUN useradd -m -u 1000 botuser && chown -R botuser:botuser /app && chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
