"""Figma API client and design data fetching."""

import contextlib
import json
import logging
import os
import re
import socket
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from figwatch.providers.ai.rate_limit import TokenBucket
from figwatch.tracing import TracedThreadPoolExecutor, get_tracer

logger = logging.getLogger(__name__)

FIGMA_API = 'https://api.figma.com/v1'


def _ssl_context():
    """Build SSL context. Uses certifi CA bundle if available, else system default."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_CTX = _ssl_context()


class FigmaTokenExpired(Exception):
    """Raised when Figma returns 403 with 'Token expired'."""

# Base64 adds ~33% overhead; cap raw bytes at 3.75 MB to stay under the 5 MB API limit.
_MAX_IMAGE_BYTES = int(3.75 * 1024 * 1024)

# Whole-journey review: a frame whose direct children are several screen-sized
# frames is treated as a multi-screen board — each child is screenshotted and
# reviewed together, since Figma can't render the whole board as one image.
JOURNEY_MIN_SCREENS = 3       # >= this many screen-sized direct children => journey
JOURNEY_MAX_SCREENS = 10      # cap of screens sent to the model
_JOURNEY_MIN_DIM = 200        # a "screen" child must be at least this wide AND tall (px)
_JOURNEY_MAX_NEST_DEPTH = 3   # how deep to descend into container/section frames
_JOURNEY_MIN_CONTAINER_SCREENS = 2  # a child with >= this many screen kids is a container
_SCREEN_TYPES = {'FRAME', 'COMPONENT', 'INSTANCE', 'SECTION'}


def urllib_quote(s):
    return urllib.parse.quote(s, safe='')


def extract_file_key(s):
    """File key from a Figma file URL, or a bare key; None if neither."""
    m = re.search(r'figma\.com/(?:design|file|board)/([a-zA-Z0-9]+)', s)
    if m:
        return m.group(1)
    s = s.strip()
    return s if re.match(r'^[a-zA-Z0-9]{10,}$', s) else None


def _temp_path(prefix, node_id, ext):
    """A stable /tmp path for a node's exported asset."""
    return os.path.join(tempfile.gettempdir(), f'{prefix}-{node_id.replace(":", "-")}.{ext}')


def _download_capped(url, out_path):
    """Download an image URL to out_path if it fits _MAX_IMAGE_BYTES; return the
    path, or None if oversized. Raises (e.g. socket.timeout) so callers decide."""
    with urllib.request.urlopen(url, timeout=30, context=SSL_CTX) as r:
        # Read one byte past the cap: enough to know it's too big without
        # buffering an oversized render in full.
        img = r.read(_MAX_IMAGE_BYTES + 1)
    if len(img) > _MAX_IMAGE_BYTES:
        return None
    with open(out_path, 'wb') as f:
        f.write(img)
    return out_path


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
        key = 'starter' if plan == 'starter' else (plan, seat)
        if key not in _RATE_LIMITS:
            raise ValueError(f'Unknown plan/seat combination: {key!r}')
        tier1_rpm, tier2_rpm, tier3_rpm = _RATE_LIMITS[key]
        self._buckets = {
            TIER_1: TokenBucket.per_minute(tier1_rpm),
            TIER_2: TokenBucket.per_minute(tier2_rpm),
            TIER_3: TokenBucket.per_minute(tier3_rpm),
        }

    def acquire(self, path: str) -> None:
        """Block until a token is available in the correct tier bucket."""
        tier = endpoint_tier(path)
        self._buckets[tier].acquire()

    def backoff(self, path: str, retry_after: float) -> None:
        """Drain the tier bucket after a 429 so other threads wait ~retry_after seconds."""
        self._buckets[endpoint_tier(path)].drain(retry_after)


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


