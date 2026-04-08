FROM python:3.11-slim
WORKDIR /app

# Install dependencies, setup secure keyrings, and install Google Chrome + Xvfb + Tkinter
RUN apt-get update && apt-get install -y wget gnupg xvfb python3-tk python3-dev \
    && mkdir -p /etc/apt/keyrings \
    && wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y google-chrome-stable \
    && pip install --no-cache-dir seleniumbase requests bs4

COPY edm_hunter.py terpene_hunter.py /app/