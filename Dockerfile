# Dockerfile
FROM python:3.9-slim-buster

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY nok-crd-metrics.py .

EXPOSE 8080

CMD ["python", "nok-crd-metrics.py"]