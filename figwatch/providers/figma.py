"""Figma API client and design data fetching."""

import json
import logging
import os
import re
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from figwatch.providers.ai.rate_limit import TokenBucket

logger = logging.getLogger(__name__)

FIGMA_API = 'https://api.figma.com/v1'


class FigmaTokenExpired(Exception):
    """Raised when Figma returns 403 with 'Token expired'."""

# Base64 adds ~33% overhead; cap raw bytes at 3.75 MB to stay under the 5 MB API limit.
_MAX_IMAGE_BYTES = int(3.75 * 1024 * 1024)


def urllib_quote(s):
    return urllib.parse.quote(s, safe='')


# ── Figma rate limit tiers ───────────────────────────────────────────

TIER_1 = 1  # Heavy: full file, nodes, image renders
TIER_2 = 2  # Medium: comments, dev_resources, variables, projects
TIER_3 = 3  # Light: styles, components, metadata

_TIER_PATTERNS: list[tuple[str, int]] = [
    # Tier 1
    ('/images/', TIER_1),
    ('/nodes', TIER_1),
    # Tier 3 (check before generic fallback)
    ('/styles', TIER_3),
    ('/components', TIER_3),
    ('/component_sets', TIER_3),
    ('/meta', TIER_3),
]

# RPM budgets from Figma docs: (tier1, tier2, tier3)
_RATE_LIMITS = {
    'starter': (1, 5, 10),  # Tier 1 is 6/month — use 1 as floor
    ('professional', 'view'): (1, 5, 10),
    ('professional', 'dev'): (10, 25, 50),
    ('organization', 'view'): (1, 5, 10),
    ('organization', 'dev'): (15, 50, 100),
    ('enterprise', 'view'): (1, 5, 10),
    ('enterprise', 'dev'): (15, 50, 100),
}


def endpoint_tier(path: str) -> int:
    """Determine Figma rate limit tier from API path."""
    for pattern, tier in _TIER_PATTERNS:
        if pattern in path:
            return tier
    # Bare /files/:key (with optional query string) = Tier 1
    if re.search(r'/files/[^/]+(\?.*)?$', path):
        return TIER_1
    return TIER_2


class FigmaRateLimiter:
    """Three independent token buckets matching Figma's tiered rate limits."""

    def __init__(self, plan: str, seat: str = 'dev'):
        if plan == 'starter':
            key = 'starter'
        else:
            key = (plan, seat)
        if key not in _RATE_LIMITS:
            raise ValueError(f'Unknown plan/seat combination: {key!r}')
        tier1_rpm, tier2_rpm, tier3_rpm = _RATE_LIMITS[key]
        self._buckets = {
            TIER_1: TokenBucket(capacity=max(tier1_rpm, 1), refill_per_second=tier1_rpm / 60),
            TIER_2: TokenBucket(capacity=tier2_rpm, refill_per_second=tier2_rpm / 60),
            TIER_3: TokenBucket(capacity=tier3_rpm, refill_per_second=tier3_rpm / 60),
        }

    def acquire(self, path: str) -> None:
        """Block until a token is available in the correct tier bucket."""
        tier = endpoint_tier(path)
        self._buckets[tier].acquire()

    def backoff(self, path: str, retry_after: float) -> None:
        """Drain the tier bucket after a 429, forcing other threads to wait.

        Sets the bucket's token count to negative so threads must wait for
        refill_rate * retry_after seconds before proceeding.
        """
        tier = endpoint_tier(path)
        bucket = self._buckets[tier]
        with bucket._lock:
            # Drain tokens so subsequent acquires block for ~retry_after seconds
            bucket._tokens = -(bucket._refill_rate * retry_after)


# ── REST helpers ──────────────────────────────────────────────────────

def _check_token_expired(e):
    """Raise FigmaTokenExpired if an HTTPError is a 403 token-expiry response."""
    if e.code != 403:
        return
    try:
        body = json.loads(e.read())
    except Exception:
        return
    if 'token expired' in str(body.get('err', '')).lower():
        raise FigmaTokenExpired(
            'Figma token expired — generate a new token at '
            'https://www.figma.com/developers/api#access-tokens'
        ) from e


def _make_request(url, pat, method='GET', body=None):
    headers = {'X-Figma-Token': pat}
    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            if method == 'DELETE':
                return None
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        _check_token_expired(e)
        raise


def validate_token(pat):
    """Check token validity against /v1/me. Returns user handle or raises.

    Raises FigmaTokenExpired on expired token, RuntimeError on other failures.
    """
    try:
        data = _make_request(f'{FIGMA_API}/me', pat)
    except FigmaTokenExpired:
        raise
    except Exception as e:
        raise RuntimeError(f'Figma token validation failed: {e}') from e
    handle = (data or {}).get('handle')
    if not handle:
        raise RuntimeError('Figma token validation returned no user handle')
    return handle


