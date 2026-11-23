FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STATE_FILE=/app/data/state.json \
    HEARTBEAT_FILE=/app/data/heartbeat.json

WORKDIR /app

COPY config.py sis_client.py notifiers.py runtime_store.py snapshot_transition.py tracker.py probe.py session_ui.py ./

EXPOSE 8765

RUN mkdir -p /app/data

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python3", "tracker.py", "--healthcheck"]

CMD ["python3", "-u", "tracker.py"]
