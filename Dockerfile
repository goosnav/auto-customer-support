FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml README.md .env.example ./
COPY support_app ./support_app
COPY prompts ./prompts
COPY examples ./examples
RUN pip install --no-cache-dir --no-deps -e . \
    && useradd --uid 10001 --create-home support \
    && mkdir -p /app/data /app/knowledge \
    && chown -R support:support /app
USER support
ENV HOST=0.0.0.0 PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1
CMD ["support-desk", "serve"]
