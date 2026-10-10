FROM mcr.microsoft.com/playwright:v1.64.0-noble@sha256:06a9939e57531807f8d5fd76ce44b53165ffb7d7501d87ab10e285c20b1e971f

USER root
WORKDIR /opt/synai
COPY . .
RUN apt-get update \
    && apt-get install --no-install-recommends -y python3.12-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/synai/.venv \
    && /opt/synai/.venv/bin/pip install --no-cache-dir . \
    && cd /opt/synai/web && npm ci \
    && chown -R pwuser:pwuser /opt/synai

USER pwuser
WORKDIR /opt/synai/web
CMD ["bash", "-lc", "npm run test:e2e && npm run test:e2e:integration"]
