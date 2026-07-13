-- MariaDB 10.6 – Spieldatenbank crondb
-- In phpMyAdmin: Tab "SQL" → einfügen → Ausführen

CREATE TABLE IF NOT EXISTS dwd_feed_lag (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    logged_at_berlin DATETIME NOT NULL,
    dwd_latest DATETIME NULL,
    dwd_lag_min SMALLINT UNSIGNED NULL,
    dwd_values_today SMALLINT UNSIGNED NULL,
    dwd_max DECIMAL(4,1) NULL,
    dwd_max_time DATETIME NULL,
    metar_latest DATETIME NULL,
    metar_lag_min SMALLINT UNSIGNED NULL,
    metar_values_today SMALLINT UNSIGNED NULL,
    metar_max DECIMAL(4,1) NULL,
    metar_max_time DATETIME NULL,
    dwd_tt10 DECIMAL(4,1) NULL,
    metar_temp DECIMAL(4,1) NULL,
    metar_raw_ob VARCHAR(255) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_logged_at (logged_at_berlin)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
