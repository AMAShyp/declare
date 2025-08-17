# Dockerfile
FROM python:3.11-slim

# If you need psycopg2/pg libs, uncomment:
# RUN apt-get update && apt-get install -y build-essential libpq-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# Streamlit must bind 0.0.0.0:$PORT on Cloud Run
ENV PORT=8080
ENV STREAMLIT_SERVER_HEADLESS=true
# Make top-level packages (e.g., `shelf_map`) importable
ENV PYTHONPATH=/app

# Create secrets in /tmp (writable) at startup, then run Streamlit
CMD bash -lc 'export HOME=/tmp && mkdir -p "$HOME/.streamlit" && \
              printf "[neon]\ndsn=\"%s\"\n" "$NEON_DSN" > "$HOME/.streamlit/secrets.toml" && \
              exec streamlit run app.py --server.port=${PORT} --server.address=0.0.0.0'
