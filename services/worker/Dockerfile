FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "psycopg2-binary==2.9.9" redis==5.0.8 requests==2.32.3 \
    "setuptools<81" \
    prometheus-client==0.20.0 \
    opentelemetry-distro==0.48b0 opentelemetry-exporter-otlp==1.27.0 \
    opentelemetry-instrumentation-requests==0.48b0 \
    opentelemetry-instrumentation-psycopg2==0.48b0 \
    opentelemetry-instrumentation-redis==0.48b0
COPY worker.py .
CMD ["sh", "-c", "if [ \"$OTEL_ENABLED\" = \"1\" ]; then exec opentelemetry-instrument python worker.py; else exec python worker.py; fi"]
