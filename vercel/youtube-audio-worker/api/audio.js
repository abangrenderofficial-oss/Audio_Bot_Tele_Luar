import crypto from "node:crypto";

// External worker deployment marker: keep YouTube extraction off Render egress.
import { once } from "node:events";
import { Innertube, UniversalCache } from "youtubei.js";

const MAX_BODY_BYTES = 256 * 1024;
const MAX_COOKIE_BYTES = 128 * 1024;
const MAX_AUDIO_BYTES = 80 * 1024 * 1024;

let guestTubePromise = null;

function json(res, status, payload) {
  res.statusCode = status;
  res.setHeader("content-type", "application/json; charset=utf-8");
  res.setHeader("cache-control", "no-store");
  res.end(JSON.stringify(payload));
}

function authorized(req) {
  const expected = process.env.YOUTUBE_WORKER_API_KEY || "";
  const auth = String(req.headers.authorization || "");
  const prefix = "Bearer ";
  if (!expected || !auth.startsWith(prefix)) return false;
  const supplied = auth.slice(prefix.length);

  const a = Buffer.from(expected);
  const b = Buffer.from(supplied);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

async function readJsonBody(req) {
  if (req.body && typeof req.body === "object" && !Buffer.isBuffer(req.body)) {
    return req.body;
  }

  let size = 0;
  const chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw new Error("request body too large");
    chunks.push(chunk);
  }

  if (!chunks.length) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function youtubeVideoId(input) {
  if (typeof input !== "string" || !input.trim()) return null;
  const value = input.trim();

  if (/^[A-Za-z0-9_-]{11}$/.test(value)) return value;

  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return null;
  }

  const host = parsed.hostname.toLowerCase().replace(/^www\./, "");
  if (host === "youtu.be") {
    const id = parsed.pathname.split("/").filter(Boolean)[0] || "";
    return /^[A-Za-z0-9_-]{11}$/.test(id) ? id : null;
  }

  if (host === "youtube.com" || host.endsWith(".youtube.com")) {
    if (parsed.pathname === "/watch") {
      const id = parsed.searchParams.get("v") || "";
      return /^[A-Za-z0-9_-]{11}$/.test(id) ? id : null;
    }

    const parts = parsed.pathname.split("/").filter(Boolean);
    if (["shorts", "embed", "live"].includes(parts[0])) {
      const id = parts[1] || "";
      return /^[A-Za-z0-9_-]{11}$/.test(id) ? id : null;
    }
  }

  return null;
}

function netscapeCookiesToHeader(text) {
  if (typeof text !== "string" || !text.trim()) return "";
  if (Buffer.byteLength(text, "utf8") > MAX_COOKIE_BYTES) return "";

  const pairs = [];
  for (let line of text.split(/\r?\n/)) {
    line = line.trim();
    if (!line) continue;

    if (line.startsWith("#HttpOnly_")) {
      line = line.slice("#HttpOnly_".length);
    } else if (line.startsWith("#")) {
      continue;
    }

    const cols = line.split("\t");
    if (cols.length < 7) continue;

    const name = cols[5]?.trim();
    const value = cols.slice(6).join("\t").trim();
    if (!name || /[;=\r\n]/.test(name) || /[\r\n]/.test(value)) continue;
    pairs.push(`${name}=${value}`);
  }

  return pairs.join("; ");
}

async function createTube(cookie = "") {
  return Innertube.create({
    lang: "en",
    location: "US",
    retrieve_player: true,
    enable_session_cache: false,
    cache: new UniversalCache(false),
    ...(cookie ? { cookie } : {}),
  });
}

async function guestTube() {
  if (!guestTubePromise) {
    guestTubePromise = createTube().catch((error) => {
      guestTubePromise = null;
      throw error;
    });
  }
  return guestTubePromise;
}

async function openAudioStream(tube, videoId) {
  const info = await tube.getBasicInfo(videoId);
  const format = info.chooseFormat({
    type: "audio",
    quality: "best",
    format: "any",
  });

  if (!format) throw new Error("no usable audio format");

  const declared = Number(format.content_length || 0);
  if (Number.isFinite(declared) && declared > MAX_AUDIO_BYTES) {
    throw new Error("audio source exceeds size limit");
  }

  const stream = await info.download({
    type: "audio",
    quality: "best",
    format: "any",
  });

  return { stream, declared };
}

async function acquireAudio(videoId, cookieHeader) {
  let guestError;
  try {
    const tube = await guestTube();
    return await openAudioStream(tube, videoId);
  } catch (error) {
    guestError = error;
    guestTubePromise = null;
  }

  if (!cookieHeader) throw guestError;

  const authenticated = await createTube(cookieHeader);
  return openAudioStream(authenticated, videoId);
}

export default async function handler(req, res) {
  if (req.method === "GET") {
    return json(res, 200, {
      ok: true,
      service: "abangrender-youtube-audio-worker",
      engine: "youtubei.js",
    });
  }

  if (req.method !== "POST") {
    res.setHeader("allow", "GET, POST");
    return json(res, 405, { error: "method_not_allowed" });
  }

  if (!authorized(req)) {
    return json(res, 401, { error: "unauthorized" });
  }

  try {
    const body = await readJsonBody(req);
    const videoId = youtubeVideoId(body?.url);
    if (!videoId) return json(res, 400, { error: "invalid_youtube_url" });

    const cookieHeader = netscapeCookiesToHeader(body?.cookies || "");
    const { stream, declared } = await acquireAudio(videoId, cookieHeader);

    res.statusCode = 200;
    res.setHeader("content-type", "application/octet-stream");
    res.setHeader("cache-control", "no-store");
    res.setHeader("x-content-type-options", "nosniff");
    res.setHeader("x-abangrender-worker", "youtubei-vercel");
    if (declared > 0) res.setHeader("content-length", String(declared));

    const reader = stream.getReader();
    let total = 0;

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (!value?.byteLength) continue;

        total += value.byteLength;
        if (total > MAX_AUDIO_BYTES) {
          throw new Error("audio stream exceeded size limit");
        }

        if (!res.write(Buffer.from(value))) {
          await once(res, "drain");
        }
      }
      res.end();
    } finally {
      try {
        reader.releaseLock();
      } catch {
        // no-op
      }
    }
  } catch (error) {
    const message =
      error && typeof error.message === "string"
        ? error.message.slice(0, 600)
        : "youtube audio worker failed";

    console.error("[YOUTUBE-AUDIO-WORKER]", message);

    if (!res.headersSent) {
      return json(res, 502, {
        error: "youtube_audio_failed",
        detail: message,
      });
    }

    res.destroy(error instanceof Error ? error : undefined);
  }
}