def _make_request(url, pat, method='GET', body=None, timeout=15):
    headers = {'X-Figma-Token': pat}
    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
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
    tracer = get_tracer()
    with tracer.start_as_current_span('figma.api_call', attributes={
        'figma.endpoint': path,
        'figma.rate_limit_tier': endpoint_tier(path),
    }) as span:
        if limiter:
            limiter.acquire(path)
        for attempt in range(retries + 1):
            try:
                data = _make_request(f'{FIGMA_API}{path}', pat, timeout=timeout)
                span.set_attribute('figma.status_code', 200)
                span.set_attribute('figma.retry_count', attempt)
                return data
            except urllib.error.HTTPError as e:
                # _make_request already turned a 403 token-expiry into FigmaTokenExpired.
                span.set_attribute('figma.status_code', e.code)
                span.set_attribute('figma.retry_count', attempt)
                if e.code == 429 and attempt < retries:
                    try:
                        wait = int(e.headers.get('Retry-After', '0') or 0)
                    except Exception:
                        wait = 0
                    wait = min(max(wait, 2), 10)  # cap so a large Retry-After can't stall a worker
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
            except FigmaTokenExpired:
                raise
            except (socket.timeout, TimeoutError):
                span.set_attribute('figma.retry_count', attempt)
                span.record_exception(socket.timeout('timeout'))
                logger.warning('figma API timeout',
                               extra={'path': path, 'timeout': timeout})
                raise
            except Exception as e:
                span.set_attribute('figma.retry_count', attempt)
                span.record_exception(e)
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


def _prune_hidden(node):
    """Recursively drop children with ``visible: false`` (and their subtrees) so
    hidden Figma layers aren't fed to the model via the node tree or text
    extraction. Visible layers omit the property or set it true; only an explicit
    ``false`` is dropped."""
    children = node.get('children')
    if isinstance(children, list):
        node['children'] = [
            _prune_hidden(c) for c in children if c.get('visible') is not False
        ]
    return node


# ── Journey (multi-screen) enumeration, ordering, capture ─────────────

def _screen_sized(box):
    box = box or {}
    return ((box.get('width') or 0) >= _JOURNEY_MIN_DIM
            and (box.get('height') or 0) >= _JOURNEY_MIN_DIM)


def _is_screen(node):
    """A visible, screen-sized FRAME/COMPONENT/INSTANCE/SECTION with an id."""
    return (node.get('id') and node.get('visible') is not False
            and node.get('type') in _SCREEN_TYPES
            and _screen_sized(node.get('absoluteBoundingBox')))


def _screen_child_count(node):
    """Number of a node's direct children that are screens."""
    return sum(1 for c in (node.get('children') or []) if _is_screen(c))


def enumerate_child_screens(tree_data, _depth=0):
    """The individual screens inside a board, for a journey review.

    A screen is a visible FRAME/COMPONENT/INSTANCE/SECTION at least
    ``_JOURNEY_MIN_DIM`` px on both sides. Boards often nest screens under
    labelled group/section frames (or a big wrapper frame), so a child that is
    itself a *container* of several screen-sized frames is descended into rather
    than captured whole. Returns [{id, name, type, x, y, w, h}].
    """
    screens = []
    for child in (tree_data or {}).get('children', []) or []:
        if not _is_screen(child):
            continue
        if (_depth < _JOURNEY_MAX_NEST_DEPTH
                and _screen_child_count(child) >= _JOURNEY_MIN_CONTAINER_SCREENS):
            # A container of screens (a section / wrapper) — go one level deeper.
            screens.extend(enumerate_child_screens(child, _depth + 1))
        else:
            box = child.get('absoluteBoundingBox') or {}
            screens.append({
                'id': child['id'], 'name': child.get('name', ''), 'type': child.get('type'),
                'x': box.get('x', 0), 'y': box.get('y', 0),
                'w': box.get('width', 0), 'h': box.get('height', 0),
            })
    return screens


def is_journey_frame(tree_data):
    """True if the node is a multi-screen board (>= JOURNEY_MIN_SCREENS screens)."""
    return len(enumerate_child_screens(tree_data)) >= JOURNEY_MIN_SCREENS


def _reading_order(screens):
    """Row-major reading order: group into rows by vertical overlap, rows top to
    bottom, left-to-right within a row."""
    rows = []
    for c in sorted(screens, key=lambda s: (s['y'], s['x'])):
        for row in rows:
            ref = row[0]
            if c['y'] < ref['y'] + ref['h'] and ref['y'] < c['y'] + c['h']:
                row.append(c)
                break
        else:
            rows.append([c])
    result = []
    for row in rows:
        result.extend(sorted(row, key=lambda s: s['x']))
    return result


