<?php
/**
 * Kasserver-Cron: Lag-CSV nach MariaDB (localhost).
 * Legt config.db.php an (nicht öffentlich zugänglich).
 */
declare(strict_types=1);

$configFile = __DIR__ . '/config.db.php';
if (!is_file($configFile)) {
    fwrite(STDERR, "Fehlt: config.db.php\n");
    exit(1);
}
$config = require $configFile;

$csvFile = $config['csv_path'] ?? (__DIR__ . '/dwd_feed_lag_log.csv');
if (!is_file($csvFile)) {
    fwrite(STDERR, "CSV fehlt: {$csvFile}\n");
    exit(1);
}

$mysqli = new mysqli(
    $config['host'] ?? 'localhost',
    $config['user'],
    $config['password'],
    $config['database'],
    (int) ($config['port'] ?? 3306)
);
if ($mysqli->connect_errno) {
    fwrite(STDERR, 'DB-Fehler: ' . $mysqli->connect_error . "\n");
    exit(1);
}
$mysqli->set_charset('utf8mb4');

$sql = <<<'SQL'
INSERT INTO dwd_feed_lag (
    logged_at_berlin, dwd_latest, dwd_lag_min, dwd_values_today,
    dwd_max, dwd_max_time, metar_latest, metar_lag_min, metar_values_today,
    metar_max, metar_max_time, dwd_tt10, metar_temp, metar_raw_ob
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON DUPLICATE KEY UPDATE
    dwd_latest=VALUES(dwd_latest), dwd_lag_min=VALUES(dwd_lag_min),
    dwd_values_today=VALUES(dwd_values_today), dwd_max=VALUES(dwd_max),
    dwd_max_time=VALUES(dwd_max_time), metar_latest=VALUES(metar_latest),
    metar_lag_min=VALUES(metar_lag_min), metar_values_today=VALUES(metar_values_today),
    metar_max=VALUES(metar_max), metar_max_time=VALUES(metar_max_time),
    dwd_tt10=VALUES(dwd_tt10), metar_temp=VALUES(metar_temp),
    metar_raw_ob=VALUES(metar_raw_ob)
SQL;

$stmt = $mysqli->prepare($sql);
$handle = fopen($csvFile, 'r');
$header = fgetcsv($handle);
$count = 0;

while (($row = fgetcsv($handle)) !== false) {
    $data = array_combine($header, $row);
    if (empty($data['logged_at_berlin'])) {
        continue;
    }
    $nullable = static function (string $key) use ($data): ?string {
        $value = trim($data[$key] ?? '');
        return $value === '' ? null : $value;
    };
    $raw = $nullable('metar_rawOb');
    if ($raw !== null) {
        $raw = substr($raw, 0, 255);
    }

    $stmt->bind_param(
        'ssiiidsiiidsds',
        $data['logged_at_berlin'],
        $nullable('dwd_latest'),
        $nullable('dwd_lag_min'),
        $nullable('dwd_values_today'),
        $nullable('dwd_max'),
        $nullable('dwd_max_time'),
        $nullable('metar_latest'),
        $nullable('metar_lag_min'),
        $nullable('metar_values_today'),
        $nullable('metar_max'),
        $nullable('metar_max_time'),
        $nullable('dwd_TT_10'),
        $nullable('metar_temp'),
        $raw
    );
    $stmt->execute();
    $count++;
}
fclose($handle);
$stmt->close();
$mysqli->close();

echo "OK: {$count} Zeilen verarbeitet.\n";
