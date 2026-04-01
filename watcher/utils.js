import { readFileSync } from 'fs';

export function loadFile(path) {
  try { return readFileSync(path, 'utf-8'); } catch { return null; }
}

export function stripMarkdown(text) {
  return text
    .replace(/\*\*/g, '')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^[-*•]\s+/gm, '')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\n{3,}/g, '\n\n');
}
