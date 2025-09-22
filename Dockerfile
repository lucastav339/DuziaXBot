# Usa a imagem do Playwright já com deps do Chromium
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Os browsers já vêm prontos nessa imagem; se quiser garantir:
RUN playwright install chromium

COPY . .
CMD ["python", "app.py"]
