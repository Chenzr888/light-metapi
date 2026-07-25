FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

ARG VCS_REF=unknown

LABEL org.opencontainers.image.source="https://github.com/Chenzr888/light-metapi" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.title="light-metapi"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    HOST=0.0.0.0 \
    PORT=8756

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY app.py ./app.py
COPY opencode_go.py ./opencode_go.py
COPY static ./static
RUN chmod 755 /app /app/static \
    && chmod 644 /app/app.py /app/opencode_go.py \
    && find /app/static -type d -exec chmod 755 {} + \
    && find /app/static -type f -exec chmod 644 {} +

EXPOSE 8756
HEALTHCHECK --interval=30s --timeout=6s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8756/api/health', timeout=5).read()"
STOPSIGNAL SIGTERM
USER 1000:1000
CMD ["gunicorn", "-w", "1", "--worker-class", "gthread", "--threads", "4", "--no-control-socket", "-b", "0.0.0.0:8756", "--timeout", "180", "--graceful-timeout", "30", "--keep-alive", "5", "app:app"]
