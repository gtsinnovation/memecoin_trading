FROM python:3.11-slim

# Prevent Python from writing pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies required for compiling certain Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Display timezone. python:3.11-slim ships no zoneinfo database, so without
# the tzdata package above BOTH the TZ variable and Python's zoneinfo fall
# back to UTC -- silently. Stored timestamps stay UTC regardless; this only
# affects what the dashboard and logs display. See app_time.py.
ENV TZ=America/New_York

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application files
COPY . .

# Run as an unprivileged user. The app never writes to its own directory
# (PYTHONDONTWRITEBYTECODE is set above), so root bought nothing but a larger
# blast radius: code execution in a root container owns everything in it.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
