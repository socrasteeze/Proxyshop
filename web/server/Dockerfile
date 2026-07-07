FROM python:3.11-slim

WORKDIR /app

COPY web/server/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Only the web package is needed in the container — never src/ (Windows-only).
COPY web /app/web

ENV PROXYSHOP_DATA_DIR=/data
VOLUME /data
EXPOSE 8000

CMD ["uvicorn", "web.server.app:app", "--host", "0.0.0.0", "--port", "8000"]
