import hashlib
import hmac
import json
import os
import time
import urllib.parse
import pytest

os.environ['TELEGRAM_BOT_TOKEN'] = '123456:dummy_token_for_testing'

import bot


def _generate_valid_init_data(uid=123456, first_name="TestUser", token=None, auth_date=None):
    token = token or bot.TELEGRAM_BOT_TOKEN
    auth_date = auth_date or str(int(time.time()))
    user_json = json.dumps({'id': uid, 'first_name': first_name}, separators=(',', ':'))
    params = {
        'auth_date': auth_date,
        'user': user_json,
    }
    data_check = "\n".join(f"{k}={params[k]}" for k in sorted(params))
    secret = hmac.new(b"WebAppData", token.encode('utf-8'), hashlib.sha256).digest()
    calc_hash = hmac.new(secret, data_check.encode('utf-8'), hashlib.sha256).hexdigest()
    params['hash'] = calc_hash
    return urllib.parse.urlencode(params)


def test_webapp_validate_init_data_valid():
    init_data = _generate_valid_init_data(uid=987654)
    uid = bot._webapp_validate_init_data(init_data)
    assert uid == 987654


def test_webapp_validate_init_data_expired():
    old_time = str(int(time.time()) - 100000)
    init_data = _generate_valid_init_data(uid=987654, auth_date=old_time)
    uid = bot._webapp_validate_init_data(init_data)
    assert uid is None


def test_webapp_validate_init_data_tampered():
    init_data = _generate_valid_init_data(uid=987654)
    tampered = init_data.replace('987654', '111111')
    uid = bot._webapp_validate_init_data(tampered)
    assert uid is None


def test_webapp_validate_init_data_empty():
    assert bot._webapp_validate_init_data("") is None
    assert bot._webapp_validate_init_data(None) is None


def test_webapp_route_public_prices():
    status, headers, body = bot._webapp_route('GET', '/api/prices', None, {})
    assert status == 200
    data = json.loads(body.decode('utf-8'))
    assert data.get('ok') is True
    assert 'coins' in data


def test_webapp_route_public_market():
    status, headers, body = bot._webapp_route('GET', '/api/market', None, {})
    assert status == 200
    data = json.loads(body.decode('utf-8'))
    assert data.get('ok') is True


def test_webapp_route_protected_unauthorized():
    status, headers, body = bot._webapp_route('GET', '/api/portfolio', None, {})
    assert status == 401
    data = json.loads(body.decode('utf-8'))
    assert data.get('ok') is False
    assert data.get('error') == 'unauthorized'


def test_webapp_route_protected_authorized():
    status, headers, body = bot._webapp_route('GET', '/api/portfolio', 123456, {})
    assert status == 200
    data = json.loads(body.decode('utf-8'))
    assert data.get('ok') is True
    assert 'items' in data


def test_webapp_wsgi_handling():
    init_data = _generate_valid_init_data(uid=55555)
    environ = {
        'PATH_INFO': '/api/portfolio',
        'REQUEST_METHOD': 'GET',
        'QUERY_STRING': '',
        'HTTP_X_TELEGRAM_INIT_DATA': init_data,
    }
    recorded_status = []
    recorded_headers = []

    def start_response(status, headers):
        recorded_status.append(status)
        recorded_headers.append(headers)

    res = bot._webapp_wsgi(environ, start_response)
    assert recorded_status[0].startswith("200")
    data = json.loads(res[0].decode('utf-8'))
    assert data.get('ok') is True
