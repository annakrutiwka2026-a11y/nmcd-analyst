FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NMCD_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt

COPY app ./app
COPY config ./config
COPY data ./data
COPY examples ./examples
COPY tools ./tools
COPY tests ./tests
COPY pytest.ini ./

RUN useradd --create-home --uid 1000 nmcd && mkdir -p /data && chown -R nmcd:nmcd /data
USER nmcd

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "app.web.main:app", "--host", "0.0.0.0", "--port", "8000"]
