FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y \
    wget gnupg ca-certificates \
    libnss3 libxkbcommon0 libasound2 libgbm1 libgtk-3-0 libx11-xcb1 \
    libxcomposite1 libxdamage1 libxrandr2 libxshmfence1 \
    fonts-liberation libxss1 xdg-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
RUN python -m playwright install --with-deps chromium

COPY . .
CMD ["python", "app.py"]
