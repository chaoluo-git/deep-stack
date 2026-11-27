const TARGET_ORIGIN = "https://h5.wpk.com";
const MAX_PAYLOAD_CHARS = 1024 * 1024;
const utf8Encoder = new TextEncoder();
export const MAX_PAIRING_BYTES = 4096;

export function isWpkUrl(value) {
  if (typeof value !== "string") return false;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.origin === TARGET_ORIGIN && !url.username && !url.password;
  } catch {
    return false;
  }
}

export function validatePairing(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).length !== 3) return null;
  const {version, url, token} = value;
  const endpoint = typeof url === "string" && /^http:\/\/127\.0\.0\.1:([1-9][0-9]{0,4})\/v1\/(frames|public)$/u.exec(url);
  if (version !== 1 || !endpoint || endpoint[0] !== url || Number(endpoint[1]) > 65535 ||
      typeof token !== "string" || token.length < 32 || token.length > 256 || /[^A-Za-z0-9_-]/u.test(token)) return null;
  return {url, token, mode: endpoint[2]};
}

const DIRECTION = new Map([
  ["Network.webSocketFrameReceived", "received"],
  ["Network.webSocketFrameSent", "sent"],
]);

export function frameMetadata(method, params) {
  const direction = DIRECTION.get(method);
  if (!direction) return null;
  const requestId = params?.requestId;
  const timestamp = params?.timestamp;
  const {opcode, payloadData} = params?.response ?? {};
  if (typeof requestId !== "string" || !requestId || requestId.length > 128 ||
      !Number.isFinite(timestamp) || timestamp < 0 || !Number.isInteger(opcode) || opcode < 0 || opcode > 15 ||
      typeof payloadData !== "string" || payloadData.length > MAX_PAYLOAD_CHARS) return null;

  if (opcode === 1) {
    let encoding = "utf8";
    if (payloadData.length <= 65536 && /^\s*[{[]/u.test(payloadData)) {
      try {
        const document = JSON.parse(payloadData);
        if (document !== null && typeof document === "object") encoding = "json";
      } catch {
        // An opening brace alone does not establish JSON encoding.
      }
    }
    return {requestId, timestamp, direction, opcode, size_bytes: utf8Encoder.encode(payloadData).length, encoding};
  }

  const padding = payloadData.endsWith("==") ? 2 : Number(payloadData.endsWith("="));
  const end = payloadData.length - padding;
  if (payloadData.length % 4 !== 0 || /[^A-Za-z0-9+/]/u.test(payloadData.slice(0, end))) return null;
  const size_bytes = payloadData.length / 4 * 3 - padding;
  if (size_bytes < 0) return null;
  let encoding = opcode >= 8 ? "control" : "binary";
  if (encoding === "binary" && size_bytes >= 10) {
    const header = atob(payloadData.slice(0, 8));
    if (header.startsWith("\x1f\x8b\x08") && (header.charCodeAt(3) & 0xe0) === 0) encoding = "gzip";
  }
  return {requestId, timestamp, direction, opcode, size_bytes, encoding};
}

export function isFrameEvent(method) {
  return DIRECTION.has(method);
}

const PUBLIC_STREETS = new Set([null, "preflop", "flop", "turn", "river", "settled"]);
const PUBLIC_ACTIONS = new Set([null, "fold", "check", "call", "bet", "raise", "all_in"]);
const SNAPSHOT_FIELDS = ["table_ref", "hand_ref", "street", "board", "seats"];
const SEAT_FIELDS = ["slot", "player_id", "action", "wager", "stack"];

function hasFields(value, fields) {
  return value && typeof value === "object" && !Array.isArray(value) &&
    Object.keys(value).length === fields.length && fields.every(field => Object.hasOwn(value, field));
}

function isPublicRef(value) {
  return value === null || (typeof value === "string" && value.length <= 24 &&
    /[1-9]/u.test(value) && !/[^0-9]/u.test(value));
}

function isAmount(value) {
  return value === null || (Number.isFinite(value) && value >= 0 && value <= 1e12);
}

export function publicSnapshot(value, offset_ms) {
  if (!Number.isSafeInteger(offset_ms) || offset_ms < 0 || !hasFields(value, SNAPSHOT_FIELDS)) return null;
  const {table_ref, hand_ref, street, board, seats} = value;
  if (!isPublicRef(table_ref) || !isPublicRef(hand_ref) || !PUBLIC_STREETS.has(street) ||
      !Array.isArray(board) || board.length > 5 || !Array.isArray(seats) || seats.length > 9) return null;

  const cards = [];
  for (const card of board) {
    if (typeof card !== "string" || card.length !== 2 || !/^[2-9TJQKA][cdhs]$/u.test(card) || cards.includes(card)) return null;
    cards.push(card);
  }
  const players = [];
  const slots = new Set();
  for (const seat of seats) {
    if (!hasFields(seat, SEAT_FIELDS)) return null;
    const {slot, player_id, action, wager, stack} = seat;
    if (!Number.isInteger(slot) || slot < 1 || slot > 9 || slots.has(slot) ||
        !isPublicRef(player_id) || !PUBLIC_ACTIONS.has(action) || !isAmount(wager) || !isAmount(stack)) return null;
    slots.add(slot);
    players.push({slot, player_id, action, wager, stack});
  }
  return {offset_ms, table_ref, hand_ref, street, board: cards, seats: players};
}
