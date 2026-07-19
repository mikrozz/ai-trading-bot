FROM python:3.11-slim AS builder
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

FROM python:3.11-slim
WORKDIR /app
RUN useradd -m -u 10001 bot
COPY --from=builder /usr/local /usr/local
COPY configs ./configs
USER bot
ENTRYPOINT ["trading-bot"]
CMD ["version"]
