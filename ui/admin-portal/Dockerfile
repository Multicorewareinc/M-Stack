# Build context is the ui/ workspace root (this portal depends on the
# @multistack/ui workspace package):
#   docker build -f admin-portal/Dockerfile -t multistack-admin-portal:test ui/

FROM node:22-alpine AS deps
WORKDIR /workspace
COPY package.json package-lock.json ./
COPY packages/ui/package.json packages/ui/package.json
COPY admin-portal/package.json admin-portal/package.json
RUN npm install --no-audit --no-fund

FROM deps AS build
COPY packages/ui packages/ui
COPY admin-portal admin-portal
WORKDIR /workspace/admin-portal
RUN npm run build

# No credentials, no DB access, no dev dependencies in the final image —
# only the static build output served by nginx (blueprint §164, §166).
FROM nginx:1.27-alpine AS serve
COPY --from=build /workspace/admin-portal/dist /usr/share/nginx/html
COPY admin-portal/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
