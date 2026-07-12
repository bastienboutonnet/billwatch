FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/bastienboutonnet/billwatch"
LABEL org.opencontainers.image.description="BillWatch — watches an inbox / Paperless for invoices and reminds you to pay them"

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

# Default entry point is the standalone iCloud-IMAP pipeline. To run the
# Paperless companion instead, override the command:
#   python -m billwatch.companion
# (the homelab Ansible role does exactly this).
CMD ["python", "-m", "billwatch.main"]
