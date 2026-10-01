FROM node:22-alpine AS build
WORKDIR /app
COPY gui/package.json gui/package-lock.json* ./
RUN npm install
COPY gui/ ./
RUN npm run build

FROM nginx:1.27-alpine
ENV FLOWXER_MIXER_URL=http://127.0.0.1:9610
ENV FLOWXER_GUI_PORT=9620
ENV NGINX_ENVSUBST_FILTER=FLOWXER_
COPY gui/nginx.main.conf /etc/nginx/nginx.conf
COPY gui/nginx.conf /etc/nginx/templates/default.conf.template
COPY gui/docker-entrypoint.d/40-flowxer-token.sh /docker-entrypoint.d/40-flowxer-token.sh
RUN chmod +x /docker-entrypoint.d/40-flowxer-token.sh \
    && : > /etc/nginx/conf.d/flowxer-auth.inc \
    && rm -f /etc/nginx/conf.d/default.conf \
    && mkdir -p /tmp/nginx-client-body /tmp/nginx-proxy /tmp/nginx-fastcgi /tmp/nginx-uwsgi /tmp/nginx-scgi /etc/nginx/templates \
    && chown -R 1000:1000 /etc/nginx /usr/share/nginx/html /var/cache/nginx /var/run /tmp
COPY --from=build /app/dist /usr/share/nginx/html
RUN chown -R 1000:1000 /usr/share/nginx/html
EXPOSE 9620
USER 1000:1000
