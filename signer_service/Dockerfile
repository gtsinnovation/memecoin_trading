FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY signer_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY signer_service/ .
# Shared with the main pipeline. tx_verify.py has no third-party imports by
# design, so it runs unchanged in both images.
COPY tx_verify.py .

EXPOSE 8100

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8100"]
