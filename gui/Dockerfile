FROM node:22-alpine AS build
WORKDIR /app
COPY gui/package.json gui/package-lock.json* ./
RUN npm install
COPY gui/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY gui/nginx.conf /etc/nginx/conf.d/default.conf
COPY gui/docker-entrypoint.d/40-flowxer-token.sh /docker-entrypoint.d/40-flowxer-token.sh
RUN chmod +x /docker-entrypoint.d/40-flowxer-token.sh \
    && : > /etc/nginx/conf.d/flowxer-auth.inc
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 9620
