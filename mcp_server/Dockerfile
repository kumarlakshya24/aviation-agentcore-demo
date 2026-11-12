# AgentCore Runtime runs linux/arm64 containers; build with --platform linux/arm64.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .

RUN useradd --create-home --uid 10001 app
USER app

LABEL project="halcyon-flight-ops"
ENV MCP_TRANSPORT=streamable-http
EXPOSE 8000
CMD ["python", "server.py"]
