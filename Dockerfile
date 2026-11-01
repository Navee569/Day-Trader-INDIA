FROM node:24-bookworm-slim AS codex-cli
ARG CODEX_CLI_VERSION=0.164.0
RUN npm install --global --no-audit --no-fund @openai/codex@${CODEX_CLI_VERSION}

FROM python:3.11-slim-bookworm AS paper-runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/home/trader CODEX_HOME=/data/codex
RUN groupadd --gid 10001 trader && \
    useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin trader
COPY --from=codex-cli /usr/local/bin/node /usr/local/bin/node
COPY --from=codex-cli /usr/local/lib/node_modules/@openai/codex /usr/local/lib/node_modules/@openai/codex
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex
WORKDIR /app
COPY requirements-web.txt /app/requirements-web.txt
RUN pip install --no-cache-dir -r /app/requirements-web.txt
COPY . /app
RUN mkdir -p /app/trader /data/codex && \
    chown -R trader:trader /app/trader /data/codex /home/trader && \
    codex --version
USER trader
CMD ["python", "-m", "india_trader", "deploy", "database"]
