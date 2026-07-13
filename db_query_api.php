<?php
/**
 * Generische MariaDB-Query-API für Kasserver (localhost).
 *
 * POST JSON: {"sql":"SELECT ...", "params": [optional bind values]}
 * Header:    X-API-Key: <api_key aus config.db.php>
 *
 * Antwort:    {"ok":true,"columns":[...],"rows":[...],"count":N,"elapsed_ms":12}
 *
 * Standard: nur SELECT (allow_writes=false in config.db.php).
 */
declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');

function respond(int $status, array $payload): void
{
    http_response_code($status);
    echo json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

function load_config(): array
{
    $path = __DIR__ . '/config.db.php';
    if (!is_file($path)) {
        respond(500, ['ok' => false, 'error' => 'config.db.php fehlt']);
    }
    $config = require $path;
    if (!is_array($config)) {
        respond(500, ['ok' => false, 'error' => 'config.db.php ungültig']);
    }
    return $config;
}

function normalize_sql(string $sql): string
{
    $sql = trim($sql);
    $sql = preg_replace('/^\uFEFF/', '', $sql) ?? $sql;
    $sql = rtrim($sql, " \t\n\r\0\x0B;");
    return $sql;
}

function starts_with(string $haystack, string $needle): bool
{
    return $needle === '' || strpos($haystack, $needle) === 0;
}

function is_read_only_sql(string $sql): bool
{
    $upper = strtoupper(ltrim($sql));
    if (
        !starts_with($upper, 'SELECT')
        && !starts_with($upper, 'SHOW')
        && !starts_with($upper, 'DESCRIBE')
        && !starts_with($upper, 'DESC')
        && !starts_with($upper, 'EXPLAIN')
    ) {
        return false;
    }
    $blocked = [
        'INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'CREATE', 'TRUNCATE',
        'REPLACE', 'GRANT', 'REVOKE', 'CALL', 'LOAD', 'INTO OUTFILE', 'INTO DUMPFILE',
    ];
    foreach ($blocked as $keyword) {
        if (preg_match('/\b' . preg_quote($keyword, '/') . '\b/i', $sql)) {
            return false;
        }
    }
    return substr_count($sql, ';') <= 1;
}

$config = load_config();
$apiKey = (string) ($config['api_key'] ?? '');
if ($apiKey === '') {
    respond(500, ['ok' => false, 'error' => 'api_key in config.db.php fehlt']);
}

$providedKey = $_SERVER['HTTP_X_API_KEY'] ?? ($_GET['api_key'] ?? '');
if (!is_string($providedKey) || !hash_equals($apiKey, $providedKey)) {
    respond(401, ['ok' => false, 'error' => 'Ungültiger API-Key']);
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    respond(405, ['ok' => false, 'error' => 'Nur POST erlaubt']);
}

$rawBody = file_get_contents('php://input') ?: '';
$body = json_decode($rawBody, true);
if (!is_array($body)) {
    respond(400, ['ok' => false, 'error' => 'JSON-Body erwartet: {"sql":"..."}']);
}

$sql = normalize_sql((string) ($body['sql'] ?? ''));
if ($sql === '') {
    respond(400, ['ok' => false, 'error' => 'sql fehlt']);
}

$allowWrites = (bool) ($config['allow_writes'] ?? false);
if (!$allowWrites && !is_read_only_sql($sql)) {
    respond(403, ['ok' => false, 'error' => 'Nur lesende Abfragen erlaubt (SELECT/SHOW/DESCRIBE/EXPLAIN)']);
}

$params = $body['params'] ?? [];
if (!is_array($params)) {
    respond(400, ['ok' => false, 'error' => 'params muss ein Array sein']);
}

$maxRows = (int) ($config['max_rows'] ?? 500);
if ($maxRows < 1) {
    $maxRows = 500;
}

$started = microtime(true);

$mysqli = new mysqli(
    $config['host'] ?? 'localhost',
    $config['user'] ?? '',
    $config['password'] ?? '',
    $config['database'] ?? '',
    (int) ($config['port'] ?? 3306)
);
if ($mysqli->connect_errno) {
    respond(500, ['ok' => false, 'error' => 'DB-Verbindung fehlgeschlagen']);
}
$mysqli->set_charset('utf8mb4');

$stmt = $mysqli->prepare($sql);
if ($stmt === false) {
    respond(400, ['ok' => false, 'error' => 'SQL-Vorbereitung fehlgeschlagen', 'detail' => $mysqli->error]);
}

if ($params !== []) {
    $types = '';
    $bindValues = [];
    foreach ($params as $param) {
        if (is_int($param)) {
            $types .= 'i';
        } elseif (is_float($param)) {
            $types .= 'd';
        } else {
            $types .= 's';
            if ($param === null) {
                $param = null;
            } else {
                $param = (string) $param;
            }
        }
        $bindValues[] = $param;
    }
    $refs = [];
    foreach ($bindValues as $key => $value) {
        $refs[$key] = &$bindValues[$key];
    }
    array_unshift($refs, $types);
    call_user_func_array([$stmt, 'bind_param'], $refs);
}

if (!$stmt->execute()) {
    respond(400, ['ok' => false, 'error' => 'SQL-Ausführung fehlgeschlagen', 'detail' => $stmt->error]);
}

$result = $stmt->get_result();
$rows = [];
$columns = [];

if ($result instanceof mysqli_result) {
    $fields = $result->fetch_fields();
    foreach ($fields as $field) {
        $columns[] = $field->name;
    }
    while ($row = $result->fetch_assoc()) {
        $rows[] = $row;
        if (count($rows) >= $maxRows) {
            break;
        }
    }
    $result->free();
} else {
    $rows = [];
    $columns = [];
}

$stmt->close();
$mysqli->close();

$elapsedMs = (int) round((microtime(true) - $started) * 1000);

respond(200, [
    'ok' => true,
    'columns' => $columns,
    'rows' => $rows,
    'count' => count($rows),
    'truncated' => count($rows) >= $maxRows,
    'elapsed_ms' => $elapsedMs,
]);
