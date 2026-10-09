<?php
// Never leak PHP warnings/notices into the JSON body (breaks the response and
// discloses absolute server paths).
ini_set('display_errors', '0');
error_reporting(0);
ini_set('log_errors', '1');

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');

$path = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) ?: '/';

// Only proxy the app's own API namespace.
if (strpos($path, '/api/') !== 0) {
    http_response_code(404);
    echo '{"ok":false,"error":"not found"}';
    exit;
}

// Cache public endpoints for 30 seconds. The cache lives in a per-app
// directory (not /tmp) with 0600 permissions so a local user on shared
// hosting cannot symlink it at a readable/writable file.
$cacheFile = null;
if ($path === '/api/prices' || $path === '/api/market' || $path === '/api/currency') {
    $cacheDir = dirname(dirname(__DIR__)) . '/.webapp_cache';
    if (!is_dir($cacheDir)) {
        @mkdir($cacheDir, 0700, true);
    }
    if (is_dir($cacheDir) && !is_link($cacheDir)) {
        $cacheFile = $cacheDir . '/cache_' . md5($path) . '.json';
        if (is_file($cacheFile) && !is_link($cacheFile) && (time() - filemtime($cacheFile)) < 30) {
            readfile($cacheFile);
            exit;
        }
    }
}

$ch = curl_init('http://127.0.0.1:8080' . $path . (empty($_SERVER['QUERY_STRING']) ? '' : '?' . $_SERVER['QUERY_STRING']));
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_TIMEOUT => 30,
    CURLOPT_CONNECTTIMEOUT => 2,
    CURLOPT_FOLLOWLOCATION => false,
]);
if (isset($_SERVER['HTTP_X_TELEGRAM_INIT_DATA'])) {
    curl_setopt($ch, CURLOPT_HTTPHEADER, ['X-Telegram-Init-Data: ' . $_SERVER['HTTP_X_TELEGRAM_INIT_DATA']]);
}
$body = curl_exec($ch);
$code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
curl_close($ch);

if ($body === false) { http_response_code(502); echo '{"ok":false,"error":"proxy"}'; exit; }

if ($cacheFile && $code === 200 && !is_link($cacheFile)) {
    @file_put_contents($cacheFile, $body, LOCK_EX);
    @chmod($cacheFile, 0600);
}

http_response_code($code ?: 502);
echo $body;
