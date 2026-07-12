FROM node:22-slim

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# API_BASE_URL is deliberately NOT passed as a build ARG -- it's a plain
# (non NEXT_PUBLIC_) env var, read fresh at request time by the Route
# Handlers, so docker-compose can set it per-environment without a rebuild.
RUN npm run build

EXPOSE 3000

CMD ["npm", "start"]