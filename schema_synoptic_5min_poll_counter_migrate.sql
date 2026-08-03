-- Migration: bestehende synoptic_5min_obs auf Poll-Counter-Semantik umstellen.
-- Einmalig ausführen VOR dem nächsten Poll-Lauf mit dem neuen poll_madis_hfmetar.py.
--
-- Bestehende Zeilen erhalten poll_counter = 1 (historischer Sammel-Lauf).
-- synoptic_poll_runs startet mit #1; der nächste Poll bekommt #2 usw.
--
-- Bei Fehlern „Duplicate column/key“: Schritt bereits erledigt, weiter zum nächsten.

CREATE TABLE IF NOT EXISTS synoptic_poll_runs (
    poll_counter BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    polled_at_utc DATETIME NOT NULL,
    observation_count INT UNSIGNED NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (poll_counter),
    KEY idx_polled_at (polled_at_utc)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- poll_counter-Spalte (Default 1 für Altbestand)
ALTER TABLE synoptic_5min_obs
    ADD COLUMN poll_counter BIGINT UNSIGNED NOT NULL DEFAULT 1 AFTER id;

-- Alten Unique-Key entfernen, neuen setzen
ALTER TABLE synoptic_5min_obs DROP INDEX uq_station_observed;
ALTER TABLE synoptic_5min_obs
    ADD UNIQUE KEY uq_poll_station_observed (poll_counter, station, observed_at_utc);
ALTER TABLE synoptic_5min_obs
    ADD KEY idx_station_observed (station, observed_at_utc);
ALTER TABLE synoptic_5min_obs
    ADD KEY idx_poll_counter (poll_counter);

-- Seed-Lauf #1 für Altbestand (nur wenn noch leer)
INSERT INTO synoptic_poll_runs (poll_counter, polled_at_utc, observation_count)
SELECT 1, UTC_TIMESTAMP(), COUNT(*) FROM synoptic_5min_obs
WHERE (SELECT COUNT(*) FROM synoptic_poll_runs) = 0;

-- Nächster AUTO_INCREMENT-Wert
ALTER TABLE synoptic_poll_runs AUTO_INCREMENT = 2;
