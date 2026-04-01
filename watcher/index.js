import { readFileSync, writeFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import { matchHandler, listTriggers } from './router.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PROCESSED_FILE = resolve(__dirname, '.processed-comments.json');

const FIGMA_API = 'https://api.figma.com/v1';

// --- Processed comment tracking ---
function loadProcessed() {
  try {
    return new Set(JSON.parse(readFileSync(PROCESSED_FILE, 'utf-8')));
  } catch {
    return new Set();
  }
}

function saveProcessed(set) {
  writeFileSync(PROCESSED_FILE, JSON.stringify([...set]), 'utf-8');
}

// --- Figma REST API helpers ---
async function figmaGet(path, pat) {
  const res = await fetch(`${FIGMA_API}${path}`, {
    headers: { 'X-Figma-Token': pat },
  });
  if (!res.ok) throw new Error(`Figma API ${res.status}: ${await res.text()}`);
  return res.json();
}

async function figmaPost(path, body, pat) {
  const res = await fetch(`${FIGMA_API}${path}`, {
    method: 'POST',
    headers: { 'X-Figma-Token': pat, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Figma API ${res.status}: ${await res.text()}`);
  return res.json();
}

async function figmaDelete(path, pat) {
  const res = await fetch(`${FIGMA_API}${path}`, {
    method: 'DELETE',
    headers: { 'X-Figma-Token': pat },
  });
  if (!res.ok) throw new Error(`Figma API ${res.status}: ${await res.text()}`);
}

function extractTextFromNode(node) {
  const texts = [];
  const walk = (n) => {
    // Skip invisible nodes and all their children
    if (n.visible === false) return;

    if (n.type === 'TEXT' && n.characters?.trim()) {
      const box = n.absoluteBoundingBox;
      texts.push({
        name: n.name,
        text: n.characters,
        id: n.id,
        x: box?.x ?? 0,
        y: box?.y ?? 0,
        w: box?.width ?? 0,
        h: box?.height ?? 0,
      });
    }
    if (n.children) n.children.forEach(walk);
  };
  walk(node);
  return texts;
}

/**
 * If the comment is directly on a TEXT node, return just that text.
 * If on a frame, find the text node closest to where the comment pin was placed.
 * Falls back to all text if no position data is available.
 */
function targetTexts(node, allTexts, commentMeta) {
  // Case 1: comment is directly on a text node
  if (node.type === 'TEXT') {
    return { texts: allTexts, targeted: true, targetName: node.name };
  }

  // Case 2: use comment offset to find nearest text
  const offset = commentMeta?.node_offset;
  const nodeBox = node.absoluteBoundingBox;
  if (offset && nodeBox && allTexts.length > 1) {
    // Comment pin position in absolute coordinates
    const pinX = nodeBox.x + (offset.x ?? 0);
    const pinY = nodeBox.y + (offset.y ?? 0);

    // Find the text node whose center is closest to the pin
    let closest = null;
    let closestDist = Infinity;
    for (const t of allTexts) {
      const cx = t.x + t.w / 2;
      const cy = t.y + t.h / 2;
      const dist = Math.sqrt((pinX - cx) ** 2 + (pinY - cy) ** 2);
      if (dist < closestDist) {
        closestDist = dist;
        closest = t;
      }
    }

    // If the closest text is reasonably near (within 200px), target it specifically
    // but also include surrounding context
    if (closest && closestDist < 200) {
      // Include the targeted text plus its immediate neighbours (sorted by distance)
      const sorted = [...allTexts].sort((a, b) => {
        const da = Math.sqrt((pinX - (a.x + a.w / 2)) ** 2 + (pinY - (a.y + a.h / 2)) ** 2);
        const db = Math.sqrt((pinX - (b.x + b.w / 2)) ** 2 + (pinY - (b.y + b.h / 2)) ** 2);
        return da - db;
      });
      // Take up to 5 nearest text nodes for context
      const nearby = sorted.slice(0, Math.min(5, sorted.length));
      return { texts: nearby, targeted: true, targetName: closest.name, primaryText: closest.text };
    }
  }

  // Case 3: fallback to all text
  return { texts: allTexts, targeted: false, targetName: null };
}

// Detect locale from comment text after trigger
function detectLocale(extra, defaultLocale) {
  const locales = ['de', 'fr', 'nl', 'benelux', 'uk'];
  const words = (extra || '').toLowerCase().split(/\s+/);
  for (const word of words) {
    if (locales.includes(word)) return word;
  }
  return defaultLocale;
}

// --- Main poll function ---
async function pollOnce(fileKey, pat, processedIds, { defaultLocale, log }) {
  const data = await figmaGet(`/files/${fileKey}/comments`, pat);
  const comments = data.comments || [];

  // Find top-level trigger comments AND reply triggers (e.g. @tone again in a thread)
  const candidates = comments.filter((c) => {
    if (processedIds.has(c.id)) return false;
    if (c.resolved_at) return false;

    if (!c.parent_id) {
      // Top-level comment: needs a node_id
      return !!c.client_meta?.node_id;
    } else {
      // Reply in a thread: check if it's a new @trigger (not from us)
      // Use the parent comment's node_id for context
      return !c.message?.includes('— Claude');
    }
  });

  // Build lookup map for O(1) parent resolution
  const commentMap = new Map(comments.map(c => [c.id, c]));

  // Check which threads already have our reply (recovers from lost processedIds)
  const repliedTo = new Set();
  for (const c of comments) {
    if (c.parent_id && c.message?.includes('\u2014 Claude')) {
      repliedTo.add(c.parent_id);
      processedIds.add(c.parent_id);
    }
  }

  for (const comment of candidates) {
    if (processedIds.has(comment.id)) continue;
    if (repliedTo.has(comment.id)) continue;
    if (comment.parent_id && repliedTo.has(comment.parent_id)) continue;

    const match = matchHandler(comment.message);
    if (!match) continue;

    const { trigger, handler, rawMode, extra } = match;

    let nodeId = comment.client_meta?.node_id;
    let replyToId = comment.id;
    if (comment.parent_id) {
      const parent = commentMap.get(comment.parent_id);
      nodeId = nodeId || parent?.client_meta?.node_id;
      replyToId = comment.parent_id;
    }

    if (!nodeId) {
      processedIds.add(comment.id);
      continue;
    }

    // Mark processed before handler runs to prevent duplicate processing
    processedIds.add(comment.id);

    log(`💬 ${trigger} comment by ${comment.user.handle} on node ${nodeId}`);

    // Post immediate acknowledgment
    let ackCommentId = null;
    try {
      const ack = await figmaPost(`/files/${fileKey}/comments`, {
        message: `⏳ ${trigger} audit received — Claude is working on it…`,
        comment_id: replyToId,
      }, pat);
      ackCommentId = ack.id;
    } catch {
      // Non-fatal — continue without ack
    }

    try {
      let response;

      if (rawMode) {
        log(`📝 Running ${trigger} audit...`);
        response = await handler({
          nodeId,
          fileKey,
          pat,
          nodeName: `node ${nodeId}`,
          extra,
          commentMessage: comment.message,
        });
      } else {
        const nodeData = await figmaGet(
          `/files/${fileKey}/nodes?ids=${encodeURIComponent(nodeId)}&depth=100`,
          pat
        );
        const node = nodeData.nodes?.[nodeId]?.document;

        if (!node) {
          log(`⚠️  Could not fetch node ${nodeId}, skipping`);
          continue;
        }

        const allTexts = extractTextFromNode(node);
        if (allTexts.length === 0) {
          // Delete ack and post error
          if (ackCommentId) try { await figmaDelete(`/files/${fileKey}/comments/${ackCommentId}`, pat); } catch {}
          await figmaPost(`/files/${fileKey}/comments`, {
            message: 'No text nodes found here. Place the comment on or near a text layer.\n\n— Claude',
            comment_id: replyToId,
          }, pat);
          continue;
        }

        const { texts, targeted, targetName, primaryText } = targetTexts(
          node, allTexts, comment.client_meta
        );

        const targetInfo = targeted
          ? `targeted text "${targetName}" (${texts.length} nearby)`
          : `all ${texts.length} text nodes in frame`;
        log(`📝 ${targetInfo}, running ${trigger} audit...`);

        const locale = detectLocale(extra, defaultLocale);
        response = await handler({
          texts,
          targeted,
          targetName,
          primaryText,
          locale,
          nodeName: node.name || 'Unnamed frame',
          extra,
          commentMessage: comment.message,
        });
      }

      // Delete the acknowledgment, then post the real response
      if (ackCommentId) {
        try { await figmaDelete(`/files/${fileKey}/comments/${ackCommentId}`, pat); } catch {}
      }

      await figmaPost(`/files/${fileKey}/comments`, {
        message: response,
        comment_id: replyToId,
      }, pat);

      log(`✅ Replied to comment ${comment.id}`);
    } catch (err) {
      // Delete ack on error too
      if (ackCommentId) {
        try { await figmaDelete(`/files/${fileKey}/comments/${ackCommentId}`, pat); } catch {}
      }
      log(`❌ Error processing comment ${comment.id}: ${err.message}`);
    }
  }

  // Prune old entries to prevent unbounded growth, then persist
  if (processedIds.size > 500) {
    const arr = [...processedIds];
    const keep = new Set(arr.slice(arr.length - 500));
    processedIds.clear();
    for (const id of keep) processedIds.add(id);
  }
  saveProcessed(processedIds);
}

/**
 * Start watching a Figma file for @trigger comments.
 * @param {string} fileKey - Figma file key
 * @param {string} pat - Figma Personal Access Token
 * @param {object} options
 * @param {number} options.interval - Poll interval in ms (default 30000)
 * @param {string} options.defaultLocale - Default locale (default 'uk')
 * @param {function} options.log - Log function (default console.log)
 */
export async function startWatching(fileKey, pat, options = {}) {
  const {
    interval = 30000,
    defaultLocale = 'uk',
    log = console.log,
  } = options;

  const processedIds = loadProcessed();

  log('');
  log('🔍 Figma Comment Watcher');
  log(`   Triggers: ${listTriggers().join(', ')}`);
  log(`   File: ${fileKey}`);
  log(`   Default locale: ${defaultLocale}`);
  log(`   Poll interval: ${interval / 1000}s`);
  log(`   Previously processed: ${processedIds.size} comments`);
  log('');

  // Run immediately
  try {
    await pollOnce(fileKey, pat, processedIds, { defaultLocale, log });
  } catch (err) {
    log(`⚠️  Poll error: ${err.message}`);
  }

  // Then on interval
  log('⏳ Watching for comments... (Ctrl+C to stop)\n');
  const timer = setInterval(async () => {
    try {
      await pollOnce(fileKey, pat, processedIds, { defaultLocale, log });
    } catch (err) {
      log(`⚠️  Poll error: ${err.message}`);
    }
  }, interval);

  // Return cleanup function
  return () => clearInterval(timer);
}

// --- Keep process alive on unhandled errors ---
process.on('uncaughtException', (err) => {
  console.error(`⚠️  Uncaught exception (continuing): ${err.message}`);
});
process.on('unhandledRejection', (err) => {
  console.error(`⚠️  Unhandled rejection (continuing): ${err?.message || err}`);
});

// --- Standalone / CLI mode ---
// Usage:
//   node index.js watch <fileKey> -l <locale>   (launched by FigWatch app)
//   node index.js                                (uses .env config)
const isMain = process.argv[1] && resolve(process.argv[1]) === resolve(__dirname, 'index.js');
if (isMain) {
  const args = process.argv.slice(2);

  // Parse CLI args: watch <fileKey> [-l <locale>] [-i <interval>]
  if (args[0] === 'watch' && args[1]) {
    // Read PAT from ~/.figwatch/config.json or ~/.figma-ds-cli/config.json
    const { homedir } = await import('os');
    const home = homedir();
    let pat = null;
    for (const configPath of [
      resolve(home, '.figwatch', 'config.json'),
      resolve(home, '.figma-ds-cli', 'config.json'),
    ]) {
      try {
        const config = JSON.parse(readFileSync(configPath, 'utf-8'));
        if (config.figmaPat) { pat = config.figmaPat; break; }
      } catch { /* try next */ }
    }

    if (!pat) { console.error('❌ No Figma PAT found in ~/.figwatch/config.json'); process.exit(1); }

    const fileKey = args[1];
    const localeIdx = args.indexOf('-l');
    const locale = localeIdx >= 0 && args[localeIdx + 1] ? args[localeIdx + 1] : 'uk';
    const intervalIdx = args.indexOf('-i');
    const interval = intervalIdx >= 0 ? parseInt(args[intervalIdx + 1], 10) : 30000;

    await startWatching(fileKey, pat, { interval, defaultLocale: locale });
  } else {
    // .env mode (for development)
    try {
      const { default: dotenv } = await import('dotenv');
      dotenv.config({ path: resolve(__dirname, '.env') });
    } catch { /* dotenv not installed, use process.env */ }

    const pat = process.env.FIGMA_PAT;
    const fileKeys = (process.env.FIGMA_FILE_KEYS || '').split(',').map(k => k.trim()).filter(Boolean);

    if (!pat) { console.error('❌ FIGMA_PAT required'); process.exit(1); }
    if (!fileKeys.length) { console.error('❌ FIGMA_FILE_KEYS required'); process.exit(1); }

    await startWatching(fileKeys[0], pat, {
      interval: parseInt(process.env.POLL_INTERVAL || '30000', 10),
      defaultLocale: process.env.DEFAULT_LOCALE || 'uk',
    });
  }
}
