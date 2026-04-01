import { toneHandler } from './handlers/tone.js';
import { uxHandler } from './handlers/ux.js';

// Map of trigger keywords to handler functions.
// Each handler receives context from the watcher and returns a string
// to post as a Figma comment reply.
const handlers = new Map([
  ['@tone', { handler: toneHandler, rawMode: false }],
  ['@ux',   { handler: uxHandler,   rawMode: true  }],
  // ['@copy', { handler: copyHandler, rawMode: false }],
]);

/**
 * Parse a comment message and return the matching handler + any extra instructions.
 * Returns null if no trigger is found.
 */
export function matchHandler(message) {
  const lowerMessage = message.toLowerCase().trim();

  for (const [trigger, entry] of handlers) {
    if (lowerMessage.includes(trigger)) {
      const triggerIndex = lowerMessage.indexOf(trigger);
      const extra = message.slice(triggerIndex + trigger.length).trim();
      return { trigger, handler: entry.handler, rawMode: entry.rawMode, extra };
    }
  }

  return null;
}

export function listTriggers() {
  return [...handlers.keys()];
}
