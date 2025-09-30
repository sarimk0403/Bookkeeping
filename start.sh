#!/usr/bin/env bash
set -e
# Ensure persistent dirs exist before starting
mkdir -p "$(dirname "$DB_PATH")"
mkdir -p "$UPLOAD_DIR"
# Render provides $PORT
exec gunicorn -b 0.0.0.0:$PORT app:app
chmod +x start.sh

