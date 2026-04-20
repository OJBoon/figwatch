"""Tests for Figma token expiry detection (#40)."""

import io
import json
import urllib.error
from unittest import mock

import pytest

from figwatch.providers.figma import (
    FigmaTokenExpired,
    _check_token_expired,
    _make_request,
    figma_get,
    figma_get_retry,
    figma_post,
    figma_delete,
    validate_token,
)


def _make_http_error(code, body_dict):
    """Build a urllib HTTPError with a JSON body."""
    body = json.dumps(body_dict).encode()
    resp = io.BytesIO(body)
    err = urllib.error.HTTPError(
        url='https://api.figma.com/v1/me',
        code=code,
        msg='error',
        hdrs={},
        fp=resp,
    )
    return err


# ── _check_token_expired ────────────────────────────────────────────


def test_check_token_expired_raises_on_403_token_expired():
    err = _make_http_error(403, {'status': 403, 'err': 'Token expired'})
    with pytest.raises(FigmaTokenExpired, match='generate a new token'):
        _check_token_expired(err)


def test_check_token_expired_ignores_403_other_message():
    err = _make_http_error(403, {'status': 403, 'err': 'Forbidden'})
    _check_token_expired(err)  # should not raise


def test_check_token_expired_ignores_non_403():
    err = _make_http_error(500, {'status': 500, 'err': 'Token expired'})
    _check_token_expired(err)  # should not raise


def test_check_token_expired_handles_malformed_body():
    err = urllib.error.HTTPError(
        url='https://api.figma.com/v1/me',
        code=403,
        msg='error',
        hdrs={},
        fp=io.BytesIO(b'not json'),
    )
    _check_token_expired(err)  # should not raise


# ── figma_get_retry — no retry on token expiry ──────────────────────


def test_figma_get_retry_raises_token_expired_immediately():
    """Token expiry should propagate without retry."""
    call_count = 0

    def fake_urlopen(req, timeout=None):
        nonlocal call_count
        call_count += 1
        raise _make_http_error(403, {'status': 403, 'err': 'Token expired'})

    with mock.patch('figwatch.providers.figma.urllib.request.urlopen', fake_urlopen):
        with pytest.raises(FigmaTokenExpired):
            figma_get_retry('/me', 'expired-pat', retries=3)

    assert call_count == 1, 'should not retry on token expiry'


# ── _make_request — token expiry detection ───────────────────────────


def test_make_request_raises_token_expired():
    def fake_urlopen(req, timeout=None):
        raise _make_http_error(403, {'status': 403, 'err': 'Token expired'})

    with mock.patch('figwatch.providers.figma.urllib.request.urlopen', fake_urlopen):
        with pytest.raises(FigmaTokenExpired):
            figma_get('/me', 'expired-pat')


def test_make_request_post_raises_token_expired():
    def fake_urlopen(req, timeout=None):
        raise _make_http_error(403, {'status': 403, 'err': 'Token expired'})

    with mock.patch('figwatch.providers.figma.urllib.request.urlopen', fake_urlopen):
        with pytest.raises(FigmaTokenExpired):
            figma_post('/files/abc/comments', {'message': 'hi'}, 'expired-pat')


def test_make_request_delete_raises_token_expired():
    def fake_urlopen(req, timeout=None):
        raise _make_http_error(403, {'status': 403, 'err': 'Token expired'})

    with mock.patch('figwatch.providers.figma.urllib.request.urlopen', fake_urlopen):
        with pytest.raises(FigmaTokenExpired):
            figma_delete('/files/abc/comments/1', 'expired-pat')


def test_make_request_non_expired_403_still_raises_http_error():
    def fake_urlopen(req, timeout=None):
        raise _make_http_error(403, {'status': 403, 'err': 'Forbidden'})

    with mock.patch('figwatch.providers.figma.urllib.request.urlopen', fake_urlopen):
        with pytest.raises(urllib.error.HTTPError):
            figma_get('/me', 'bad-pat')


# ── validate_token ──────────────────────────────────────────────────


def test_validate_token_success():
    ctx = mock.MagicMock()
    ctx.__enter__ = mock.Mock(return_value=ctx)
    ctx.__exit__ = mock.Mock(return_value=False)
    ctx.read.return_value = json.dumps({'handle': 'testuser'}).encode()

    with mock.patch('figwatch.providers.figma.urllib.request.urlopen', return_value=ctx):
        assert validate_token('good-pat') == 'testuser'


def test_validate_token_expired():
    def fake_urlopen(req, timeout=None):
        raise _make_http_error(403, {'status': 403, 'err': 'Token expired'})

    with mock.patch('figwatch.providers.figma.urllib.request.urlopen', fake_urlopen):
        with pytest.raises(FigmaTokenExpired):
            validate_token('expired-pat')


def test_validate_token_no_handle():
    ctx = mock.MagicMock()
    ctx.__enter__ = mock.Mock(return_value=ctx)
    ctx.__exit__ = mock.Mock(return_value=False)
    ctx.read.return_value = json.dumps({}).encode()

    with mock.patch('figwatch.providers.figma.urllib.request.urlopen', return_value=ctx):
        with pytest.raises(RuntimeError, match='no user handle'):
            validate_token('weird-pat')


def test_validate_token_network_error():
    def fake_urlopen(req, timeout=None):
        raise ConnectionError('no network')

    with mock.patch('figwatch.providers.figma.urllib.request.urlopen', fake_urlopen):
        with pytest.raises(RuntimeError, match='validation failed'):
            validate_token('offline-pat')
