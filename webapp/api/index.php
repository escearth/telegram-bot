<?php
$uri = $_SERVER['REQUEST_URI'];
$path = strtok($uri, '?');

// Cache public endpoints for 30 seconds
$cacheFile = null;
if ($path === '/api/prices' || $path === '/api/market') {
    $cacheFile = sys_get_temp_dir() . '/webapp_cache_' . md5($path);
    if (file_exists($cacheFile) && (time() - filemtime($cacheFile)) < 30) {
        header('Content-Type: application/json; charset=utf-8');
        header('Cache-Control: no-store');
        readfile($cacheFile);
        exit;
    }
}

$ch = curl_init('http://127.0.0.1:8080' . $uri);
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_TIMEOUT => 15,
    CURLOPT_CONNECTTIMEOUT => 2,
]);
if (isset($_SERVER['HTTP_X_TELEGRAM_INIT_DATA'])) {
    curl_setopt($ch, CURLOPT_HTTPHEADER, ['X-Telegram-Init-Data: ' . $_SERVER['HTTP_X_TELEGRAM_INIT_DATA']]);
}
$body = curl_exec($ch);
$code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
curl_close($ch);

if ($body === false) { http_response_code(502); echo '{"ok":false,"error":"proxy"}'; exit; }

if ($cacheFile && $code === 200) file_put_contents($cacheFile, $body);

http_response_code($code);
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
echo $body;
