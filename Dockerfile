FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

# ── System dependencies for Chromium ─────────────────────────────────────────
# All real Chromium runtime dependencies installed here via apt.
# playwright install --with-deps is NOT used because it tries to install
# Ubuntu-only font packages (ttf-unifont, ttf-ubuntu-font-family) that don't
# exist on Debian Trixie and cause the build to fail with exit code 100.
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
ENV PLAYWRIGHT_BROWSERS_PATH=/usr/bin
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# ── Install Playwright Python package and register browser ────────────────────
# Use 'playwright install chromium' WITHOUT --with-deps.
# The --with-deps flag pulls Ubuntu font packages that don't exist on Debian.
# All actual Chromium system dependencies are already installed above via apt.
RUN pip install playwright==1.44.0 && playwright install chromium

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
