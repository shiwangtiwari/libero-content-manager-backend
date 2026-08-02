FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

# ── System dependencies for Chromium ─────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2t64 \
    libatk1.0-0 \
    libxfixes3 \
    libxext6 \
    libx11-xcb1 \
    libcairo2 \
    libpango-1.0-0 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# ── Tell Playwright to use system Chromium ────────────────────────────────────
# PLAYWRIGHT_BROWSERS_PATH tells Playwright where to look for browsers.
# Setting it to /usr/bin makes it find the system chromium binary directly.
ENV PLAYWRIGHT_BROWSERS_PATH=/usr/bin
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium
# Skip downloading Playwright's own Chromium — we use the system one above
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

WORKDIR /app

COPY requirements.txt .

# ── Install Python deps ───────────────────────────────────────────────────────
RUN pip install --no-cache-dir -r requirements.txt

# ── Install Playwright Python package explicitly ──────────────────────────────
# This is separate from requirements.txt to guarantee it runs even if
# there's a version conflict. The --with-deps flag installs any remaining
# OS-level libraries Playwright needs that apt-get may have missed.
RUN pip install playwright==1.44.0 && playwright install --with-deps chromium

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
