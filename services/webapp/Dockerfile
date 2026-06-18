FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir flask==3.0.3 requests==2.32.3 \
    "setuptools<81" \
    prometheus-client==0.20.0 \
    opentelemetry-distro==0.48b0 opentelemetry-exporter-otlp==1.27.0 \
    opentelemetry-instrumentation-flask==0.48b0 \
    opentelemetry-instrumentation-requests==0.48b0
COPY app.py .
CMD ["sh", "-c", "if [ \"$OTEL_ENABLED\" = \"1\" ]; then exec opentelemetry-instrument python app.py; else exec python app.py; fi"]
