ARG BUILD_FROM=docker.io/library/python:3.12-slim-bookworm
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt /app/
RUN pip install -r requirements.txt \
 && python -c "from livekit.plugins import silero; silero.VAD.load()"
COPY bridge/ /app/bridge/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

LABEL io.hass.type="addon" io.hass.version="0.4.2"
CMD ["/run.sh"]
