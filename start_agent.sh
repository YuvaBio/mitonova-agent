#!/bin/bash
set -e
# Find available port inside container
PORT=8000
while netstat -ln | grep -q ":$PORT "; do
    ((PORT++))
done

exec env UNICORE_PORT=$PORT python3 web_ui.py
