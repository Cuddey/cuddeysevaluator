FROM python:3.13-slim

WORKDIR /app

# Install system dependencies required for Chrome
RUN apt-get update && apt-get install -y \
    wget unzip curl gnupg ca-certificates apt-transport-https \
    fonts-liberation xdg-utils \
    libasound2 libatk-bridge2.0-0 libatk1.0-0 libc6 \
    libcairo2 libcups2 libdbus-1-3 libexpat1 libfontconfig1 \
    libgcc1 libgdk-pixbuf2.0-0 libglib2.0-0 libgtk-3-0 \
    libnspr4 libnss3 libpango-1.0-0 libpangocairo-1.0-0 \
    libstdc++6 libx11-6 libx11-xcb1 libxcb1 libxcomposite1 \
    libxcursor1 libxdamage1 libxext6 libxfixes3 libxi6 \
    libxrandr2 libxrender1 libxss1 libxtst6 \
 && rm -rf /var/lib/apt/lists/*

# Install Chrome
RUN wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get update && apt-get install -y ./google-chrome-stable_current_amd64.deb \
 && rm google-chrome-stable_current_amd64.deb

# Install Chromedriver
RUN CHROME_VERSION=$(google-chrome --version | awk '{print $3}') \
 && CHROME_MAJOR=$(echo $CHROME_VERSION | cut -d. -f1) \
 && wget -q "https://chromedriver.storage.googleapis.com/LATEST_RELEASE_${CHROME_MAJOR}" -O LATEST_RELEASE \
 && CHROMEDRIVER_VERSION=$(cat LATEST_RELEASE) \
 && wget -q "https://chromedriver.storage.googleapis.com/${CHROMEDRIVER_VERSION}/chromedriver_linux64.zip" \
 && unzip chromedriver_linux64.zip -d /usr/local/bin/ \
 && rm chromedriver_linux64.zip LATEST_RELEASE

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

EXPOSE 5000

CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]
