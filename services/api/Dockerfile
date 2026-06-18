FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir flask==3.0.3 "psycopg2-binary==2.9.9" redis==5.0.8 requests==2.32.3 \
    "setuptools<81" \
    prometheus-client==0.20.0 \
    opentelemetry-distro==0.48b0 opentelemetry-exporter-otlp==1.27.0 \
    opentelemetry-instrumentation-flask==0.48b0 \
    opentelemetry-instrumentation-requests==0.48b0 \
    opentelemetry-instrumentation-psycopg2==0.48b0 \
    opentelemetry-instrumentation-redis==0.48b0
COPY app.py .
# Tracing is opt-in: with OTEL_ENABLED=1 the process is wrapped by opentelemetry-instrument
# (auto-spans for Flask/requests/psycopg2/redis); otherwise it runs exactly as before, so a
# missing/broken collector can never stop the service booting.
CMD ["sh", "-c", "if [ \"$OTEL_ENABLED\" = \"1\" ]; then exec opentelemetry-instrument python app.py; else exec python app.py; fi"]
