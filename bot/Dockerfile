# Deno is the JavaScript runtime yt-dlp uses to solve YouTube's signature and
# "n" challenges. Without one, logged-in (cookie) extraction fails outright
# on current YouTube ("The page needs to be reloaded"). Only the binary is
# copied over from the official image.
ARG DENO_VERSION=2.9.3
FROM denoland/deno:bin-${DENO_VERSION} AS deno

FROM python:3.12-slim

COPY --from=deno /deno /usr/local/bin/deno

# System tools CandyDownloader relies on:
# - ffmpeg: muxing/audio extraction/thumbnail embedding for yt-dlp
# - aria2c: fast multi-connection downloader (used as external downloader + generic fallback)
# - wget: last-resort generic downloader for plain direct links
# - git: some gallery-dl/yt-dlp extractor updates benefit from it
# - fontconfig + fonts-noto-core: burned-in subtitles are drawn by ffmpeg (libass), which needs a font that has
#   Persian/Arabic (and Latin, Cyrillic, Hebrew, Thai, Devanagari...). CJK is not in this package.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    aria2 \
    wget \
    git \
    fontconfig \
    fonts-noto-core \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data /app/tmp /app/cookies

CMD ["python", "-u", "main.py"]
