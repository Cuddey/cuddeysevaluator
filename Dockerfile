FROM python:3.12-slim

WORKDIR /app

# Install basic tools
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates unzip \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

# Add Google Chrome repo
RUN curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-linux.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list

# Install Chrome + Chromedriver + its dependencies
RUN apt-get update && apt-get install -y \
    google-chrome-stable chromium-driver \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python packages
COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

EXPOSE 5000

CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]
