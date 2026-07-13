-- MariaDB 10.6 – Spieldatenbank crondb
-- In phpMyAdmin: Tab "SQL" → einfügen → Ausführen
--
-- Speichert den Synoptic-5-Minuten-Feed (HFMETAR) pro Station.
-- Dedup über UNIQUE KEY (station, observed_at_utc): der Poller kann beliebig
-- oft laufen, es wird nur gespeichert, was noch nicht vorhanden ist.

CREATE TABLE IF NOT EXISTS synoptic_5min_obs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    station VARCHAR(10) NOT NULL,
    observed_at_utc DATETIME NOT NULL,
    air_temp_c DECIMAL(4,1) NULL,
    air_temp_f DECIMAL(5,1) NULL,
    is_metar TINYINT(1) NOT NULL DEFAULT 0,
    metar_raw VARCHAR(255) NULL,
    fetched_at_utc DATETIME NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_station_observed (station, observed_at_utc),
    KEY idx_station_day (station, observed_at_utc)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
