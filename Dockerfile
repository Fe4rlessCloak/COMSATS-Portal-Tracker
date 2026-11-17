FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STATE_FILE=/app/data/state.json

WORKDIR /app

COPY sis_client.py notifiers.py tracker.py probe.py ./

RUN mkdir -p /app/data

CMD ["python3", "-u", "tracker.py"]
