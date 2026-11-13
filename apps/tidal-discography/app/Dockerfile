FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY tidal_disco ./tidal_disco
RUN pip install --no-cache-dir . && useradd -u 10001 -m disco && mkdir /data && chown disco /data
USER 10001
ENTRYPOINT ["tidal-disco"]
CMD ["sync"]
