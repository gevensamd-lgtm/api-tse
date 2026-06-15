FROM python:3.12-slim

# Playwright Chromium system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
    libcairo2 libxcursor1 libxi6 libxtst6 libx11-xcb1 \
    fonts-liberation ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium + system deps via playwright
RUN python -m playwright install --with-deps chromium

COPY app.py tse_scraper.py ./

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
