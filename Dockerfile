FROM python:3.12-slim

RUN useradd -m -u 501 mcpuser

WORKDIR /app
COPY . .
RUN chmod -R a+r /app/src

RUN pip install --no-cache-dir -e . azure-cli

USER mcpuser
ENTRYPOINT ["querymcpal"]
