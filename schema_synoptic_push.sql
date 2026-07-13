-- MariaDB 10.6 – Spieldatenbank crondb
-- In phpMyAdmin: Tab "SQL" → einfügen → Ausführen
-- (Optional: stream_synoptic_push.py legt beide Tabellen beim Start
--  automatisch per CREATE TABLE IF NOT EXISTS an.)
--
-- Speichert den Synoptic-Push-Stream (WebSocket, wss://push.synopticdata.com).
-- Bewusst eine EIGENE Tabelle neben synoptic_5min_obs:
--   * Push liefert einzelne Sensorwerte (kein METAR-Rohtext) – anderes Format.
--   * received_at_utc mit Millisekunden erlaubt echte Latenzmessung
--     Push vs. Poll (fetched_at_utc in synoptic_5min_obs).
--   * Die Kanäle bleiben unabhängig vergleichbar; der Poll-Kanal dient als
--     Vollständigkeits-Backstop, der Push-Kanal als Niedriglatenz-Kanal.

CREATE TABLE IF NOT EXISTS synoptic_push_obs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    station VARCHAR(10) NOT NULL,
    sensor VARCHAR(40) NOT NULL,
    sensor_set TINYINT UNSIGNED NOT NULL DEFAULT 1,
    observed_at_utc DATETIME NOT NULL,
    value_num DECIMAL(9,2) NULL,
    qc_flags VARCHAR(255) NULL,
    received_at_utc DATETIME(3) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_push_obs (station, sensor, sensor_set, observed_at_utc),
    KEY idx_push_station_day (station, observed_at_utc)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Merkt sich die WebSocket-Session-ID pro Stream-Konfiguration. Damit kann
-- der nächste Workflow-Lauf die Session fortsetzen (Resume bis 3 Tage) und
-- verpasste Beobachtungen zwischen zwei Läufen lückenlos nachladen.
CREATE TABLE IF NOT EXISTS synoptic_push_state (
    stream_key VARCHAR(120) NOT NULL,
    session_id VARCHAR(64) NULL,
    updated_at_utc DATETIME NOT NULL,
    PRIMARY KEY (stream_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
