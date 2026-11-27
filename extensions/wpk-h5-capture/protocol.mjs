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
  const endpoint = typeof url === "string" && /^http:\/\/127\.0\.0\.1:([1-9][0-9]{0,4})\/v1\/frames$/u.exec(url);
  if (version !== 1 || !endpoint || endpoint[0] !== url || Number(endpoint[1]) > 65535 ||
      typeof token !== "string" || token.length < 32 || token.length > 256 || /[^A-Za-z0-9_-]/u.test(token)) return null;
  return {url, token};
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
