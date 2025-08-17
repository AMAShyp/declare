# Dockerfile
FROM python:3.11-slim

# (Optional) system libs if you use psycopg2 or similar
# RUN apt-get update && apt-get install -y build-essential libpq-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# Cloud Run provides $PORT. Streamlit must bind 0.0.0.0:$PORT
ENV PORT=8080
ENV STREAMLIT_SERVER_HEADLESS=true

# Create secrets.toml under /tmp at runtime so Streamlit can read it
# (filesystem is read-only except /tmp on Cloud Run instances)
CMD bash -lc 'export HOME=/tmp && mkdir -p "$HOME/.streamlit" && \
              printf "[neon]\ndsn=\"%s\"\n" "$NEON_DSN" > "$HOME/.streamlit/secrets.toml" && \
              exec streamlit run app.py --server.port=${PORT} --server.address=0.0.0.0'
