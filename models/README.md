# Optional offline Vosk model

The image downloads `vosk-model-small-fa-0.42.zip` from alphacephei.com while building. If the build
machine cannot reach that site, download the zip elsewhere and place it here, unchanged:

    models/vosk-model-small-fa-0.42.zip

`docker compose build` then uses it instead of downloading. Zip files here are git-ignored.
