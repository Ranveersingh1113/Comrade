# The SPA, built once and served as static files.
#
# Vite bakes VITE_* variables into the bundle at BUILD time, so they are build
# arguments rather than runtime environment. That is worth saying out loud
# because it is the usual way a frontend deploy goes wrong: setting
# VITE_SUPABASE_URL on the running container changes nothing at all.
FROM node:22-slim AS build
WORKDIR /app

COPY frontend/package.json frontend/package-lock.json ./
# `npm ci` is broken in this repository — package-lock.json is out of sync with
# package.json (Missing: @emnapi/core). Pre-existing, and fixing the lock is a
# separate change from shipping this image, so the install that WORKS is the
# one used here.
RUN npm install --no-audit --no-fund

COPY frontend/ ./
ARG VITE_SUPABASE_URL
ARG VITE_SUPABASE_ANON_KEY
ARG VITE_AGENT_API_URL
ENV VITE_SUPABASE_URL=$VITE_SUPABASE_URL \
    VITE_SUPABASE_ANON_KEY=$VITE_SUPABASE_ANON_KEY \
    VITE_AGENT_API_URL=$VITE_AGENT_API_URL
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY docker/frontend.nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
