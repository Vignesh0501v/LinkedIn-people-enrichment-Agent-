# Frontend production image (Plan unit #22): build with Node, serve the
# static output with nginx. Deliberately not a `vite dev` container -- the
# Phase 4 decision record calls for a production build.

# --- Stage 1: build ---------------------------------------------------------
FROM node:22-alpine AS build

WORKDIR /app

# Vite inlines env vars at build time, so the API base URL has to be known
# here rather than at container start. The default matches the port
# docker-compose publishes the backend on, which is what the *browser* (not
# the frontend container) talks to -- hence localhost, not the `backend`
# service name.
ARG VITE_API_BASE_URL=http://localhost:8000
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# --- Stage 2: serve ---------------------------------------------------------
FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