def _screen_owner_map(tree_data, screen_ids):
    """Map every node id to the screen that contains it, in one DFS.

    A node's owner is the nearest ancestor (or itself) whose id is in
    ``screen_ids`` — so a reaction on a deep button is attributed to its screen.
    """
    owner = {}

    def walk(node, current):
        nid = node.get('id')
        if nid in screen_ids:
            current = nid
        if nid is not None and current is not None:
            owner.setdefault(nid, current)
        for child in node.get('children', []) or []:
            walk(child, current)

    walk(tree_data, None)
    return owner


def _flow_order(screens, tree_data):
    """Order screens by prototype flow when reactions connect the direct-child
    screens; returns None if the prototype doesn't wire them together."""
    by_id = {s['id']: s for s in screens}
    owner = _screen_owner_map(tree_data, set(by_id))
    edges = {}
    for f in _extract_prototype_flows(tree_data):
        src, dst = owner.get(f.get('node_id')), owner.get(f.get('destination'))
        if src and dst and src != dst:
            edges.setdefault(src, dst)
    if not edges:
        return None
    starts = []
    for sp in (tree_data.get('flowStartingPoints') or []):
        sid = owner.get(sp.get('nodeId'))
        if sid and sid not in starts:
            starts.append(sid)
    if not starts:
        incoming = set(edges.values())
        starts = [s['id'] for s in screens if s['id'] not in incoming] or [screens[0]['id']]
    result, seen = [], set()
    for start in starts:
        cur = start
        while cur in by_id and cur not in seen:
            seen.add(cur)
            result.append(by_id[cur])
            cur = edges.get(cur)
    for s in _reading_order(screens):  # append any screens the flow didn't reach
        if s['id'] not in seen:
            seen.add(s['id'])
            result.append(s)
    return result


def order_screens(screens, tree_data=None):
    """Order screens into a journey: prototype-flow order when the board is wired
    up, else row-major reading order. Never raises — falls back to reading order."""
    if not screens:
        return []
    if tree_data:
        try:
            flow = _flow_order(screens, tree_data)
            if flow:
                return flow
        except Exception:
            pass
    return _reading_order(screens)


class _RenderTimedOut(Exception):
    """/images timed out twice: Figma is stuck on this render."""


def _render_images(file_key, ids_param, pat, limiter, *, scale, fmt, timeout, log_extra):
    """One /images render → {node_id: url}, or None if Figma refused it.

    Figma renders large frames server-side and caches the result, so a cold render
    can exceed one request's timeout while the follow-up hits the warm cache and
    returns fast. A timeout is retried once; a second one raises _RenderTimedOut.
    """
    path = f'/images/{file_key}?ids={ids_param}&scale={scale}&format={fmt}'
    for retry in range(2):
        try:
            data = figma_get_retry(path, pat, timeout=timeout, limiter=limiter)
        except (socket.timeout, TimeoutError):
            if retry == 0:
                logger.info('render timed out — retrying (warm cache)', extra=log_extra)
                continue
            logger.warning('render timed out after retry', extra=log_extra)
            raise _RenderTimedOut() from None
        if not data or data.get('err') or data.get('status') == 400:
            return None
        return data.get('images') or {}
    return None


def fetch_screenshots_batch(file_key, node_ids, pat, limiter=None, *, scale=1, fmt='png'):
    """Render multiple nodes in ONE /images call → {node_id: file_path}.

    /images is Tier-1 rate-limited (as low as 1 rpm), so all ids MUST go in one
    request; the response is an id→url map (values may be null when a node fails
    to render). Each downloaded image is capped at ``_MAX_IMAGE_BYTES``.
    """
    if not node_ids:
        return {}
    tracer = get_tracer()
    with tracer.start_as_current_span('figma.screenshots_batch', attributes={
        'figma.file_key': file_key, 'figma.count': len(node_ids),
    }):
        enc_ids = ','.join(urllib_quote(n) for n in node_ids)
        try:
            images = _render_images(
                file_key, enc_ids, pat, limiter, scale=scale, fmt=fmt, timeout=60,
                log_extra={'file_key': file_key, 'count': len(node_ids)},
            )
        except Exception:
            return {}
        if not images:
            return {}
        out = {}
        for nid in node_ids:
            url = images.get(nid)
            if not url:
                continue
            try:
                path = _download_capped(url, _temp_path('figwatch-screen', nid, fmt))
                if path:
                    out[nid] = path
            except Exception:
                continue
        return out


