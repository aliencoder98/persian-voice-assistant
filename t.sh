#!/bin/sh
# dev helper: run a command in the app image with the working tree mounted
MSYS_NO_PATHCONV=1 docker compose run --rm -v "$(pwd -W):/app" bot "$@"
