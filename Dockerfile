FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run injects $PORT. One worker is plenty for a single-owner bot.
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 30 webhook:app