def figma_get(path, pat):
    return _make_request(f'{FIGMA_API}{path}', pat)


def figma_post(path, body, pat):
    return _make_request(f'{FIGMA_API}{path}', pat, method='POST', body=body)


def figma_delete(path, pat):
    _make_request(f'{FIGMA_API}{path}', pat, method='DELETE')


def figma_get_retry(path, pat, retries=1, timeout=15, limiter=None):
    """GET a Figma API endpoint with retry on 429. Returns parsed JSON or None.

    Raises FigmaTokenExpired immediately on 403 token-expiry (no retry).
    Raises socket.timeout / TimeoutError on timeout so callers can distinguish
    slow responses from other failures.

    If *limiter* is provided, acquires from the appropriate tier bucket before
    making the request.
    """
    if limiter:
        limiter.acquire(path)
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                f'{FIGMA_API}{path}',
                headers={'X-Figma-Token': pat},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            _check_token_expired(e)
            if e.code == 429 and attempt < retries:
                try:
                    wait = int(e.headers.get('Retry-After', '0') or 0)
                except Exception:
                    wait = 0
                wait = max(wait, 2)
                if limiter:
                    limiter.backoff(path, wait)
                logger.warning(
                    'figma 429 — retrying',
                    extra={'path': path, 'retry_in_seconds': wait},
                )
                time.sleep(wait)
                continue
            logger.warning('figma API error',
                           extra={'path': path, 'status': e.code})
            return None
        except (socket.timeout, TimeoutError):
            logger.warning('figma API timeout',
                           extra={'path': path, 'timeout': timeout})
            raise
        except Exception as e:
            logger.warning('figma API call failed',
                           extra={'path': path, 'error': str(e)})
            return None
    return None


# ── Node data extraction ──────────────────────────────────────────────

def extract_text_from_node(node):
    texts = []

    def walk(n):
        if n.get('visible') is False:
            return
        if n.get('type') == 'TEXT' and (n.get('characters') or '').strip():
            box = n.get('absoluteBoundingBox') or {}
            texts.append({
                'name': n.get('name', ''),
                'text': n['characters'],
                'id': n.get('id', ''),
                'x': box.get('x', 0),
                'y': box.get('y', 0),
                'w': box.get('width', 0),
                'h': box.get('height', 0),
            })
        for child in n.get('children', []):
            walk(child)

    walk(node)
    return texts


def _extract_prototype_flows(node):
    flows = []

    def walk(n):
        for reaction in n.get('reactions', []):
            flows.append({
                'node_id': n.get('id'),
                'node_name': n.get('name'),
                'trigger': reaction.get('trigger', {}).get('type'),
                'action': reaction.get('action', {}).get('type'),
                'destination': reaction.get('action', {}).get('destinationId'),
            })
        for child in n.get('children', []):
            walk(child)

    walk(node)
    return flows


def _extract_annotations(node):
    annotations = []

    def walk(n):
        name = (n.get('name') or '').lower()
        if 'annotation' in name or 'note' in name:
            annotations.append({
                'id': n.get('id'),
                'name': n.get('name'),
                'type': n.get('type'),
                'characters': n.get('characters', ''),
            })
        for child in n.get('children', []):
            walk(child)

    walk(node)
    return annotations


# ── Data fetching ─────────────────────────────────────────────────────

def fetch_screenshot(file_key, node_id, pat, limiter=None):
    """Download a Figma node screenshot. Returns file path or None.

    Tries progressively smaller PNG scales then falls back to JPEG. Returns None
    if nothing fits within 3.75 MB (safe ceiling before base64 hits the 5 MB API limit).

    Timeouts abort immediately — retrying at smaller scales won't help when
    Figma is slow to render (see #37).
    """
    enc_id = urllib_quote(node_id)
    attempts = [('png', 1), ('png', 0.5), ('jpg', 1), ('jpg', 0.5), ('jpg', 0.25)]

    for fmt, scale in attempts:
        out_path = os.path.join(
            tempfile.gettempdir(),
            f'figwatch-screenshot-{node_id.replace(":", "-")}.{fmt}',
        )
        try:
            data = figma_get_retry(
                f'/images/{file_key}?ids={enc_id}&scale={scale}&format={fmt}',
                pat,
                timeout=45,
                limiter=limiter,
            )
            if not data or data.get('err') or data.get('status') == 400:
                continue
            url = (data.get('images') or {}).get(node_id)
            if not url:
                continue
            with urllib.request.urlopen(url, timeout=30) as r:
                img_bytes = r.read()
            if len(img_bytes) > _MAX_IMAGE_BYTES:
                continue
            with open(out_path, 'wb') as f:
                f.write(img_bytes)
            return out_path
        except (socket.timeout, TimeoutError):
            logger.warning(
                'screenshot render timed out — aborting fallback chain',
                extra={'file_key': file_key, 'node_id': node_id,
                       'format': fmt, 'scale': scale},
            )
            return None
        except Exception:
            continue
    return None


