import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import { execFile } from 'child_process';
import { promisify } from 'util';
import { loadFile, stripMarkdown } from '../utils.js';

const execFileAsync = promisify(execFile);

const HOME = process.env.HOME || process.env.USERPROFILE;
const __handlerDir = dirname(fileURLToPath(import.meta.url));
const SKILL_PATH = resolve(__handlerDir, '..', 'skills', 'tone', 'skill.md');
const SKILL_PATH_FALLBACK = resolve(HOME, '.claude/skills/tone-reviewer/SKILL.md');
const REFS_DIR = resolve(__handlerDir, '..', 'skills', 'tone', 'references');
const REFS_DIR_FALLBACK = resolve(HOME, '.claude/skills/tone-reviewer/references');

// Cache: skill/reference files are static at runtime
const skillCache = new Map();
function cachedLoad(path, fallback) {
  if (skillCache.has(path)) return skillCache.get(path);
  const content = loadFile(path) || (fallback ? loadFile(fallback) : null);
  if (content) skillCache.set(path, content);
  return content;
}

function loadTovGuide(locale) {
  const fileMap = { de: 'tov-de.md', fr: 'tov-fr.md', nl: 'tov-nl.md', benelux: 'tov-benelux.md' };
  const file = fileMap[locale.toLowerCase()];
  if (!file) return null;
  const key = `tov-${locale}`;
  if (skillCache.has(key)) return skillCache.get(key);
  const content = loadFile(resolve(REFS_DIR, file)) || loadFile(resolve(REFS_DIR_FALLBACK, file));
  if (content) skillCache.set(key, content);
  return content;
}

const UK_GUIDELINES = `Joybuy Tone of Voice — UK (English)
Friendly & approachable, clear & direct, trustworthy, helpful.
GBP (£) before amount, no space. Full stop decimal. "delivery" not "shipping".
Avoid: hype language, exclamation mark overuse (max 1 per screen), ambiguous CTAs.`;

export async function toneHandler({ texts, targeted, targetName, primaryText, locale, nodeName, extra }) {
  const tovGuide = loadTovGuide(locale) || UK_GUIDELINES;
  const skill = cachedLoad(SKILL_PATH, SKILL_PATH_FALLBACK);

  const textList = texts
    .map((t, i) => `${i + 1}. [${t.name}]: "${t.text}"`)
    .join('\n');

  // Build the prompt that invokes Mode 3 of the tone-reviewer skill
  const prompt = `You have a skill called tone-reviewer. Use Mode 3: Comment Reply.

${skill ? `Here is the skill definition:\n${skill}\n` : ''}
Here is the ToV guide for ${locale.toUpperCase()}:
${tovGuide}

Now run Mode 3 with this input:
- locale: ${locale}
- targeted: ${targeted}
- targetName: ${targetName || 'N/A'}
- primaryText: ${primaryText || 'N/A'}
${extra ? `- extra context from reviewer: "${extra}"` : ''}

Text nodes:
${textList}

Respond with ONLY the comment reply. No preamble, no explanation — just the output as specified by Mode 3.`;

  const { stdout } = await execFileAsync('claude', ['--print', '-p', prompt], {
    timeout: 60000,
    maxBuffer: 1024 * 1024,
  });

  let reply = stripMarkdown(stdout.trim() || 'Unable to generate audit.');

  return `🗣️ Claude ToV Audit\n\n${reply}\n\n— Claude`;
}
