FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 agent \
    && mkdir -p /workspace /data \
    && chown -R agent:agent /workspace /data
COPY checkpoint.py /opt/checkpoint.py
COPY --chown=agent:agent download_data.sh /workspace/download_data.sh
RUN chmod 0755 /workspace/download_data.sh
WORKDIR /workspace
USER agent
CMD ["sleep", "infinity"]
