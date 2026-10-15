FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data/audio \
    && chown -R app:app /data

WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements.txt -r requirements-dev.txt

# Vosk Persian model, baked into the image (~50 MB zip). Kept above COPY . . so it stays cached.
ARG VOSK_MODEL_URL=https://alphacephei.com/vosk/models/vosk-model-small-fa-0.42.zip
RUN python -c "import io,urllib.request,zipfile; z=zipfile.ZipFile(io.BytesIO(urllib.request.urlopen('${VOSK_MODEL_URL}', timeout=300).read())); z.extractall('/opt/vosk')"     && mv /opt/vosk/* /opt/vosk/model     && chown -R app:app /opt/vosk

COPY --chown=app:app . .

USER app
