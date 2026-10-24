# TDM-3: dev image for the TS apps (pnpm workspace). Source is bind-mounted at
# /app; node_modules dirs are named volumes so the host's macOS install is
# never touched.
FROM node:22-bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g pnpm@11.2.2
COPY docker/dev-keepalive.sh /usr/local/bin/dev-keepalive
RUN chmod +x /usr/local/bin/dev-keepalive
# pnpm 11 reads pnpm_config_*; without it the store lands in the bind-mounted
# repo as .pnpm-store/.
ENV PNPM_HOME=/pnpm \
    pnpm_config_store_dir=/pnpm/store \
    npm_config_store_dir=/pnpm/store \
    WATCHPACK_POLLING=true \
    CHOKIDAR_USEPOLLING=true \
    NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
ENTRYPOINT ["dev-keepalive"]
