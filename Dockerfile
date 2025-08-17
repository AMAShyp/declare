FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

# Make top-level packages importable
ENV PYTHONPATH=/app
ENV PORT=8080
ENV STREAMLIT_SERVER_HEADLESS=true

# Fail fast if the module can’t be imported (build-time sanity check)
RUN python - <<'PY'
import sys; sys.path.append('/app')
import importlib
importlib.import_module('shelf_map.shelf_map_handler')
print('OK: shelf_map.shelf_map_handler importable')
PY

# Create secrets.toml under /tmp and run Streamlit
CMD bash -lc 'export HOME=/tmp && mkdir -p "$HOME/.streamlit" && \
              printf "[neon]\ndsn=\"%s\"\n" "$NEON_DSN" > "$HOME/.streamlit/secrets.toml" && \
              exec streamlit run app.py --server.port=${PORT} --server.address=0.0.0.0'
