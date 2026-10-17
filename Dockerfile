# Builds and checks everything that runs without the confidential dataset or an LLM key, then serves the app
# with synthetic demo boreholes: docker build -t borehole-atlas . && docker run -p 8000:8000 borehole-atlas

FROM node:24-slim AS web
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run lint && npm test && npm run build

FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY pytest.ini ./
COPY schema/ schema/
COPY pipeline/ pipeline/
COPY api/ api/
COPY fixtures/ fixtures/
COPY tests/ tests/
RUN python -m pytest -q -p no:cacheprovider
COPY --from=web /app/web/dist web/dist
ENV DATA_DIR=/app/data DB_PATH=/app/data/demo.sqlite
RUN mkdir -p data && python -m fixtures.demo_data
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