def fetch_journey_screenshots(file_key, tree_data, pat, limiter=None,
                              *, max_screens=JOURNEY_MAX_SCREENS):
    """Enumerate → order → batch-render a board's child screens.

    Returns an ordered list of {id, name, path} for the screens captured. Scale
    is reduced as the screen count grows to bound total request size/tokens.
    """
    screens = order_screens(enumerate_child_screens(tree_data), tree_data)[:max_screens]
    if not screens:
        return []
    n = len(screens)
    scale, fmt = (1, 'png') if n <= 4 else (0.5, 'jpg') if n <= 8 else (0.25, 'jpg')
    paths = fetch_screenshots_batch(
        file_key, [s['id'] for s in screens], pat, limiter=limiter, scale=scale, fmt=fmt,
    )
    return [
        {'id': s['id'], 'name': s['name'], 'path': paths[s['id']]}
        for s in screens if s['id'] in paths
    ]


# ── Data fetching ─────────────────────────────────────────────────────

def fetch_screenshot(file_key, node_id, pat, limiter=None):
    """Download a Figma node screenshot. Returns file path or None.

    Tries progressively smaller PNG scales then falls back to JPEG. Returns None
    if nothing fits within 3.75 MB (safe ceiling before base64 hits the 5 MB API limit).

    Large multi-screen frames render slowly on a cold cache and can exceed the
    per-request timeout; since Figma renders server-side and caches the result, a
    timeout is retried once (the follow-up usually hits the warm cache). A second
    timeout on the same scale aborts the chain — smaller scales render slower, not
    faster, so they won't beat the clock either (see #37).
    """
    tracer = get_tracer()
    with tracer.start_as_current_span('figma.screenshot', attributes={
        'figma.file_key': file_key,
        'figma.node_id': node_id,
    }):
        enc_id = urllib_quote(node_id)
        attempts = [('png', 1), ('png', 0.5), ('jpg', 1), ('jpg', 0.5), ('jpg', 0.25)]

        for fmt, scale in attempts:
            # If even the warm retry times out, Figma is genuinely stuck and smaller
            # scales (which render slower, not faster) won't help — abort the chain.
            # Non-timeout misses (400 / too big) fall through to the next scale.
            try:
                images = _render_images(
                    file_key, enc_id, pat, limiter, scale=scale, fmt=fmt, timeout=30,
                    log_extra={'file_key': file_key, 'node_id': node_id,
                               'format': fmt, 'scale': scale},
                )
                url = (images or {}).get(node_id)
                if url:
                    path = _download_capped(url, _temp_path('figwatch-screenshot', node_id, fmt))
                    if path:
                        return path
            except _RenderTimedOut:
                return None
            except Exception:
                pass
        return None


def fetch_node_tree(file_key, node_id, pat, limiter=None):
    """Fetch the full node tree for a Figma node.

    Returns (file_path, parsed_data) or (None, None).
    """
    tracer = get_tracer()
    with tracer.start_as_current_span('figma.node_tree', attributes={
        'figma.file_key': file_key,
        'figma.node_id': node_id,
    }):
        enc_id = urllib_quote(node_id)
        try:
            url = f'/files/{file_key}/nodes?ids={enc_id}&depth=100'
            data = figma_get_retry(url, pat, limiter=limiter)
            node = data.get('nodes', {}).get(node_id, {}).get('document') if data else None
            if not node:
                return None, None
            node = _prune_hidden(node)
            out_path = _temp_path('figwatch-tree', node_id, 'json')
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(node, f, indent=2)
            return out_path, node
        except Exception:
            return None, None


