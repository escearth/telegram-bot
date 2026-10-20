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

// Sanitize the incoming path before any prefix/forwarding logic: decode it,
// then reject traversal attempts, NUL bytes and other control characters.
$path = rawurldecode($path);
if ($path === '' || strpos($path, '..') !== false || strpos($path, "\0") !== false
    || preg_match('/[\x00-\x1f\x7f]/', $path)) {
    http_response_code(400);
    echo '{"ok":false,"error":"bad request"}';
    exit;
}

// Only proxy the app's own API namespace.
if (strpos($path, '/api/') !== 0) {
    http_response_code(404);
    echo '{"ok":false,"error":"not found"}';
    exit;
}

// DEV BYPASS LOCKDOWN: the ?dev_uid fallback must never forward on shared
// hosting by default. It is only honoured when WEBAPP_ALLOW_DEV === '1' AND
// (if WEBAPP_DEV_SECRET is set) the request carries the matching secret.
if (empty($_SERVER['HTTP_X_TELEGRAM_INIT_DATA'])) {
    if (getenv('WEBAPP_ALLOW_DEV') !== '1') {
        unset($_GET['dev'], $_GET['dev_uid']);
    } else {
        $devSecret = getenv('WEBAPP_DEV_SECRET');
        $given = $_GET['dev_secret']
            ?? (isset($_SERVER['HTTP_X_DEV_SECRET']) ? $_SERVER['HTTP_X_DEV_SECRET'] : null);
        if ($devSecret !== false && $devSecret !== '' && $given !== $devSecret) {
            unset($_GET['dev'], $_GET['dev_uid']);
        }
    }
    // Rebuild the query string from the (possibly sanitized) $_GET.
    $query = http_build_query($_GET);
} else {
    // Requests WITH initData keep their query untouched.
    $query = $_SERVER['QUERY_STRING'] ?? '';
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
        // Include the query string in the key so users with different
        // currency/locale params never see each other's cached responses.
        $cacheFile = $cacheDir . '/cache_' . md5($path . '?' . $query) . '.json';
        if (is_file($cacheFile) && !is_link($cacheFile) && (time() - filemtime($cacheFile)) < 30) {
            readfile($cacheFile);
            exit;
        }
    }
}

$ch = curl_init('http://127.0.0.1:8080' . $path . ($query === '' ? '' : '?' . $query));
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_TIMEOUT => 20,
    CURLOPT_CONNECTTIMEOUT => 2,
    CURLOPT_FOLLOWLOCATION => false,
]);
if (isset($_SERVER['HTTP_X_TELEGRAM_INIT_DATA'])) {
    // Strip CR/LF so a poisoned header value cannot inject extra headers.
    $initData = str_replace(["\r", "\n"], '', $_SERVER['HTTP_X_TELEGRAM_INIT_DATA']);
    curl_setopt($ch, CURLOPT_HTTPHEADER, ['X-Telegram-Init-Data: ' . $initData]);
}
$body = curl_exec($ch);
$code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
curl_close($ch);

if ($body === false) { http_response_code(502); echo '{"ok":false,"error":"proxy"}'; exit; }

// Only cache bodies that are valid, successful JSON — never cache errors,
// partial responses or HTML noise.
$d = json_decode($body, true);
if ($cacheFile && $code === 200 && is_array($d) && isset($d['ok']) && $d['ok'] === true
    && !is_link($cacheFile)) {
    // Atomic write: build a temp file beside the target, then rename() so
    // concurrent readers never see a truncated/half-written JSON body.
    $tmp = $cacheFile . '.' . uniqid('', true) . '.tmp';
    if (@file_put_contents($tmp, $body) !== false) {
        @rename($tmp, $cacheFile);
        @chmod($cacheFile, 0600);
    }
    @unlink($tmp); // no-op if rename succeeded
}

http_response_code($code ?: 502);
echo $body;
