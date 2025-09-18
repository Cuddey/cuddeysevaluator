FROM debian:bookworm-slim

WORKDIR /app

# Install system dependencies (no python3-pip this time)
RUN apt-get update && apt-get install -y \
    python3 python3-dev python3-venv \
    wget curl gnupg ca-certificates unzip \
    fonts-liberation xdg-utils \
    libasound2 libatk-bridge2.0-0 libatk1.0-0 libc6 \
    libcairo2 libcups2 libdbus-1-3 libexpat1 libfontconfig1 \
    libgcc1 libgdk-pixbuf2.0-0 libglib2.0-0 libgtk-3-0 \
    libnspr4 libnss3 libpango-1.0-0 libpangocairo-1.0-0 \
    libstdc++6 libx11-6 libx11-xcb1 libxcb1 libxcomposite1 \
    libxcursor1 libxdamage1 libxext6 libxfixes3 libxi6 \
    libxrandr2 libxrender1 libxss1 libxtst6 \
 && rm -rf /var/lib/apt/lists/*

# Install pip manually (avoids externally-managed-environment issue)
RUN curl -sS https://bootstrap.pypa.io/get-pip.py | python3

# Add Google’s apt repo and install Chrome + Chromedriver
RUN curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-linux.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list \
 && apt-get update \
 && apt-get install -y google-chrome-stable chromium-driver \
 && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python packages
COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r requirements.txt

# Copy the rest of the app
COPY . .

EXPOSE 5000

# Start Flask with Gunicorn
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]