def fetch_node_name(file_key, node_id, pat, limiter=None):
    """Resolve just a node's name (depth=1) for the reply header.

    Skills that don't request the full node tree (e.g. a report generator) leave
    tree_data None, which otherwise renders as "Unknown frame". This is a cheap
    single-node lookup — /files/.../nodes is not the Tier-1 /images endpoint —
    and works for any node type, SECTION included. Returns the name or None.
    """
    try:
        enc_id = urllib_quote(node_id)
        data = figma_get_retry(
            f'/files/{file_key}/nodes?ids={enc_id}&depth=1', pat, limiter=limiter,
        )
        doc = (data or {}).get('nodes', {}).get(node_id, {}).get('document')
        return doc.get('name') if doc else None
    except Exception:
        return None


# Data points that are a single REST call each ({fk} = file key, {node} = node id).
_DATA_ENDPOINTS = {
    'dev_resources': '/files/{fk}/dev_resources?node_ids={node}',
    'variables_local': '/files/{fk}/variables/local',
    'variables_published': '/files/{fk}/variables/published',
    'styles': '/files/{fk}/styles',
    'components': '/files/{fk}/components',
    'file_structure': '/files/{fk}?depth=2',
}


def fetch_figma_data(required_data, file_key, node_id, pat, limiter=None):
    """Fetch only the declared data points from Figma API in parallel.

    Returns (dict[data_type -> value], tree_data).
    """
    tracer = get_tracer()
    with tracer.start_as_current_span('figma.fetch_data', attributes={
        'figma.file_key': file_key,
        'figma.node_id': node_id,
    }):
        result = {}
        enc_id = urllib_quote(node_id)
        tree_data = None

        futures = {}
        with TracedThreadPoolExecutor(max_workers=3) as pool:
            if 'screenshot' in required_data:
                futures['screenshot'] = pool.submit(
                    fetch_screenshot, file_key, node_id, pat, limiter=limiter,
                )
            tree_keys = ('node_tree', 'text_nodes', 'annotations', 'prototype_flows')
            needs_tree = any(k in required_data for k in tree_keys)
            if needs_tree:
                futures['_tree'] = pool.submit(
                    fetch_node_tree, file_key, node_id, pat, limiter=limiter,
                )
            for key, endpoint in _DATA_ENDPOINTS.items():
                if key in required_data:
                    futures[key] = pool.submit(
                        figma_get_retry, endpoint.format(fk=file_key, node=enc_id),
                        pat, limiter=limiter,
                    )

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

    def __init__(self, pat: str, limiter: 'FigmaRateLimiter | None' = None):
        self._pat = pat
        self._limiter = limiter

    def _acquire(self, path):
        if self._limiter:
            self._limiter.acquire(path)

    def post_reply(self, file_key: str, parent_comment_id: str, message: str):
        self._acquire(f'/files/{file_key}/comments')
        resp = figma_post(f'/files/{file_key}/comments', {
            'message': message,
            'comment_id': parent_comment_id,
        }, self._pat)
        return resp.get('id')

    def delete_comment(self, file_key: str, comment_id: str) -> None:
        with contextlib.suppress(Exception):
            self._acquire(f'/files/{file_key}/comments')
            figma_delete(f'/files/{file_key}/comments/{comment_id}', self._pat)

    def comment_exists(self, file_key: str, comment_id: str) -> bool:
        comments = self.fetch_comments(file_key)
        for c in comments:
            if c.get('id') == comment_id:
                return True
            for reply in c.get('replies', []):
                if reply.get('id') == comment_id:
                    return True
        return False

    def fetch_comments(self, file_key: str) -> list:
        self._acquire(f'/files/{file_key}/comments')
        data = figma_get(f'/files/{file_key}/comments', self._pat)
        return (data or {}).get('comments', [])


class FigmaDesignDataRepository:
    """DesignDataRepository implementation backed by the Figma REST API."""

    def __init__(self, pat: str, limiter: 'FigmaRateLimiter | None' = None):
        self._pat = pat
        self._limiter = limiter

    def fetch(self, required_data: list, file_key: str, node_id: str) -> tuple:
        return fetch_figma_data(required_data, file_key, node_id, self._pat, limiter=self._limiter)

    def fetch_journey_screenshots(self, file_key: str, tree_data: dict) -> list:
        return fetch_journey_screenshots(file_key, tree_data, self._pat, limiter=self._limiter)

    def fetch_node_name(self, file_key: str, node_id: str) -> 'str | None':
        return fetch_node_name(file_key, node_id, self._pat, limiter=self._limiter)
