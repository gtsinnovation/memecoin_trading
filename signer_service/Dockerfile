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
# Shared with the main pipeline. Both have zero third-party imports by design,
# so they run unchanged in both images.
#   tx_verify.py      -- inbound transaction verification before signing
#   execution_rails.py -- the ABSOLUTE_MAX_POSITION_USD ceiling policy_guard
#                         reconciles MAX_TRADE_USD against. Without this file
#                         the import falls back to a mirrored copy of the
#                         constant, which would silently defeat the point of
#                         the check.
COPY tx_verify.py execution_rails.py ./

EXPOSE 8100

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8100"]
