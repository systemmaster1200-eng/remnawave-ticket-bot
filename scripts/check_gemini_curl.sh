#!/bin/bash
# Проверка доступности Gemini API через curl.
# Ключ берётся из .env (GEMINI_API_KEY) или передаётся первым аргументом.
# Создать ключ: https://aistudio.google.com/app/apikey (формат обычно AIzaSy...)

set -e
cd "$(dirname "$0")/.."
if [ -n "$1" ]; then
  KEY="$1"
else
  source .env 2>/dev/null || true
  KEY="${GEMINI_API_KEY:-}"
fi
if [ -z "$KEY" ]; then
  echo "Usage: $0 [GEMINI_API_KEY]"
  echo "Or set GEMINI_API_KEY in .env"
  exit 1
fi

MODEL="${GEMINI_MODEL:-gemini-2.0-flash}"
URL="https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:generateContent"

echo "Testing Gemini API: $MODEL"
echo "URL: $URL"
echo " (key via x-goog-api-key header, see https://ai.google.dev/gemini-api/docs/quickstart)"
echo ""

curl -s -w "\n\nHTTP_CODE:%{http_code}\n" -X POST "$URL" \
  -H "x-goog-api-key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"contents":[{"parts":[{"text":"Say hello in one word."}]}]}'
