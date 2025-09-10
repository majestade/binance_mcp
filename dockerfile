FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN python -m pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt
COPY binance_mcp_server.py .
ENV HOST=0.0.0.0 PORT=8080
EXPOSE 8080
CMD ["uvicorn", "binance_mcp_server:app", "--host", "0.0.0.0", "--port", "8080"]