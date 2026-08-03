-- MariaDB 10.6 – Spieldatenbank crondb
-- In phpMyAdmin: Tab "SQL" → einfügen → Ausführen
--
-- Speichert 5-Min-/METAR-Beobachtungen pro Station (MADIS HFMETAR + AWC METAR
-- via poll_madis_hfmetar.py; historisch auch Synoptic via poll_synoptic_5min.py).
--
-- Jeder Poll-Lauf bekommt eine eigene poll_counter-ID (synoptic_poll_runs) und
-- schreibt ALLE in diesem Lauf gelesenen Beobachtungen. Dadurch sind Telegram-
-- Statusmeldungen und Poll-Latenzen aus der DB rekonstruierbar.
-- Dedup pro Lauf: UNIQUE (poll_counter, station, observed_at_utc).

CREATE TABLE IF NOT EXISTS synoptic_poll_runs (
    poll_counter BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    polled_at_utc DATETIME NOT NULL,
    observation_count INT UNSIGNED NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (poll_counter),
    KEY idx_polled_at (polled_at_utc)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS synoptic_5min_obs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    poll_counter BIGINT UNSIGNED NOT NULL,
    station VARCHAR(10) NOT NULL,
    observed_at_utc DATETIME NOT NULL,
    air_temp_c DECIMAL(4,1) NULL,
    air_temp_f DECIMAL(5,1) NULL,
    is_metar TINYINT(1) NOT NULL DEFAULT 0,
    metar_raw VARCHAR(255) NULL,
    fetched_at_utc DATETIME NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_poll_station_observed (poll_counter, station, observed_at_utc),
    KEY idx_station_observed (station, observed_at_utc),
    KEY idx_poll_counter (poll_counter)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
