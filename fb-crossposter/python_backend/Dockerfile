# FB Post Copier backend — runs on any machine with Docker (Windows/macOS/Linux).
#
# Build:  docker build -t fb-post-copier ./python_backend
# Run:    docker run -d --name fb-post-copier --restart unless-stopped \
#           -p 8765:8765 -p 5000:5000 -v fb-copier-data:/app/data fb-post-copier
#
# The Firefox extension on the same machine connects to ws://localhost:8765.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    FB_COPIER_WS_HOST=0.0.0.0 \
    FB_COPIER_WS_PORT=8765 \
    FB_COPIER_HTTP_HOST=0.0.0.0 \
    FB_COPIER_HTTP_PORT=5000 \
    FB_COPIER_AUTOSTART=true

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py notifier.py simulate_extension.py fbctl.py ./
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8765 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:5000/api/status', timeout=4); sys.exit(0)"

CMD ["python", "server.py"]
