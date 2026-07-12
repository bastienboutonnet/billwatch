FROM python:3.12-slim

# PyMuPDF/pdfplumber need a couple of system libs for some PDFs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo libopenjp2-7 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY billwatch ./billwatch

VOLUME ["/data"]
ENV DB_PATH=/data/billwatch.db PYTHONUNBUFFERED=1

CMD ["python", "-m", "billwatch.main"]
