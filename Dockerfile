FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    GOOGLE_TOKEN_FILE=/data/google-token.json STATE_FILE=/data/sync.sqlite3
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home bridge \
    && mkdir /data && chown bridge:bridge /data
COPY pyproject.toml ./
COPY tododolist ./tododolist
RUN pip install --no-cache-dir --no-deps .
USER bridge
VOLUME ["/data"]
# Healthy only while sync passes keep finishing (stops if Google access is lost).
HEALTHCHECK --interval=60s --timeout=15s --start-period=180s --retries=3 \
    CMD ["python", "-m", "tododolist", "health"]
ENTRYPOINT ["python", "-m", "tododolist"]
CMD ["watch"]
