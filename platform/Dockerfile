# Trusted platform image: the governed MCP gateway plus the mover and notifier workers.
FROM python:3.12-slim
ENV AWS_DEFAULT_REGION=us-east-1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gateway/ gateway/
COPY workers/ workers/
COPY launcher/ launcher/
COPY catalog.yaml /platform/catalog.yaml
COPY contracts/ /platform/contracts/
COPY policies/ /platform/policies/
RUN python -m gateway.lake /lake
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8080"]
