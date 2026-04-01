import { existsSync, writeFileSync, unlinkSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import { execFile } from 'child_process';
import { promisify } from 'util';
import { loadFile, stripMarkdown } from '../utils.js';

const execFileAsync = promisify(execFile);
const __handlerDir = dirname(fileURLToPath(import.meta.url));

const SKILL_PATH = resolve(__handlerDir, '..', 'skills', 'ux', 'skill.md');
const HEURISTICS_PATH = resolve(__handlerDir, '..', 'skills', 'ux', 'references', 'nielsen-heuristics.md');

// Cache static files
let _skillCache = null;
let _heuristicsCache = null;
function getSkill() { return _skillCache || (_skillCache = loadFile(SKILL_PATH) || ''); }
function getHeuristics() { return _heuristicsCache || (_heuristicsCache = loadFile(HEURISTICS_PATH) || ''); }

// ── Figma REST API helpers ──────────────────────────────────────────

async function figmaApi(path, pat) {
  const res = await fetch(`https://api.figma.com/v1${path}`, {
    headers: { 'X-Figma-Token': pat },
  });
  if (!res.ok) return null;
  return res.json();
}

/**
 * Resolve the commented node to its top-level frame via REST API.
 * Fetches the file at depth=2 to find which page-level frame contains the node.
 */
async function resolveParentFrame(fileKey, nodeId, pat) {
  // First check if the node itself is a top-level frame
  const nodeData = await figmaApi(
    `/files/${fileKey}/nodes?ids=${encodeURIComponent(nodeId)}`, pat
  );
  const node = nodeData?.nodes?.[nodeId]?.document;
  if (!node) return null;

  // If it's already a frame/component at page level, use it
  if (['FRAME', 'COMPONENT', 'COMPONENT_SET'].includes(node.type)) {
    return { id: node.id, name: node.name, type: node.type };
  }

  // Otherwise get the file structure and find the containing top-level frame
  // by looking at which frame's ID is a prefix of our node's ID path
  const fileData = await figmaApi(`/files/${fileKey}?depth=2`, pat);
  if (!fileData?.document?.children) return { id: node.id, name: node.name, type: node.type };

  // Figma REST API includes `containingFrame` in node responses when using certain endpoints
  // Fallback: use the node itself as the target (still evaluates its subtree)
  for (const page of fileData.document.children) {
    for (const frame of page.children || []) {
      if (frame.id === nodeId) {
        return { id: frame.id, name: frame.name, type: frame.type };
      }
    }
  }

  // Use the node itself — the REST API will give us its full subtree for analysis
  return { id: node.id, name: node.name, type: node.type };
}

/**
 * Run a shell command via zsh -l (for figma-ds-cli fallback).
 */
async function shell(cmd, timeout = 30000) {
  return execFileAsync('/bin/zsh', ['-l', '-c', cmd], {
    timeout,
    maxBuffer: 5 * 1024 * 1024,
  });
}

/**
 * Get a screenshot of a frame.
 * 1. Try Figma REST API (fast, no desktop needed) at scale=2, then scale=1
 * 2. Fall back to figma-ds-cli local export (handles any size, needs Figma Desktop)
 */
async function getScreenshot(fileKey, frameId, pat) {
  const outPath = `/tmp/figwatch-screenshot-${frameId.replace(/:/g, '-')}.png`;

  // Attempt 1: REST API (scale 2, then 1)
  for (const scale of [2, 1]) {
    try {
      const data = await figmaApi(
        `/images/${fileKey}?ids=${encodeURIComponent(frameId)}&scale=${scale}&format=png`, pat
      );
      if (data?.err || data?.status === 400) continue;
      const url = data?.images?.[frameId];
      if (!url) continue;

      const imgRes = await fetch(url);
      if (!imgRes.ok) continue;
      const buffer = Buffer.from(await imgRes.arrayBuffer());
      writeFileSync(outPath, buffer);
      return outPath;
    } catch {
      continue;
    }
  }

  // Attempt 2: figma-ds-cli local export (optional dependency, no size limit)
  try {
    const { stdout } = await shell(
      `figma-ds-cli export node "${frameId}" -s 2 -f png -o "${outPath}"`,
      30000
    );
    if (existsSync(outPath)) return outPath;

    // CLI may output the path instead of using -o
    const lines = stdout.trim().split('\n');
    for (const line of lines) {
      const m = line.trim().match(/(?:^|\s)(\/[^\s]+\.png)/);
      if (m && existsSync(m[1])) return m[1];
    }
  } catch {
    // figma-ds-cli not available or failed
  }

  return null;
}

/**
 * Get the full node tree via REST API and save to a temp file.
 */
async function getNodeTree(fileKey, frameId, pat) {
  try {
    const data = await figmaApi(
      `/files/${fileKey}/nodes?ids=${encodeURIComponent(frameId)}&depth=100`, pat
    );
    const node = data?.nodes?.[frameId]?.document;
    if (!node) return null;
    const path = `/tmp/figwatch-tree-${frameId.replace(/:/g, '-')}.json`;
    writeFileSync(path, JSON.stringify(node, null, 2));
    return path;
  } catch {
    return null;
  }
}

// ── Handler ─────────────────────────────────────────────────────────

export async function uxHandler({ nodeId, fileKey, pat, nodeName, extra }) {
  // Phase 1: Resolve parent frame via REST API
  const frame = await resolveParentFrame(fileKey, nodeId, pat);
  if (!frame) {
    return '🗣️ Claude UX Audit\n\n⚠️ Could not locate the commented frame. Check the file key and node ID.\n\n— Claude';
  }

  const frameId = frame.id;
  const screenName = frame.name || 'Unnamed screen';

  // Phase 2 + 3: Get screenshot and node tree in parallel via REST API
  const [screenshotPath, treePath] = await Promise.all([
    getScreenshot(fileKey, frameId, pat),
    getNodeTree(fileKey, frameId, pat),
  ]);

  if (!screenshotPath && !treePath) {
    return '🗣️ Claude UX Audit\n\n⚠️ Could not retrieve design data from Figma API.\n\n— Claude';
  }

  // Phase 4: Build prompt and call Claude
  const skill = getSkill();
  const heuristics = getHeuristics();

  let dataInstructions = '';
  if (screenshotPath) {
    dataInstructions += `\nRead the screenshot image at: ${screenshotPath}`;
  }
  if (treePath) {
    dataInstructions += `\nRead the node tree JSON at: ${treePath}`;
  }
  if (!screenshotPath) {
    dataInstructions += '\n\nNote: Screenshot unavailable. Evaluate using node tree only.';
  }
  if (!treePath) {
    dataInstructions += '\n\nNote: Node tree unavailable. Evaluate using screenshot only.';
  }

  const prompt = `You have a skill for heuristic evaluation. Follow the skill instructions exactly.

${skill}

Here are the detailed heuristic evaluation criteria:
${heuristics}

Now evaluate this screen:
- screenName: ${screenName}
- screenshotPath: ${screenshotPath || 'N/A'}
- treePath: ${treePath || 'N/A'}
${extra ? `- Additional context from reviewer: "${extra}"` : ''}

${dataInstructions}

Read the data sources, evaluate all 10 heuristics, then respond with ONLY the comment reply as specified by the output format. No preamble, no markdown, no explanation — just the formatted reply.`;

  try {
    // Try with tool access first so Claude can read the screenshot image
    const { stdout } = await execFileAsync('claude', [
      '-p', prompt, '--print', '--allowedTools', 'Read,Bash'
    ], { timeout: 120000, maxBuffer: 5 * 1024 * 1024 });

    const reply = stripMarkdown(stdout.trim() || 'Unable to generate evaluation.');
    return `🗣️ Claude UX Audit — ${screenName}\n\n${reply}\n\n— Claude`;
  } catch {
    // Fallback: inline tree data as text (no image analysis)
    try {
      const treeData = treePath ? loadFile(treePath) : null;
      const fallbackPrompt = `${prompt}\n\n${treeData ? `NODE TREE JSON:\n${treeData.slice(0, 50000)}` : ''}`;

      const { stdout } = await execFileAsync('claude', ['--print', '-p', fallbackPrompt], {
        timeout: 120000, maxBuffer: 5 * 1024 * 1024,
      });

      const reply = stripMarkdown(stdout.trim() || 'Unable to generate evaluation.');
      return `🗣️ Claude UX Audit — ${screenName}\n\n${reply}\n\nNote: Visual analysis limited\n\n— Claude`;
    } catch (e) {
      return `🗣️ Claude UX Audit\n\n⚠️ Evaluation failed: ${e.message}\n\n— Claude`;
    }
  } finally {
    // Clean up temp files
    for (const p of [screenshotPath, treePath]) {
      if (p) try { unlinkSync(p); } catch { /* ok */ }
    }
  }
}
