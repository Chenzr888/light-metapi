FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8756

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./app.py
COPY static ./static

EXPOSE 8756
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:8756", "--timeout", "60", "app:app"]
