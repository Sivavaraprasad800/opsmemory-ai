# OpsMemory AI — frontend image.
#
# Runs the Vite dev server rather than a static build, because the dev server is what provides
# the /api proxy. The browser then sees a single origin, which is what the backend's CORS and
# security-header configuration expects. For a production deployment, build with `npm run
# build` and serve dist/ from a reverse proxy that forwards /api to the backend.

FROM node:20-alpine

WORKDIR /app

# Copy manifests first so a source change does not invalidate the dependency layer.
COPY package.json package-lock.json ./
RUN npm ci

COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src

EXPOSE 5173

# VITE_API_TARGET is read by vite.config.ts and defaults to 127.0.0.1:8000.
# docker-compose sets it to http://backend:8000.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
