FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-chi-sim poppler-utils libreoffice-writer libreoffice-calc && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --uid 10001 --create-home workbench && mkdir /app/data && chown workbench:workbench /app/data
COPY app ./app
COPY core ./core
COPY workbench ./workbench
COPY policy_collector ./policy_collector
COPY config ./config
COPY samples ./samples
COPY start.py ./
ENV HOST=0.0.0.0 PORT=8765 WORKBENCH_DATA_DIR=/app/data PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=2).read()"
CMD ["python", "start.py"]