def fetch_node_tree(file_key, node_id, pat, limiter=None):
    """Fetch the full node tree for a Figma node. Returns (file_path, parsed_data) or (None, None)."""
    enc_id = urllib_quote(node_id)
    try:
        data = figma_get_retry(f'/files/{file_key}/nodes?ids={enc_id}&depth=100', pat, limiter=limiter)
        node = data.get('nodes', {}).get(node_id, {}).get('document') if data else None
        if not node:
            return None, None
        out_path = os.path.join(
            tempfile.gettempdir(),
            f'figwatch-tree-{node_id.replace(":", "-")}.json',
        )
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(node, f, indent=2)
        return out_path, node
    except Exception:
        return None, None


def fetch_figma_data(required_data, file_key, node_id, pat, limiter=None):
    """Fetch only the declared data points from Figma API in parallel.

    Returns (dict[data_type -> value], tree_data).
    """
    result = {}
    enc_id = urllib_quote(node_id)
    tree_data = None

    futures = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        if 'screenshot' in required_data:
            futures['screenshot'] = pool.submit(fetch_screenshot, file_key, node_id, pat, limiter=limiter)
        needs_tree = any(k in required_data for k in ('node_tree', 'text_nodes', 'annotations', 'prototype_flows'))
        if needs_tree:
            futures['_tree'] = pool.submit(fetch_node_tree, file_key, node_id, pat, limiter=limiter)
        if 'dev_resources' in required_data:
            futures['dev_resources'] = pool.submit(
                figma_get_retry, f'/files/{file_key}/dev_resources?node_ids={enc_id}', pat, limiter=limiter
            )
        if 'variables_local' in required_data:
            futures['variables_local'] = pool.submit(
                figma_get_retry, f'/files/{file_key}/variables/local', pat, limiter=limiter
            )
        if 'variables_published' in required_data:
            futures['variables_published'] = pool.submit(
                figma_get_retry, f'/files/{file_key}/variables/published', pat, limiter=limiter
            )
        if 'styles' in required_data:
            futures['styles'] = pool.submit(figma_get_retry, f'/files/{file_key}/styles', pat, limiter=limiter)
        if 'components' in required_data:
            futures['components'] = pool.submit(figma_get_retry, f'/files/{file_key}/components', pat, limiter=limiter)
        if 'file_structure' in required_data:
            futures['file_structure'] = pool.submit(figma_get_retry, f'/files/{file_key}?depth=2', pat, limiter=limiter)

        for key, future in futures.items():
            try:
                result[key] = future.result()
            except Exception:
                result[key] = None

    tree_result = result.pop('_tree', None)
    if tree_result:
        tree_path, tree_data = tree_result
        result['node_tree'] = tree_path

    if tree_data:
        if 'text_nodes' in required_data:
            result['text_nodes'] = extract_text_from_node(tree_data)
        if 'prototype_flows' in required_data:
            result['prototype_flows'] = _extract_prototype_flows(tree_data)
        if 'annotations' in required_data:
            result['annotations'] = _extract_annotations(tree_data)

    return result, tree_data


# ── Repository implementations ───────────────────────────────────────

class FigmaCommentRepository:
    """CommentRepository implementation backed by the Figma REST API."""

    def __init__(self, pat: str):
        self._pat = pat

    def post_reply(self, file_key: str, parent_comment_id: str, message: str):
        resp = figma_post(f'/files/{file_key}/comments', {
            'message': message,
            'comment_id': parent_comment_id,
        }, self._pat)
        return resp.get('id')

    def delete_comment(self, file_key: str, comment_id: str) -> None:
        try:
            figma_delete(f'/files/{file_key}/comments/{comment_id}', self._pat)
        except Exception:
            pass

    def fetch_comments(self, file_key: str) -> list:
        data = figma_get(f'/files/{file_key}/comments', self._pat)
        return (data or {}).get('comments', [])


class FigmaDesignDataRepository:
    """DesignDataRepository implementation backed by the Figma REST API."""

    def __init__(self, pat: str, limiter: 'FigmaRateLimiter | None' = None):
        self._pat = pat
        self._limiter = limiter

    def fetch(self, required_data: list, file_key: str, node_id: str) -> tuple:
        return fetch_figma_data(required_data, file_key, node_id, self._pat, limiter=self._limiter)
