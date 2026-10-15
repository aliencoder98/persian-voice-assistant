# Static ffmpeg binary (~80 MB) instead of Debian's ffmpeg package (~450 MB with its X11/codec libraries)
FROM mwader/static-ffmpeg:7.1.1 AS ffmpeg

FROM python:3.12.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg converts Telegram OGG/Opus to WAV for Vosk
COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data/audio \
    && chown -R app:app /data

WORKDIR /app

# INSTALL_DEV=0 leaves pytest out of the production image
ARG INSTALL_DEV=1
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements.txt \
    && if [ "$INSTALL_DEV" = "1" ]; then pip install -r requirements-dev.txt; fi

# Vosk Persian model (~50 MB), baked into the image. If the server cannot reach alphacephei.com,
# put the zip in ./models/ (see models/README.md) and it is used instead of downloading.
ARG VOSK_MODEL_URL=https://alphacephei.com/vosk/models/vosk-model-small-fa-0.42.zip
COPY models/ /tmp/models/
RUN python -c "import glob,io,os,urllib.request,zipfile; \
local=glob.glob('/tmp/models/vosk-model-*.zip'); \
data=open(local[0],'rb').read() if local else urllib.request.urlopen('${VOSK_MODEL_URL}', timeout=300).read(); \
zipfile.ZipFile(io.BytesIO(data)).extractall('/opt/vosk')" \
    && mv /opt/vosk/* /opt/vosk/model \
    && chown -R app:app /opt/vosk \
    && rm -rf /tmp/models

COPY --chown=app:app . .

USER app
