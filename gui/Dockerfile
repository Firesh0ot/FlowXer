FROM node:22-alpine AS build
WORKDIR /app
COPY gui/package.json gui/package-lock.json* ./
RUN npm install
COPY gui/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY gui/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 9620
