# mctl-support runtime spike, TypeScript candidate. Multi-stage, non-root.
FROM node:24-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY tsconfig.json ./
COPY src ./src
RUN npx tsc -p tsconfig.json && npm prune --omit=dev

FROM node:24-slim
ENV NODE_ENV=production PORT=8021 REPLICA=a
WORKDIR /app
COPY --from=build --chown=node:node /app/package.json ./
COPY --from=build --chown=node:node /app/node_modules ./node_modules
COPY --from=build --chown=node:node /app/dist ./dist
USER node
EXPOSE 8021
CMD ["node", "dist/server.js"]
