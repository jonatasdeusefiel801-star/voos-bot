FROM python:3.12-slim

WORKDIR /app

# Dependências do sistema para Playwright/Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libxss1 libasound2 libpangocairo-1.0-0 libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instala Chromium via playwright
RUN playwright install chromium

COPY . .

EXPOSE 5000

CMD ["gunicorn", "bot:app", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "1", \
     "--timeout", "120", \
     "--keep-alive", "5"]
