// The chat key is not a new credential type — it's an ordinary row in the api_keys module
// (see features/apiKeys/), minted with a naming convention so it's recognizable, rather than a
// parallel auth system built just for chat. create-or-rotate (not pure create): only the key's
// hash is ever stored server-side, so returning a usable raw value on a repeat call necessarily
// re-mints it — look up the caller's own existing non-revoked session key and rotate it if
// found, create if absent/revoked.
//
// Known simplification vs. a from-scratch chat-key design: this reuses the SAME
// apikey.manage-gated create/rotate endpoints every other API key goes through, rather than a
// separate ungated self-provisioning route — so today a user needs apikey.manage to use chat,
// same as they'd need it to manage any other key. A real follow-up would add a dedicated,
// permission-free self-mint endpoint for exactly this narrow case.

import { createApiKey, listApiKeys, rotateApiKey } from '../../api/apiKeys';

const CHAT_KEY_PREFIX = 'session:chat:';

export function chatKeyName(userId: string): string {
  return `${CHAT_KEY_PREFIX}${userId}`;
}

export function isChatKey(name: string): boolean {
  return name.startsWith(CHAT_KEY_PREFIX);
}

/** Returns a fresh raw key value every call (rotating if one already exists). */
export async function ensureChatKey(userId: string): Promise<string> {
  const name = chatKeyName(userId);
  const keys = await listApiKeys();
  const mine = keys.find((k) => k.name === name && k.status !== 'revoked');
  const created = mine ? await rotateApiKey(mine.id) : await createApiKey({ name });
  return created.raw_key;
}
