FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run sets $PORT. Single worker keeps Firestore state writes simple; the
# nightly job is short and low-concurrency.
CMD exec gunicorn --bind :${PORT:-8080} --workers 1 --threads 4 --timeout 600 app:app
