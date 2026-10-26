FROM node:22-alpine AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
# The site's base is /highbar/, as on GitHub Pages.
COPY --from=build /app/.vitepress/dist /usr/share/nginx/html/highbar
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
