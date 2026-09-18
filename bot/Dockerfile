FROM python:3.12-slim

# System tools CandyDownloader relies on:
# - ffmpeg: muxing/audio extraction/thumbnail embedding for yt-dlp
# - aria2c: fast multi-connection downloader (used as external downloader + generic fallback)
# - wget: last-resort generic downloader for plain direct links
# - git: some gallery-dl/yt-dlp extractor updates benefit from it
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    aria2 \
    wget \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data /app/tmp /app/cookies

CMD ["python", "-u", "main.py"]
