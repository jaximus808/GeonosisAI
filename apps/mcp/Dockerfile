# TDM-81: production image for the Claude connector (apps/mcp). Build context = the repo root:
#   docker build -f apps/mcp/Dockerfile -t rdf-mcp .
#   fly deploy . --config apps/mcp/fly.toml --dockerfile apps/mcp/Dockerfile --ignorefile apps/mcp/Dockerfile.dockerignore
# apps/mcp/Dockerfile.dockerignore keeps the context to the lockfile, @rdf/env and apps/mcp (no .env,
# no node_modules, no spikes). Node 24 runs the .ts sources directly (type stripping), no build step.
FROM node:24-bookworm-slim
RUN npm install -g pnpm@11.2.2 && npm cache clean --force
WORKDIR /app
ENV pnpm_config_store_dir=/tmp/pnpm-store CI=true
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY packages/env/package.json packages/env/
COPY apps/mcp/package.json apps/mcp/
RUN pnpm install --frozen-lockfile --prod --filter @rdf/mcp... && rm -rf /tmp/pnpm-store
COPY packages/env/ packages/env/
COPY apps/mcp/src/ apps/mcp/src/
COPY apps/mcp/scripts/ apps/mcp/scripts/
# No .env in the image: config comes from Fly env + secrets only.
ENV NODE_ENV=production HOST=0.0.0.0 PORT=8080 RDF_ENV_FILE=/nonexistent
USER node
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD node -e "fetch('http://127.0.0.1:'+process.env.PORT+'/healthz').then(r=>process.exit(r.ok?0:1),()=>process.exit(1))"
CMD ["node", "apps/mcp/src/main.ts"]
