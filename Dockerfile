FROM debian:bookworm-slim

# Set working directory
WORKDIR /app

# Install Python and Chrome dependencies
RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-dev \
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

# Install Google Chrome
RUN wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get update && apt-get install -y ./google-chrome-stable_current_amd64.deb \
 && rm google-chrome-stable_current_amd64.deb

# Install matching Chromedriver
RUN CHROME_VERSION=$(google-chrome --version | awk '{print $3}') \
 && CHROME_MAJOR=$(echo $CHROME_VERSION | cut -d. -f1) \
 && wget -q "https://chromedriver.storage.googleapis.com/LATEST_RELEASE_${CHROME_MAJOR}" -O LATEST_RELEASE \
 && CHROMEDRIVER_VERSION=$(cat LATEST_RELEASE) \
 && wget -q "https://chromedriver.storage.googleapis.com/${CHROMEDRIVER_VERSION}/chromedriver_linux64.zip" \
 && unzip chromedriver_linux64.zip -d /usr/local/bin/ \
 && rm chromedriver_linux64.zip LATEST_RELEASE

# Copy Python requirements and install deps
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

EXPOSE 5000

# Start with Gunicorn
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]
