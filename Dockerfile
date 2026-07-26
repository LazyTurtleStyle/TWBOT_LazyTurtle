# TWB - Tribal Wars Bot (LazyTurtle fork)
#
# One image runs both the web dashboard and the world bots. The dashboard
# spawns bots itself (Start/Stop buttons), so they must live in the same
# container - do not split them into separate services.
#
#   docker compose up -d      build + run
#   docker compose logs -f    watch the bots
FROM python:3.12-slim

# tzdata so the bot's active_hours / night scavenging follow your local clock.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Amsterdam \
    PORT=5000

WORKDIR /app

# Dependencies first so code edits don't invalidate the pip layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x docker/entrypoint.sh

EXPOSE 5000

# Fails the build early if the bot cannot import its own modules.
RUN python twb.py -i

ENTRYPOINT ["/app/docker/entrypoint.sh"]
