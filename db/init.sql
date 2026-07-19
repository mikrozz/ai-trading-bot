-- TimescaleDB bootstrap (MVP)
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS md_trades (
    ts          TIMESTAMPTZ NOT NULL,
    symbol      TEXT        NOT NULL,
    trade_id    BIGINT      NOT NULL,
    price       DOUBLE PRECISION NOT NULL,
    qty         DOUBLE PRECISION NOT NULL,
    is_buyer_maker BOOLEAN NOT NULL,
    PRIMARY KEY (symbol, trade_id, ts)
);

SELECT create_hypertable('md_trades', 'ts', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS md_klines (
    ts          TIMESTAMPTZ NOT NULL,
    symbol      TEXT        NOT NULL,
    interval    TEXT        NOT NULL,
    open        DOUBLE PRECISION NOT NULL,
    high        DOUBLE PRECISION NOT NULL,
    low         DOUBLE PRECISION NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    volume      DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (symbol, interval, ts)
);

SELECT create_hypertable('md_klines', 'ts', if_not_exists => TRUE);

-- Retention: сырые трейды 30 дней (L2 позже отдельной таблицей 7–14 дней)
SELECT add_retention_policy('md_trades', INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_retention_policy('md_klines', INTERVAL '365 days', if_not_exists => TRUE);
