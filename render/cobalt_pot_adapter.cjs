"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

const PROVIDER_URL =
  process.env.POT_PROVIDER_URL || "http://127.0.0.1:4416/get_pot";
const HOST = process.env.POT_ADAPTER_HOST || "127.0.0.1";
const PORT = Number(process.env.POT_ADAPTER_PORT || "4417");
const ATTEMPTS = Number(process.env.POT_ADAPTER_ATTEMPTS || "30");
const RETRY_MS = Number(process.env.POT_ADAPTER_RETRY_MS || "500");
const EXTERNAL_PROVIDER_PROXY = String(
  process.env.POT_PROVIDER_PROXY || ""
).trim();

const YOUTUBE_WORKER_API_KEY = String(
  process.env.YOUTUBE_WORKER_API_KEY || ""
).trim();
const YOUTUBE_WORKER_YTDLP_BIN = String(
  process.env.YOUTUBE_WORKER_YTDLP_BIN ||
    path.join(process.cwd(), ".session-bin", "yt-dlp")
).trim();
const YOUTUBE_WORKER_PLUGIN_DIR = String(
  process.env.YOUTUBE_WORKER_PLUGIN_DIR ||
    path.join(process.cwd(), ".bgutil", "plugin")
).trim();
const YOUTUBE_WORKER_PROXY = String(
  process.env.YOUTUBE_WORKER_PROXY || "socks5://127.0.0.1:1080"
).trim();
const YOUTUBE_WORKER_MAX_SOURCE_BYTES =
  Number(process.env.YOUTUBE_WORKER_MAX_SOURCE_BYTES || "") ||
  150 * 1024 * 1024;
let youtubeWorkerTail = Promise.resolve();
let socialWorkerTail = Promise.resolve();

const WARP_ENABLED = /^(1|true|yes|on)$/i.test(
  String(process.env.COBALT_WARP_ENABLED || "")
);
const WARP_HOME = process.env.COBALT_WARP_HOME || "/tmp/cobalt-warp";
const WARP_SOCKS_HOST = "127.0.0.1";
const WARP_SOCKS_PORT = Number(process.env.COBALT_WARP_SOCKS_PORT || "1080");
const WARP_HTTP_HOST = "127.0.0.1";
const WARP_HTTP_PORT = Number(process.env.COBALT_WARP_HTTP_PORT || "3128");
const RUNTIME_ENV_FILE =
  process.env.COBALT_RUNTIME_ENV_FILE ||
  "/opt/render/project/src/render/cobalt_runtime.env";
const WGCF_VERSION = "2.3.0";
const WIREPROXY_VERSION = "1.1.3";

const ARCH = process.arch === "x64" ? "amd64" :
  process.arch === "arm64" ? "arm64" : null;

const CHECKSUMS = {
  amd64: {
    wgcf: "01614e38c0eb5f3405232e71cfaf02d64d4809e4988ad8f5a8071af16d193405",
    wireproxy: "e88c1d090740373fc606c1bafd81d9a5eadc642cce5667616e20e9d7a444f51c",
  },
  arm64: {
    wgcf: "dcadadc42bcc410a4032a6d1c0490ea510e199f0aaaee397dc1aa0fbd27038e8",
    wireproxy: "370e00bd2167960d1ecd1c3c1439715bbaa94a0a110a2040468670c9af6021b6",
  },
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function writeRuntimeProxyEnv(enabled) {
  try {
    const proxyUrl = `http://${WARP_HTTP_HOST}:${WARP_HTTP_PORT}`;
    const lines = ["NO_PROXY=127.0.0.1,localhost"];
    if (enabled) {
      lines.push(`HTTP_PROXY=${proxyUrl}`, `HTTPS_PROXY=${proxyUrl}`);
    }

    fs.mkdirSync(path.dirname(RUNTIME_ENV_FILE), { recursive: true });
    const tempPath = `${RUNTIME_ENV_FILE}.tmp`;
    fs.writeFileSync(tempPath, `${lines.join("\n")}\n`, "utf8");
    fs.renameSync(tempPath, RUNTIME_ENV_FILE);
    console.log(
      `[WARP] Cobalt runtime proxy env ${enabled ? "enabled" : "cleared"}`
    );
  } catch (error) {
    console.error(
      "[WARP] failed to update Cobalt runtime env:",
      error?.message || error
    );
  }
}

function sha256File(filePath) {
  const hash = crypto.createHash("sha256");
  hash.update(fs.readFileSync(filePath));
  return hash.digest("hex");
}

async function downloadFile(url, destination, expectedSha) {
  const response = await fetch(url, { redirect: "follow" });
  if (!response.ok) {
    throw new Error(`download failed ${response.status}: ${url}`);
  }
  const body = Buffer.from(await response.arrayBuffer());
  fs.writeFileSync(destination, body);
  const actual = sha256File(destination);
  if (actual !== expectedSha) {
    throw new Error(`checksum mismatch for ${path.basename(destination)}`);
  }
}

function findNamedFile(root, name) {
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const full = path.join(root, entry.name);
    if (entry.isFile() && entry.name === name) return full;
    if (entry.isDirectory()) {
      const nested = findNamedFile(full, name);
      if (nested) return nested;
    }
  }
  return null;
}

async function waitForPort(host, port, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const ok = await new Promise((resolve) => {
      const socket = net.createConnection({ host, port });
      const done = (value) => {
        socket.destroy();
        resolve(value);
      };
      socket.setTimeout(750);
      socket.once("connect", () => done(true));
      socket.once("timeout", () => done(false));
      socket.once("error", () => done(false));
    });
    if (ok) return true;
    await sleep(250);
  }
  return false;
}

function createSocketReader(socket) {
  let buffered = Buffer.alloc(0);
  let pending = null;

  socket.on("data", (chunk) => {
    buffered = Buffer.concat([buffered, chunk]);
    if (pending) {
      const current = pending;
      pending = null;
      current();
    }
  });

  return async function readExactly(size) {
    while (buffered.length < size) {
      await new Promise((resolve, reject) => {
        pending = resolve;
        socket.once("error", reject);
        socket.once("close", () => reject(new Error("SOCKS socket closed")));
      });
    }
    const out = buffered.subarray(0, size);
    buffered = buffered.subarray(size);
    return out;
  };
}

async function connectThroughSocks(targetHost, targetPort) {
  const socket = net.createConnection({
    host: WARP_SOCKS_HOST,
    port: WARP_SOCKS_PORT,
  });
  await new Promise((resolve, reject) => {
    socket.once("connect", resolve);
    socket.once("error", reject);
  });

  const read = createSocketReader(socket);
  socket.write(Buffer.from([0x05, 0x01, 0x00]));
  const greeting = await read(2);
  if (greeting[0] !== 0x05 || greeting[1] !== 0x00) {
    throw new Error("SOCKS5 authentication negotiation failed");
  }

  const hostBytes = Buffer.from(targetHost, "utf8");
  if (hostBytes.length > 255) throw new Error("target hostname too long");
  const portBytes = Buffer.alloc(2);
  portBytes.writeUInt16BE(targetPort, 0);
  socket.write(Buffer.concat([
    Buffer.from([0x05, 0x01, 0x00, 0x03, hostBytes.length]),
    hostBytes,
    portBytes,
  ]));

  const head = await read(4);
  if (head[0] !== 0x05 || head[1] !== 0x00) {
    throw new Error(`SOCKS5 connect failed: code=${head[1]}`);
  }

  if (head[3] === 0x01) {
    await read(6);
  } else if (head[3] === 0x03) {
    const len = (await read(1))[0];
    await read(len + 2);
  } else if (head[3] === 0x04) {
    await read(18);
  } else {
    throw new Error("SOCKS5 returned unknown address type");
  }
  return socket;
}

function startLocalHttpProxy() {
  const proxy = http.createServer((req, res) => {
    res.writeHead(501, { "content-type": "text/plain" });
    res.end("CONNECT only");
  });

  proxy.on("connect", async (req, client, head) => {
    try {
      const separator = req.url.lastIndexOf(":");
      if (separator <= 0) throw new Error("invalid CONNECT target");
      const host = req.url.slice(0, separator);
      const port = Number(req.url.slice(separator + 1)) || 443;
      const upstream = await connectThroughSocks(host, port);
      client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
      if (head?.length) upstream.write(head);
      upstream.pipe(client);
      client.pipe(upstream);
      const close = () => {
        upstream.destroy();
        client.destroy();
      };
      upstream.once("error", close);
      client.once("error", close);
    } catch (error) {
      try {
        client.write("HTTP/1.1 502 Bad Gateway\r\n\r\n");
      } finally {
        client.destroy();
      }
      console.error("[WARP] CONNECT proxy error:", error?.message || error);
    }
  });

  proxy.listen(WARP_HTTP_PORT, WARP_HTTP_HOST, () => {
    console.log(
      `[WARP] local HTTP proxy ready at http://${WARP_HTTP_HOST}:${WARP_HTTP_PORT}`
    );
  });
  return proxy;
}

async function startWarp() {
  if (!WARP_ENABLED) return null;
  if (!ARCH) {
    console.error(`[WARP] unsupported architecture: ${process.arch}`);
    return null;
  }

  try {
    fs.mkdirSync(WARP_HOME, { recursive: true });
    const wgcf = path.join(WARP_HOME, "wgcf");
    const wireproxy = path.join(WARP_HOME, "wireproxy");
    const wireTar = path.join(WARP_HOME, "wireproxy.tar.gz");
    const extractDir = path.join(WARP_HOME, "wireproxy-extract");

    if (!fs.existsSync(wgcf)) {
      await downloadFile(
        `https://github.com/ViRb3/wgcf/releases/download/v${WGCF_VERSION}/wgcf_${WGCF_VERSION}_linux_${ARCH}`,
        wgcf,
        CHECKSUMS[ARCH].wgcf
      );
      fs.chmodSync(wgcf, 0o755);
    }

    if (!fs.existsSync(wireproxy)) {
      await downloadFile(
        `https://github.com/windtf/wireproxy/releases/download/v${WIREPROXY_VERSION}/wireproxy_linux_${ARCH}.tar.gz`,
        wireTar,
        CHECKSUMS[ARCH].wireproxy
      );
      fs.rmSync(extractDir, { recursive: true, force: true });
      fs.mkdirSync(extractDir, { recursive: true });
      const untar = spawnSync("tar", ["-xzf", wireTar, "-C", extractDir], {
        stdio: "ignore",
      });
      if (untar.status !== 0) throw new Error("wireproxy extraction failed");
      const found = findNamedFile(extractDir, "wireproxy");
      if (!found) throw new Error("wireproxy binary not found after extraction");
      fs.copyFileSync(found, wireproxy);
      fs.chmodSync(wireproxy, 0o755);
    }

    const account = path.join(WARP_HOME, "wgcf-account.toml");
    const profile = path.join(WARP_HOME, "wgcf-profile.conf");

    if (!fs.existsSync(account)) {
      const registered = spawnSync(wgcf, ["register", "--accept-tos"], {
        cwd: WARP_HOME,
        stdio: "ignore",
        timeout: 45000,
      });
      if (registered.status !== 0) throw new Error("WARP registration failed");
    }
    if (!fs.existsSync(profile)) {
      const generated = spawnSync(wgcf, ["generate", "--keepalive=25"], {
        cwd: WARP_HOME,
        stdio: "ignore",
        timeout: 30000,
      });
      if (generated.status !== 0) throw new Error("WARP profile generation failed");
    }

    let profileText = fs.readFileSync(profile, "utf8");
    if (!profileText.includes("[Socks5]")) {
      profileText = profileText.trimEnd() +
        `\n\n[Socks5]\nBindAddress = ${WARP_SOCKS_HOST}:${WARP_SOCKS_PORT}\n`;
      fs.writeFileSync(profile, profileText, { mode: 0o600 });
    }

    const wire = spawn(wireproxy, ["-c", profile, "-s"], {
      cwd: WARP_HOME,
      stdio: "ignore",
    });

    const ready = await waitForPort(WARP_SOCKS_HOST, WARP_SOCKS_PORT, 30000);
    if (!ready) {
      wire.kill("SIGTERM");
      throw new Error("WARP SOCKS5 endpoint did not become ready");
    }

    const curl = spawnSync(
      "curl",
      [
        "-fsS",
        "--max-time",
        "15",
        "--socks5-hostname",
        `${WARP_SOCKS_HOST}:${WARP_SOCKS_PORT}`,
        "https://www.cloudflare.com/cdn-cgi/trace",
      ],
      { encoding: "utf8", timeout: 20000 }
    );
    if (curl.status === 0 && /warp=(on|plus)/i.test(curl.stdout || "")) {
      console.log("[WARP] verified for Cobalt");
    } else {
      console.warn("[WARP] SOCKS5 is ready; external verification was inconclusive");
    }

    const proxy = startLocalHttpProxy();
    const httpReady = await waitForPort(
      WARP_HTTP_HOST,
      WARP_HTTP_PORT,
      10000
    );
    if (!httpReady) {
      proxy.close();
      wire.kill("SIGTERM");
      throw new Error("WARP HTTP proxy did not become ready");
    }

    writeRuntimeProxyEnv(true);
    return { wire, proxy };
  } catch (error) {
    writeRuntimeProxyEnv(false);
    console.error("[WARP] startup failed:", error?.message || error);
    return null;
  }
}

const PROVIDER_ENDPOINT = new URL(PROVIDER_URL);
const PROVIDER_HOST = PROVIDER_ENDPOINT.hostname || "127.0.0.1";
const PROVIDER_PORT = Number(PROVIDER_ENDPOINT.port || "4416");
const PROVIDER_SCRIPT = path.join(
  process.cwd(),
  ".bgutil/server/build/main.js"
);
let managedProvider = null;
let providerStartPromise = null;

function stopProviderProcesses() {
  if (managedProvider && !managedProvider.killed) {
    try {
      managedProvider.kill("SIGTERM");
    } catch {}
    managedProvider = null;
  }

  try {
    for (const entry of fs.readdirSync("/proc", { withFileTypes: true })) {
      if (!entry.isDirectory() || !/^\d+$/.test(entry.name)) continue;
      const pid = Number(entry.name);
      if (!pid || pid === process.pid) continue;

      let args;
      try {
        args = fs
          .readFileSync(`/proc/${entry.name}/cmdline`, "utf8")
          .split("\0")
          .filter(Boolean);
      } catch {
        continue;
      }

      const isProvider = args.some(
        (arg) =>
          arg === ".bgutil/server/build/main.js" ||
          arg.endsWith("/.bgutil/server/build/main.js")
      );
      if (!isProvider) continue;

      try {
        process.kill(pid, "SIGTERM");
      } catch {}
    }
  } catch {}
}

function scheduleProviderStop() {
  // Keep the provider hot. Cobalt refreshes its YouTube trusted session every
  // 300 seconds; killing bgutil after every mint creates a cold-start window
  // where requests can hit youtube.login before the next session is ready.
  // The provider is intentionally kept alive for the lifetime of the service.
}

async function ensureProviderRunning() {
  if (await waitForPort(PROVIDER_HOST, PROVIDER_PORT, 750)) return;

  if (providerStartPromise) {
    await providerStartPromise;
    return;
  }

  providerStartPromise = (async () => {
    if (!fs.existsSync(PROVIDER_SCRIPT)) {
      throw new Error(`POT provider script missing: ${PROVIDER_SCRIPT}`);
    }

    managedProvider = spawn(
      process.execPath,
      [
        PROVIDER_SCRIPT,
        "--host",
        PROVIDER_HOST,
        "--port",
        String(PROVIDER_PORT),
      ],
      {
        cwd: process.cwd(),
        stdio: "ignore",
      }
    );

    managedProvider.once("exit", () => {
      managedProvider = null;
    });

    const ready = await waitForPort(
      PROVIDER_HOST,
      PROVIDER_PORT,
      20000
    );
    if (!ready) {
      stopProviderProcesses();
      throw new Error("POT provider did not become ready");
    }

    console.log("[POT-ADAPTER] bgutil provider started on demand");
  })().finally(() => {
    providerStartPromise = null;
  });

  await providerStartPromise;
}

const WEB_EMBEDDED_CONTEXT = {
  client: {
    clientName: "WEB_EMBEDDED_PLAYER",
    clientVersion: "2.20260708.00.00",
    userAgent:
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.5 Safari/605.1.15,gzip(gfe)",
    hl: "en",
    timeZone: "UTC",
    utcOffsetMinutes: 0,
  },
};

async function requestProvider(body) {
  const response = await fetch(PROVIDER_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const text = await response.text();
  let data;

  try {
    data = JSON.parse(text);
  } catch {
    data = null;
  }

  if (!response.ok || !data?.poToken || !data?.contentBinding) {
    throw new Error(
      data?.error ||
      data?.message ||
      `provider status ${response.status}`
    );
  }

  return data;
}

async function fetchSession() {
  let lastError = "provider unavailable";
  await ensureProviderRunning();

  const proxy = EXTERNAL_PROVIDER_PROXY ||
    (WARP_ENABLED
      ? `socks5h://${WARP_SOCKS_HOST}:${WARP_SOCKS_PORT}`
      : undefined);

  for (let attempt = 1; attempt <= ATTEMPTS; attempt += 1) {
    try {
      const seed = await requestProvider({
        ...(proxy ? { proxy } : {}),
      });

      const embedded = await requestProvider({
        content_binding: seed.contentBinding,
        bypass_cache: true,
        ...(proxy ? { proxy } : {}),
        innertube_context: {
          client: {
            ...WEB_EMBEDDED_CONTEXT.client,
            visitorData: seed.contentBinding,
          },
        },
      });

      console.log(
        `[POT-ADAPTER] minted WEB_EMBEDDED session via ${proxy ? "WARP" : "direct"} egress`
      );
      scheduleProviderStop();
      return JSON.stringify(embedded);
    } catch (error) {
      lastError = error?.message || String(error);
      console.error(
        `[POT-ADAPTER] provider failed: error=${String(lastError).slice(0, 500)}`
      );
    }

    if (attempt < ATTEMPTS) {
      await sleep(RETRY_MS);
    }
  }

  throw new Error(lastError);
}

function readJsonBody(request, maxBytes = 2 * 1024 * 1024) {
  return new Promise((resolve, reject) => {
    let total = 0;
    const chunks = [];
    request.on("data", (chunk) => {
      total += chunk.length;
      if (total > maxBytes) {
        reject(new Error("request body too large"));
        request.destroy();
        return;
      }
      chunks.push(chunk);
    });
    request.on("end", () => {
      try {
        const text = Buffer.concat(chunks).toString("utf8");
        resolve(text ? JSON.parse(text) : {});
      } catch {
        reject(new Error("invalid JSON body"));
      }
    });
    request.on("error", reject);
  });
}

const EXPECTED_TELEGRAM_BOT_ID = 8700444915;
const telegramAuthCache = new Map();

function timingSafeTextEqual(leftText, rightText) {
  const left = Buffer.from(String(leftText || ""));
  const right = Buffer.from(String(rightText || ""));
  return left.length === right.length && crypto.timingSafeEqual(left, right);
}

async function validateTelegramBotToken(token) {
  if (!/^\d+:[A-Za-z0-9_-]+$/.test(token)) return false;

  const digest = crypto.createHash("sha256").update(token).digest("hex");
  const cached = telegramAuthCache.get(digest);
  const now = Date.now();
  if (cached && cached.expiresAt > now) return cached.ok;

  let ok = false;
  try {
    const response = await fetch(
      `https://api.telegram.org/bot${token}/getMe`,
      { signal: AbortSignal.timeout(5000) }
    );
    if (response.ok) {
      const data = await response.json();
      ok = Boolean(
        data?.ok &&
        Number(data?.result?.id) === EXPECTED_TELEGRAM_BOT_ID
      );
    }
  } catch {}

  telegramAuthCache.set(digest, {
    ok,
    expiresAt: now + (ok ? 10 * 60 * 1000 : 30 * 1000),
  });
  return ok;
}

async function workerAuthorized(request) {
  const auth = String(request.headers.authorization || "");
  const prefix = "Bearer ";
  if (!auth.startsWith(prefix)) return false;
  const token = auth.slice(prefix.length).trim();
  if (!token) return false;

  if (
    YOUTUBE_WORKER_API_KEY &&
    timingSafeTextEqual(token, YOUTUBE_WORKER_API_KEY)
  ) {
    return true;
  }

  return validateTelegramBotToken(token);
}

function isAllowedSocialUrl(source, value) {
  try {
    const parsed = new URL(String(value || ""));
    const host = parsed.hostname.toLowerCase().replace(/^www\./, "");
    if (parsed.protocol !== "https:") return false;

    const allowed = {
      tiktok: host === "tiktok.com" || host.endsWith(".tiktok.com"),
      instagram:
        host === "instagram.com" || host.endsWith(".instagram.com"),
      threads:
        host === "threads.net" ||
        host.endsWith(".threads.net") ||
        host === "threads.com" ||
        host.endsWith(".threads.com"),
      twitter:
        host === "x.com" ||
        host.endsWith(".x.com") ||
        host === "twitter.com" ||
        host.endsWith(".twitter.com"),
    };
    return Boolean(allowed[String(source || "").toLowerCase()]);
  } catch {
    return false;
  }
}

function isAllowedYoutubeUrl(value) {
  try {
    const parsed = new URL(String(value || ""));
    const host = parsed.hostname.toLowerCase().replace(/^www\./, "");
    return (
      parsed.protocol === "https:" &&
      (host === "youtube.com" ||
        host.endsWith(".youtube.com") ||
        host === "youtu.be")
    );
  } catch {
    return false;
  }
}

function runYoutubeWorkerProcess(args, timeoutMs = 120000) {
  return new Promise((resolve, reject) => {
    const child = spawn(YOUTUBE_WORKER_YTDLP_BIN, args, {
      cwd: process.cwd(),
      env: {
        ...process.env,
        NO_PROXY: "127.0.0.1,localhost",
        no_proxy: "127.0.0.1,localhost",
      },
      stdio: ["ignore", "ignore", "pipe"],
    });

    let stderr = "";
    let settled = false;
    const finish = (callback) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      callback();
    };
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      finish(() => reject(new Error("YouTube worker timed out")));
    }, timeoutMs);
    timer.unref?.();

    child.stderr.on("data", (chunk) => {
      stderr = (stderr + chunk.toString("utf8")).slice(-12000);
    });
    child.once("error", (error) => {
      finish(() => reject(error));
    });
    child.once("exit", (code, signal) => {
      finish(() => {
        if (code === 0) {
          resolve();
          return;
        }
        const detail = stderr.trim().slice(-1200);
        reject(
          new Error(
            `yt-dlp worker failed code=${code} signal=${signal || ""}` +
              (detail ? `: ${detail}` : "")
          )
        );
      });
    });
  });
}

function findYoutubeWorkerOutput(prefix) {
  const directory = path.dirname(prefix);
  const stem = path.basename(prefix) + ".";
  const matches = fs
    .readdirSync(directory)
    .filter((name) => name.startsWith(stem) && !name.endsWith(".part"))
    .map((name) => path.join(directory, name))
    .filter((name) => {
      try {
        return fs.statSync(name).isFile();
      } catch {
        return false;
      }
    });
  if (!matches.length) {
    throw new Error("YouTube worker produced no audio file");
  }
  matches.sort((a, b) => fs.statSync(b).size - fs.statSync(a).size);
  return matches[0];
}

function runYoutubeWorkerCapture(args, timeoutMs = 60000) {
  return new Promise((resolve, reject) => {
    const child = spawn(YOUTUBE_WORKER_YTDLP_BIN, args, {
      cwd: process.cwd(),
      env: {
        ...process.env,
        NO_PROXY: "127.0.0.1,localhost",
        no_proxy: "127.0.0.1,localhost",
      },
      stdio: ["ignore", "pipe", "pipe"],
    });

    let stdout = "";
    let stderr = "";
    let settled = false;
    const finish = (callback) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      callback();
    };
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      finish(() => reject(new Error("YouTube URL resolver timed out")));
    }, timeoutMs);
    timer.unref?.();

    child.stdout.on("data", (chunk) => {
      stdout = (stdout + chunk.toString("utf8")).slice(-64000);
    });
    child.stderr.on("data", (chunk) => {
      stderr = (stderr + chunk.toString("utf8")).slice(-12000);
    });
    child.once("error", (error) => finish(() => reject(error)));
    child.once("exit", (code, signal) => {
      finish(() => {
        if (code === 0) {
          resolve(stdout);
          return;
        }
        const detail = stderr.trim().slice(-1200);
        reject(
          new Error(
            `yt-dlp URL resolver failed code=${code} signal=${signal || ""}` +
              (detail ? `: ${detail}` : "")
          )
        );
      });
    });
  });
}

async function resolveYoutubeWorkerAudioUrl(videoUrl, cookiesText) {
  if (!fs.existsSync(YOUTUBE_WORKER_YTDLP_BIN)) {
    throw new Error("yt-dlp worker binary is missing");
  }
  if (!fs.existsSync(YOUTUBE_WORKER_PLUGIN_DIR)) {
    throw new Error("bgutil yt-dlp plugin directory is missing");
  }

  const id = crypto.randomUUID();
  const cookiePath = path.join(os.tmpdir(), `youtube-url-${id}.cookies.txt`);
  if (String(cookiesText || "").trim()) {
    fs.writeFileSync(cookiePath, cookiesText, { mode: 0o600 });
  }

  const baseArgs = [
    "--no-playlist",
    "--no-warnings",
    "--quiet",
    "--socket-timeout", "15",
    "--retries", "1",
    "--fragment-retries", "1",
    "--proxy", YOUTUBE_WORKER_PROXY,
    "--plugin-dirs", YOUTUBE_WORKER_PLUGIN_DIR,
    "--js-runtimes", "node",
    "--extractor-args",
    "youtube:player_client=mweb;fetch_pot=always",
    "--extractor-args",
    "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416",
    "--format",
    "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
    "--get-url",
  ];

  const parseUrl = (output) => {
    const candidates = String(output || "")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line.startsWith("https://"));
    if (!candidates.length) throw new Error("yt-dlp returned no direct audio URL");
    const selected = candidates[0];
    const parsed = new URL(selected);
    if (parsed.protocol !== "https:") throw new Error("resolved audio URL is not HTTPS");
    return selected;
  };

  try {
    console.log("[YOUTUBE-WORKER] direct URL resolve start");
    const started = Date.now();
    try {
      const output = await runYoutubeWorkerCapture(
        [...baseArgs, videoUrl],
        60000
      );
      const audioUrl = parseUrl(output);
      console.log(
        `[YOUTUBE-WORKER] direct URL resolved ms=${Date.now() - started}`
      );
      return audioUrl;
    } catch (guestError) {
      const guestMessage = String(guestError?.message || guestError);
      if (
        !fs.existsSync(cookiePath) ||
        guestMessage.includes("Requested format is not available")
      ) {
        throw guestError;
      }
      console.warn(
        "[YOUTUBE-WORKER] guest URL resolve failed:",
        guestMessage.slice(0, 1000)
      );
      const output = await runYoutubeWorkerCapture(
        [...baseArgs, "--cookies", cookiePath, videoUrl],
        60000
      );
      const audioUrl = parseUrl(output);
      console.log(
        `[YOUTUBE-WORKER] cookie URL resolved ms=${Date.now() - started}`
      );
      return audioUrl;
    }
  } finally {
    try { fs.rmSync(cookiePath, { force: true }); } catch {}
  }
}

function socialSourceLabel(source) {
  return {
    tiktok: "TikTok",
    instagram: "Instagram",
    threads: "Threads",
    twitter: "X",
  }[String(source || "").toLowerCase()] || "Social";
}

function instagramShortcode(mediaUrl) {
  try {
    const parsed = new URL(mediaUrl);
    const parts = parsed.pathname.split("/").filter(Boolean);
    const idx = parts.findIndex((part) =>
      ["reel", "reels", "p", "tv"].includes(part)
    );
    return idx >= 0 && parts[idx + 1] ? parts[idx + 1] : "";
  } catch {
    return "";
  }
}

function findInstagramMediaNode(root, shortcode) {
  const stack = [root];
  while (stack.length) {
    const cur = stack.pop();
    if (Array.isArray(cur)) {
      for (const item of cur) stack.push(item);
      continue;
    }
    if (!cur || typeof cur !== "object") continue;

    const code = cur.code || cur.shortcode || "";
    if (
      (!shortcode || code === shortcode) &&
      (
        cur.clips_metadata ||
        cur.music_metadata ||
        cur.clips_music_attribution_info
      )
    ) {
      return cur;
    }
    for (const value of Object.values(cur)) stack.push(value);
  }
  return null;
}

function instagramMusicFromNode(media) {
  if (!media || typeof media !== "object") return null;

  const containers = [
    media.clips_metadata,
    media.music_metadata,
  ].filter((value) => value && typeof value === "object");

  for (const container of containers) {
    const info = container.music_info;
    const asset =
      info && typeof info === "object"
        ? (
            info.music_asset_info &&
            typeof info.music_asset_info === "object"
              ? info.music_asset_info
              : info
          )
        : null;

    if (asset) {
      const title = String(
        asset.title ||
        asset.song_name ||
        ""
      ).trim();
      const artist = String(
        asset.display_artist ||
        asset.artist_name ||
        asset.subtitle ||
        ""
      ).trim();

      if (title && !/^original (audio|sound)$/i.test(title)) {
        return {
          title,
          performer: artist || "Instagram",
          source: "music_asset_info",
        };
      }
    }
  }

  const attribution =
    (
      media.clips_music_attribution_info &&
      typeof media.clips_music_attribution_info === "object"
    )
      ? media.clips_music_attribution_info
      : null;

  if (attribution) {
    const title = String(
      attribution.song_name ||
      attribution.title ||
      ""
    ).trim();
    const artist = String(
      attribution.artist_name ||
      attribution.artist ||
      ""
    ).trim();

    if (title && !/^original (audio|sound)$/i.test(title)) {
      return {
        title,
        performer: artist || "Instagram",
        source: "clips_music_attribution_info",
      };
    }
  }

  for (const container of containers) {
    const original =
      container.original_sound_info &&
      typeof container.original_sound_info === "object"
        ? container.original_sound_info
        : null;
    if (!original) continue;

    const title = String(
      original.original_audio_title ||
      original.audio_title ||
      original.title ||
      ""
    ).trim();

    let performer = "";
    if (
      original.ig_artist &&
      typeof original.ig_artist === "object"
    ) {
      performer = String(
        original.ig_artist.username ||
        original.ig_artist.name ||
        ""
      ).trim();
    }
    performer = performer || String(
      original.artist_name ||
      original.artist ||
      ""
    ).trim();

    if (title && !/^original (audio|sound)$/i.test(title)) {
      return {
        title,
        performer: performer ? `@${performer.replace(/^@+/, "")}` : "Instagram",
        source: "original_sound_info",
      };
    }
  }

  return null;
}

async function fetchInstagramSoundMetadata(mediaUrl) {
  const shortcode = instagramShortcode(mediaUrl);
  if (!shortcode) return null;

  const cleanUrl = `https://www.instagram.com/reel/${shortcode}/`;
  try {
    const response = await fetch(cleanUrl, {
      redirect: "follow",
      headers: {
        "user-agent":
          "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) " +
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 " +
          "Mobile/15E148 Safari/604.1",
        "accept-language": "en-US,en;q=0.9",
        accept: "text/html,application/xhtml+xml",
      },
    });
    if (!response.ok) {
      console.warn(
        `[SOCIAL-WORKER] instagram metadata page HTTP ${response.status}`
      );
      return null;
    }

    const html = await response.text();
    const scriptRe =
      /<script[^>]*type=["']application\/json["'][^>]*>([\s\S]*?)<\/script>/gi;
    let match;
    while ((match = scriptRe.exec(html)) !== null) {
      const raw = match[1];
      if (!raw.includes(shortcode)) continue;
      if (
        !raw.includes("clips_metadata") &&
        !raw.includes("music_metadata") &&
        !raw.includes("clips_music_attribution_info")
      ) {
        continue;
      }

      try {
        const parsed = JSON.parse(raw);
        const node = findInstagramMediaNode(parsed, shortcode);
        const music = instagramMusicFromNode(node);
        if (music) {
          console.log(
            `[SOCIAL-WORKER] instagram sound metadata source=${music.source} title=${music.title} performer=${music.performer}`
          );
          return music;
        }
      } catch {}
    }
  } catch (error) {
    console.warn(
      "[SOCIAL-WORKER] instagram sound metadata failed:",
      String(error?.message || error).slice(0, 700)
    );
  }

  return null;
}

function socialReadMeta(filePath, fallback = "") {
  try {
    const value = fs.readFileSync(filePath, "utf8").trim();
    if (!value || value === "NA" || value === "None") return fallback;
    return value;
  } catch {
    return fallback;
  }
}

function isThreadsShareAlias(value) {
  try {
    const u = new URL(String(value || ""));
    const host = u.hostname.toLowerCase().replace(/^www\./, "");
    return (
      (host === "threads.com" || host === "threads.net") &&
      /^\/share\/[A-Za-z0-9_-]+\/?$/.test(u.pathname)
    );
  } catch {
    return false;
  }
}


async function resolveThreadsShareViaCrawler(mediaUrl) {
  if (!isThreadsShareAlias(mediaUrl)) return mediaUrl;

  const crawlerUa =
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)";

  for (let attempt = 1; attempt <= 5; attempt += 1) {
    try {
      const response = await fetch(mediaUrl, {
        redirect: "follow",
        headers: {
          "user-agent": crawlerUa,
          "accept-language": "en-US,en;q=0.9",
          "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        },
        signal: AbortSignal.timeout(10000),
      });
      const html = await response.text();
      const meta =
        html.match(/<meta[^>]+property=["']og:url["'][^>]+content=["']([^"']+)["']/i) ||
        html.match(/<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:url["']/i);
      const candidate = String(meta?.[1] || response.url || "")
        .replace(/&#0?64;/gi, "@")
        .replace(/&amp;/gi, "&");
      const match = candidate.match(
        /^https:\/\/(?:www\.)?threads\.(?:com|net)\/@[^\s/?#]+\/post\/[A-Za-z0-9_-]+/i
      );
      if (match) {
        console.log(
          "[SOCIAL-WORKER] threads crawler resolved share attempt=" +
          attempt + " -> " + match[0]
        );
        return match[0];
      }
      console.log(
        "[SOCIAL-WORKER] threads crawler miss attempt=" + attempt +
        " status=" + response.status + " bytes=" + html.length
      );
    } catch (error) {
      console.warn(
        "[SOCIAL-WORKER] threads crawler failed attempt=" + attempt + ":",
        String(error?.message || error).slice(0, 300)
      );
    }
    if (attempt < 5) {
      await new Promise((resolve) => setTimeout(resolve, 900));
    }
  }

  return mediaUrl;
}

async function resolveThreadsShareViaJina(mediaUrl) {
  if (!isThreadsShareAlias(mediaUrl)) return mediaUrl;
  const endpoint = "https://r.jina.ai/" + mediaUrl;
  try {
    const response = await fetch(endpoint, {
      redirect: "follow",
      headers: {
        "accept": "text/plain,text/markdown,*/*;q=0.8",
        "user-agent": "AbangRender-MusicBot/1.0",
      },
      signal: AbortSignal.timeout(30000),
    });
    const body = await response.text();
    const normalized = String(body || "")
      .replace(/\\u0026/g, "&")
      .replace(/\\\//g, "/")
      .replace(/&#0?64;/gi, "@")
      .replace(/&amp;/gi, "&");
    const canonical = normalized.match(
      /https:\/\/(?:www\.)?threads\.(?:com|net)\/@[A-Za-z0-9._-]+\/post\/[A-Za-z0-9_-]+/i
    )?.[0];
    if (response.ok && canonical) {
      console.log("[SOCIAL-WORKER] threads Jina resolved share -> " + canonical);
      return canonical;
    }
    console.warn(
      "[SOCIAL-WORKER] threads Jina resolver miss status=" + response.status +
      " bytes=" + body.length +
      " preview=" + normalized.replace(/\s+/g, " ").slice(0, 500)
    );
  } catch (error) {
    console.warn(
      "[SOCIAL-WORKER] threads Jina resolver failed:",
      String(error?.message || error).slice(0, 500)
    );
  }
  return mediaUrl;
}

async function resolveThreadsShareViaTelegramBot(mediaUrl) {
  if (!isThreadsShareAlias(mediaUrl)) return mediaUrl;

  try {
    const response = await fetch(mediaUrl, {
      redirect: "follow",
      headers: {
        "user-agent": "Mozilla/5.0 (compatible; TelegramBot)",
        "accept": "text/html,*/*",
      },
      signal: AbortSignal.timeout(12000),
    });
    const html = await response.text();
    const candidates = [
      response.url,
      ...(html.match(
        /<meta[^>]+property=["']og:url["'][^>]+content=["']([^"']+)["']/i
      ) || []).slice(1),
      ...(html.match(
        /<link[^>]+rel=["']canonical["'][^>]+href=["']([^"']+)["']/i
      ) || []).slice(1),
    ];
    for (const value of candidates) {
      const match = String(value || "").match(
        /https:\/\/(?:www\.)?threads\.(?:com|net)\/@[^\s/?#]+\/post\/[A-Za-z0-9_-]+/i
      );
      if (match) {
        console.log(
          `[SOCIAL-WORKER] threads TelegramBot resolved share -> ${match[0]}`
        );
        return match[0];
      }
    }
    console.warn(
      `[SOCIAL-WORKER] threads TelegramBot resolver miss status=${response.status} final=${response.url}`
    );
  } catch (error) {
    console.warn(
      "[SOCIAL-WORKER] threads TelegramBot resolver failed:",
      String(error?.message || error).slice(0, 500)
    );
  }
  return mediaUrl;
}

async function resolveThreadsShareViaEdgeResolver(mediaUrl) {
  if (!isThreadsShareAlias(mediaUrl)) return mediaUrl;

  const endpoint = "https://resolver.mythic3011.com/v1/threads/resolve";
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "accept": "application/json",
        "origin": "https://share-tools.mythic3011.com",
        "user-agent": "AbangRender-MusicBot/1.0",
      },
      body: JSON.stringify({ url: mediaUrl }),
      signal: AbortSignal.timeout(12000),
    });
    const raw = await response.text();
    let data = null;
    try { data = JSON.parse(raw); } catch {}

    const candidate = String(data?.canonicalUrl || "").trim();
    const match = candidate.match(
      /^https:\/\/(?:www\.)?threads\.(?:com|net)\/@[^\s/?#]+\/post\/[A-Za-z0-9_-]+/i
    );
    if (response.ok && data?.ok && match) {
      console.log(
        "[SOCIAL-WORKER] threads edge resolver success -> " + match[0] +
        " resolution=" + String(data?.resolution || "-")
      );
      return match[0];
    }

    console.warn(
      "[SOCIAL-WORKER] threads edge resolver miss status=" + response.status +
      " body=" + raw.slice(0, 500)
    );
  } catch (error) {
    console.warn(
      "[SOCIAL-WORKER] threads edge resolver failed:",
      String(error?.message || error).slice(0, 500)
    );
  }

  return mediaUrl;
}

async function resolveThreadsShareViaPlainClient(mediaUrl) {
  if (!isThreadsShareAlias(mediaUrl)) return mediaUrl;

  for (const userAgent of ["curl/8.0", "Wget/1.21.4", "python-urllib/3.11"]) {
    try {
      const response = await fetch(mediaUrl, {
        redirect: "manual",
        headers: { "user-agent": userAgent, "accept": "*/*" },
        signal: AbortSignal.timeout(8000),
      });
      const location = String(response.headers.get("location") || "").trim();
      if (location) {
        const absolute = new URL(location, mediaUrl).toString();
        const match = absolute.match(
          /^https:\/\/(?:www\.)?threads\.(?:com|net)\/@[^\s/?#]+\/post\/[A-Za-z0-9_-]+/i
        );
        if (match) {
          console.log(
            "[SOCIAL-WORKER] threads plain-client resolved share ua=" +
            userAgent + " -> " + match[0]
          );
          return match[0];
        }
      }
      console.log(
        "[SOCIAL-WORKER] threads plain-client miss ua=" + userAgent +
        " status=" + response.status +
        " location=" + (location.slice(0, 180) || "-")
      );
    } catch (error) {
      console.warn(
        "[SOCIAL-WORKER] threads plain-client failed ua=" + userAgent + ":",
        String(error?.message || error).slice(0, 300)
      );
    }
  }
  return mediaUrl;
}

async function resolveThreadsShareViaRedirectChecker(mediaUrl) {
  if (!isThreadsShareAlias(mediaUrl)) return mediaUrl;

  const endpoint =
    "https://api.domainee.dev/v1/tools/redirect-checker?url=" +
    encodeURIComponent(mediaUrl);
  try {
    const response = await fetch(endpoint, {
      headers: {
        "accept": "application/json",
        "user-agent": "AbangRender-MusicBot/1.0",
      },
      signal: AbortSignal.timeout(12000),
    });
    const raw = await response.text();
    let data = null;
    try { data = JSON.parse(raw); } catch {}

    const strings = [];
    const walk = (value) => {
      if (typeof value === "string") {
        strings.push(value);
      } else if (Array.isArray(value)) {
        for (const item of value) walk(item);
      } else if (value && typeof value === "object") {
        for (const item of Object.values(value)) walk(item);
      }
    };
    walk(data);

    const canonical = strings.find((value) =>
      /^https:\/\/(?:www\.)?threads\.(?:com|net)\/@[^\s/?#]+\/post\/[A-Za-z0-9_-]+/i.test(value)
    );
    if (response.ok && canonical) {
      const clean = canonical.match(
        /^https:\/\/(?:www\.)?threads\.(?:com|net)\/@[^\s/?#]+\/post\/[A-Za-z0-9_-]+/i
      )?.[0];
      if (clean) {
        console.log(
          `[SOCIAL-WORKER] threads redirect API resolved share -> ${clean}`
        );
        return clean;
      }
    }

    console.warn(
      `[SOCIAL-WORKER] threads redirect API miss status=${response.status} body=${raw.slice(0, 300)}`
    );
  } catch (error) {
    console.warn(
      "[SOCIAL-WORKER] threads redirect API failed:",
      String(error?.message || error).slice(0, 500)
    );
  }

  return mediaUrl;
}

async function resolveThreadsShareViaLinkExpander(mediaUrl) {
  if (!isThreadsShareAlias(mediaUrl)) return mediaUrl;

  const endpoint =
    "https://www.linkexpander.com/?url=" + encodeURIComponent(mediaUrl);
  try {
    const response = await fetch(endpoint, {
      redirect: "follow",
      headers: {
        "accept": "text/plain,*/*;q=0.8",
        "user-agent":
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
      },
      signal: AbortSignal.timeout(15000),
    });
    const raw = String(await response.text()).trim();
    const match = raw.match(
      /https:\/\/(?:www\.)?threads\.(?:com|net)\/@[^\s/?#]+\/post\/[A-Za-z0-9_-]+/i
    );
    if (response.ok && match) {
      console.log(
        `[SOCIAL-WORKER] threads LinkExpander resolved share -> ${match[0]}`
      );
      return match[0];
    }
    console.warn(
      `[SOCIAL-WORKER] threads LinkExpander miss status=${response.status} body=${raw.slice(0, 300)}`
    );
  } catch (error) {
    console.warn(
      "[SOCIAL-WORKER] threads LinkExpander failed:",
      String(error?.message || error).slice(0, 500)
    );
  }
  return mediaUrl;
}

function collectThreadsHybridAssets(data) {
  const candidates = [];
  const seen = new Set();

  const add = (url, kind, score, pathHint = "") => {
    const value = String(url || "").trim().replace(/\\u0026/g, "&").replace(/\\\//g, "/");
    if (!/^https:\/\//i.test(value) || seen.has(value)) return;
    seen.add(value);
    candidates.push({ url: value, kind, score, pathHint });
  };

  const walk = (value, path = []) => {
    if (typeof value === "string") {
      if (!/^https:\/\//i.test(value.trim())) return;
      const hint = path.join(".").toLowerCase();
      const url = value.trim();
      const audioExt = /\.(?:m4a|mp3|aac|ogg|opus|wav)(?:[?#]|$)/i.test(url);
      const videoExt = /\.(?:mp4|m3u8|mov|webm)(?:[?#]|$)/i.test(url);
      const audioHint = /audio|music|sound|song|track/.test(hint);
      const videoHint = /video|play|cover|download|hd|sd/.test(hint);

      if (audioExt || audioHint) add(url, "audio", 900 + (audioExt ? 80 : 0), hint);
      if (videoExt || videoHint) add(url, "video", 1200 + (videoExt ? 120 : 0), hint);
      return;
    }

    if (Array.isArray(value)) {
      for (let i = 0; i < value.length; i += 1) {
        const item = value[i];
        if (item && typeof item === "object" && Number(item.mediaType) === 2) {
          for (const key of ["video_url", "videoUrl", "url", "cover", "src", "download_url", "downloadUrl", "hd", "sd"]) {
            if (typeof item[key] === "string") add(item[key], "video", 1600, `medias.${i}.${key}`);
          }
        }
        walk(item, [...path, String(i)]);
      }
      return;
    }

    if (value && typeof value === "object") {
      for (const [key, item] of Object.entries(value)) {
        walk(item, [...path, key]);
      }
    }
  };

  walk(data);
  return candidates.sort((a, b) => b.score - a.score);
}

function threadsMetadataFromPayload(data, fallbackKind = "video") {
  const strings = [];
  const walk = (value, path = []) => {
    if (typeof value === "string") {
      const text = value.trim();
      if (!text || /^https?:\/\//i.test(text)) return;
      strings.push({ text, path: path.join(".").toLowerCase() });
      return;
    }
    if (Array.isArray(value)) {
      for (let i = 0; i < value.length; i += 1) walk(value[i], [...path, String(i)]);
      return;
    }
    if (value && typeof value === "object") {
      for (const [key, item] of Object.entries(value)) walk(item, [...path, key]);
    }
  };
  walk(data);

  const pick = (rx) => strings.find((item) => rx.test(item.path))?.text || "";
  const title =
    pick(/music.*title|audio.*title|track.*title|song.*title|sound.*title/) ||
    pick(/music.*name|audio.*name|track.*name|song.*name|sound.*name/) ||
    "";
  const performer =
    pick(/artist|music.*author|audio.*author|track.*artist|song.*artist/) ||
    "";
  const username = String(data?.username || "").trim().replace(/^@+/, "");
  const postText = String(data?.text || "").replace(/\s+/g, " ").trim();

  return {
    title:
      title ||
      postText.slice(0, 180) ||
      (fallbackKind === "audio"
        ? (username ? `Threads music — @${username}` : "Threads music")
        : (username ? `Original audio — @${username}` : "Threads audio")),
    performer:
      performer ||
      (username ? `@${username}` : "Threads"),
    duration: null,
  };
}

async function downloadThreadsHybridAsset(
  candidate,
  prefix,
  proxyBase = "",
  { referer = "https://www.threads.com/", extraHeaders = {} } = {}
) {
  const proxyAttempt = proxyBase
    ? {
        name: "proxy",
        url: `${proxyBase}?${new URLSearchParams({ url: candidate.url }).toString()}`,
      }
    : null;
  const attempts = candidate.kind === "video"
    ? [proxyAttempt, { name: "direct", url: candidate.url }].filter(Boolean)
    : [{ name: "direct", url: candidate.url }, proxyAttempt].filter(Boolean);

  let lastError = "no usable asset";
  for (const attempt of attempts) {
    let filePath = "";
    try {
      const response = await fetch(attempt.url, {
        redirect: "follow",
        headers: {
          "user-agent": "Mozilla/5.0",
          "referer": referer,
          "accept": "video/*,audio/*,application/octet-stream;q=0.9,*/*;q=0.1",
          ...Object.fromEntries(
            Object.entries(extraHeaders || {}).filter(
              ([key, value]) =>
                typeof key === "string" &&
                typeof value === "string" &&
                !["host", "content-length", "connection"].includes(key.toLowerCase())
            )
          ),
        },
        signal: AbortSignal.timeout(90000),
      });
      if (!response.ok || !response.body) {
        lastError = `${attempt.name} HTTP ${response.status}`;
        continue;
      }

      const contentType = String(response.headers.get("content-type") || "").toLowerCase();
      if (
        contentType.includes("json") ||
        contentType.includes("html") ||
        contentType.startsWith("text/") ||
        contentType.startsWith("image/")
      ) {
        lastError = `${attempt.name} non-audio media ${contentType || "unknown"}`;
        continue;
      }

      const extension =
        contentType.includes("audio/mpeg") ? "mp3" :
        contentType.includes("audio/mp4") || contentType.includes("audio/x-m4a") ? "m4a" :
        contentType.includes("audio/aac") ? "aac" :
        contentType.includes("video/webm") ? "webm" :
        contentType.includes("video/") ? "mp4" :
        candidate.kind === "audio" ? "m4a" : "mp4";
      filePath = `${prefix}.threads.${candidate.kind}.${extension}`;

      const file = fs.openSync(filePath, "w");
      let total = 0;
      try {
        const reader = response.body.getReader();
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          if (!value?.byteLength) continue;
          total += value.byteLength;
          if (total > YOUTUBE_WORKER_MAX_SOURCE_BYTES) {
            try { await reader.cancel(); } catch {}
            throw new Error("Threads media exceeded safety limit");
          }
          fs.writeSync(file, Buffer.from(value));
        }
      } finally {
        fs.closeSync(file);
      }

      if (total <= 0) {
        try { fs.rmSync(filePath, { force: true }); } catch {}
        lastError = `${attempt.name} empty media`;
        continue;
      }

      const codec = probeAudioCodec(filePath);
      if (!codec) {
        try { fs.rmSync(filePath, { force: true }); } catch {}
        lastError = `${attempt.name} asset has no audio stream`;
        continue;
      }

      return { filePath, bytes: total, codec, route: attempt.name };
    } catch (error) {
      if (filePath) {
        try { fs.rmSync(filePath, { force: true }); } catch {}
      }
      lastError = String(error?.message || error).slice(0, 400);
    }
  }

  throw new Error(lastError);
}


function readHtmlMetaContent(html, key) {
  const wanted = String(key || "").toLowerCase();
  for (const match of String(html || "").matchAll(/<meta\b[^>]*>/gi)) {
    const tag = match[0];
    const name = (
      tag.match(/\b(?:property|name)=["']([^"']+)["']/i) || []
    )[1];
    if (!name || String(name).toLowerCase() !== wanted) continue;
    const content = (tag.match(/\bcontent=["']([^"']*)["']/i) || [])[1];
    if (content) return content;
  }
  return "";
}

function readHtmlCanonical(html) {
  for (const match of String(html || "").matchAll(/<link\b[^>]*>/gi)) {
    const tag = match[0];
    const rel = (tag.match(/\brel=["']([^"']+)["']/i) || [])[1];
    if (!rel || String(rel).toLowerCase() !== "canonical") continue;
    const href = (tag.match(/\bhref=["']([^"']+)["']/i) || [])[1];
    if (href) return href;
  }
  return "";
}

function cleanThreadsEmbedValue(value) {
  return String(value || "")
    .replace(/&amp;/gi, "&")
    .replace(/&#0?64;/gi, "@")
    .replace(/\\u0026/g, "&")
    .replace(/\\\//g, "/")
    .trim();
}

async function fetchFixThreadsMedia(mediaUrl, prefix) {
  const started = Date.now();
  const original = new URL(mediaUrl);
  const pageUrl = "https://fixthreads.seria.moe" + original.pathname;
  const response = await fetch(pageUrl, {
    redirect: "follow",
    headers: {
      "user-agent":
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
      "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
      "accept-language": "en-US,en;q=0.9",
    },
    signal: AbortSignal.timeout(30000),
  });
  const html = await response.text();
  if (!response.ok) {
    throw new Error("FixThreads HTTP " + response.status + ": " + html.slice(0, 300));
  }

  const canonical = cleanThreadsEmbedValue(
    readHtmlCanonical(html) || readHtmlMetaContent(html, "og:url")
  );
  const candidates = [];
  const seen = new Set();
  const add = (value, kind, score) => {
    const url = cleanThreadsEmbedValue(value);
    if (!/^https?:\/\//i.test(url) || seen.has(url)) return;
    seen.add(url);
    candidates.push({ url, kind, score });
  };

  add(readHtmlMetaContent(html, "og:video:secure_url"), "video", 2400);
  add(readHtmlMetaContent(html, "og:video:url"), "video", 2350);
  add(readHtmlMetaContent(html, "og:video"), "video", 2300);
  add(readHtmlMetaContent(html, "twitter:player:stream"), "video", 2250);
  add(readHtmlMetaContent(html, "og:audio:secure_url"), "audio", 2200);
  add(readHtmlMetaContent(html, "og:audio"), "audio", 2150);

  candidates.sort((a, b) => b.score - a.score);
  if (!candidates.length) {
    throw new Error(
      "FixThreads returned no playable media" +
      (canonical ? " canonical=" + canonical : "")
    );
  }

  let lastError = "FixThreads media download failed";
  for (const candidate of candidates.slice(0, 8)) {
    try {
      const downloaded = await downloadThreadsHybridAsset(
        candidate, prefix, "", { referer: pageUrl }
      );
      const title = cleanThreadsEmbedValue(readHtmlMetaContent(html, "og:title"));
      console.log(
        `[SOCIAL-WORKER] threads FixThreads ready kind=${candidate.kind} bytes=${downloaded.bytes} codec=${downloaded.codec} ms=${Date.now() - started}`
      );
      return {
        filePath: downloaded.filePath,
        metadata: {
          title: title || (candidate.kind === "audio" ? "Threads music" : "Threads audio"),
          performer: "Threads",
          duration: null,
        },
        mediaKind: candidate.kind,
      };
    } catch (error) {
      lastError = String(error?.message || error).slice(0, 500);
    }
  }

  throw new Error(lastError);
}

async function fetchFxThreadsPage(mediaUrl) {
  const original = new URL(mediaUrl);
  const pageUrl = "https://fx.akitsuki.me" + original.pathname;
  const response = await fetch(pageUrl, {
    redirect: "follow",
    headers: {
      "user-agent": "Mozilla/5.0 (compatible; TelegramBot)",
      "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    },
    signal: AbortSignal.timeout(20000),
  });
  const html = await response.text();
  if (!response.ok) {
    throw new Error(
      "FxThreads HTTP " + response.status + ": " + html.slice(0, 300)
    );
  }
  return { pageUrl, html, finalUrl: response.url };
}

async function resolveThreadsShareViaFxThreads(mediaUrl) {
  if (!isThreadsShareAlias(mediaUrl)) return mediaUrl;
  try {
    const page = await fetchFxThreadsPage(mediaUrl);
    const candidate = cleanThreadsEmbedValue(
      readHtmlMetaContent(page.html, "og:url") ||
      readHtmlCanonical(page.html)
    );
    if (
      /^https:\/\/(?:www\.)?threads\.(?:com|net)\/@[^\s/?#]+\/post\/[A-Za-z0-9_-]+/i.test(candidate)
    ) {
      console.log(
        "[SOCIAL-WORKER] threads FxThreads resolved share -> " + candidate
      );
      return candidate;
    }
    console.warn(
      "[SOCIAL-WORKER] threads FxThreads resolver returned no canonical post"
    );
  } catch (error) {
    console.warn(
      "[SOCIAL-WORKER] threads FxThreads resolver failed:",
      String(error?.message || error).slice(0, 500)
    );
  }
  return mediaUrl;
}

async function fetchFxThreadsMedia(mediaUrl, prefix) {
  const started = Date.now();
  const page = await fetchFxThreadsPage(mediaUrl);
  const candidates = [];
  const seen = new Set();
  const add = (value, kind, score) => {
    const url = cleanThreadsEmbedValue(value);
    if (!/^https?:\/\//i.test(url) || seen.has(url)) return;
    seen.add(url);
    candidates.push({ url, kind, score });
  };

  add(readHtmlMetaContent(page.html, "og:video:secure_url"), "video", 2200);
  add(readHtmlMetaContent(page.html, "og:video"), "video", 2100);
  add(readHtmlMetaContent(page.html, "twitter:player:stream"), "video", 2000);
  add(readHtmlMetaContent(page.html, "og:audio:secure_url"), "audio", 1950);
  add(readHtmlMetaContent(page.html, "og:audio"), "audio", 1900);

  candidates.sort((a, b) => b.score - a.score);
  if (!candidates.length) {
    const canonical = cleanThreadsEmbedValue(
      readHtmlMetaContent(page.html, "og:url") ||
      readHtmlCanonical(page.html)
    );
    throw new Error(
      "FxThreads returned no playable media" +
      (canonical ? " canonical=" + canonical : "")
    );
  }

  let lastError = "FxThreads media download failed";
  for (const candidate of candidates.slice(0, 8)) {
    try {
      const downloaded = await downloadThreadsHybridAsset(
        candidate,
        prefix,
        "",
        { referer: page.pageUrl }
      );
      const title = cleanThreadsEmbedValue(
        readHtmlMetaContent(page.html, "og:title")
      );
      console.log(
        `[SOCIAL-WORKER] threads FxThreads ready kind=${candidate.kind} bytes=${downloaded.bytes} codec=${downloaded.codec} ms=${Date.now() - started}`
      );
      return {
        filePath: downloaded.filePath,
        metadata: {
          title:
            title ||
            (candidate.kind === "audio" ? "Threads music" : "Threads audio"),
          performer: "Threads",
          duration: null,
        },
        mediaKind: candidate.kind,
      };
    } catch (error) {
      lastError = String(error?.message || error).slice(0, 500);
    }
  }

  throw new Error(lastError);
}

async function fetchEasyDownThreadsMedia(mediaUrl, prefix) {
  const token = String(process.env.EASYDOWN_API_KEY || "").trim();
  if (!token) throw new Error("EASYDOWN_API_KEY not configured");

  const started = Date.now();
  const response = await fetch(
    "https://api.easydown.org/api/v1/platforms/threads/parse",
    {
      method: "POST",
      headers: {
        "authorization": `Bearer ${token}`,
        "content-type": "application/json",
        "accept": "application/json",
        "user-agent": "AbangRender-MusicBot/1.0",
      },
      body: JSON.stringify({ url: mediaUrl }),
      signal: AbortSignal.timeout(45000),
    }
  );

  const raw = await response.text();
  let payload = null;
  try { payload = JSON.parse(raw); } catch {}

  if (!response.ok || !payload || Number(payload?.status || response.status) >= 400) {
    throw new Error(
      `EasyDown failed status=${response.status}: ${String(
        payload?.msg || payload?.message || raw || "invalid response"
      ).slice(0, 700)}`
    );
  }

  const media = payload?.data?.media || payload?.data || {};
  const candidates = [];
  const seen = new Set();

  const add = (item, kind, score) => {
    const url = String(item?.url || item?.src || "").trim();
    if (!/^https?:\/\//i.test(url) || seen.has(url)) return;
    seen.add(url);
    candidates.push({
      url,
      kind,
      score,
      headers:
        item?.headers && typeof item.headers === "object"
          ? item.headers
          : {},
    });
  };

  for (const item of Array.isArray(media?.videos) ? media.videos : []) {
    const hasAudio = item?.hasAudio !== false;
    add(item, "video", hasAudio ? 3000 : 1800);
  }
  for (const item of Array.isArray(media?.audios) ? media.audios : []) {
    add(item, "audio", 2600);
  }

  // Threads can expose music/linked media in platformData rather than in
  // the normalized arrays. Scan only media-looking URL fields as a fallback.
  const scan = (value, path = "") => {
    if (typeof value === "string") {
      if (!/^https?:\/\//i.test(value)) return;
      const hint = path.toLowerCase();
      if (/audio|music|sound|track/.test(hint)) {
        add({ url: value }, "audio", 2200);
      } else if (/video|playback|video_versions|linked_inline_media/.test(hint)) {
        add({ url: value }, "video", 2100);
      }
      return;
    }
    if (Array.isArray(value)) {
      value.forEach((item, index) => scan(item, path + "." + index));
      return;
    }
    if (value && typeof value === "object") {
      for (const [key, item] of Object.entries(value)) {
        scan(item, path ? path + "." + key : key);
      }
    }
  };
  scan(payload?.data?.platformData || {});

  // Hybrid priority:
  // video with embedded audio first; standalone Threads music/audio next.
  candidates.sort((a, b) => b.score - a.score);
  if (!candidates.length) {
    throw new Error("EasyDown returned no video/audio candidates");
  }

  let lastError = "EasyDown returned no usable audio";
  for (const candidate of candidates.slice(0, 20)) {
    try {
      const downloaded = await downloadThreadsHybridAsset(
        candidate,
        prefix,
        "",
        {
          referer: "https://www.threads.com/",
          extraHeaders: candidate.headers,
        }
      );
      console.log(
        `[SOCIAL-WORKER] threads EasyDown ready kind=${candidate.kind} bytes=${downloaded.bytes} codec=${downloaded.codec} ms=${Date.now() - started}`
      );
      return {
        filePath: downloaded.filePath,
        metadata: {
          title: String(media?.title || payload?.data?.platformData?.text || "")
            .replace(/\s+/g, " ")
            .trim()
            .slice(0, 180) ||
            (candidate.kind === "audio" ? "Threads music" : "Threads audio"),
          performer:
            String(
              payload?.data?.platformData?.user?.username ||
              payload?.data?.platformData?.user?.full_name ||
              ""
            ).trim() || "Threads",
          duration:
            Number.isFinite(Number(media?.duration))
              ? Number(media.duration)
              : null,
        },
        mediaKind: candidate.kind,
      };
    } catch (error) {
      lastError = String(error?.message || error).slice(0, 500);
    }
  }

  throw new Error(lastError);
}

async function fetchCurlXThreadsMedia(mediaUrl, prefix) {
  const started = Date.now();
  const response = await fetch("https://www.curl-x.com/api/extract", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "accept": "application/json",
      "user-agent": "AbangRender-MusicBot/1.0",
    },
    body: JSON.stringify({ url: mediaUrl }),
    signal: AbortSignal.timeout(30000),
  });
  const raw = await response.text();
  let data = null;
  try { data = JSON.parse(raw); } catch {}

  if (!response.ok || !data || data?.error) {
    throw new Error(
      "curl-x failed status=" + response.status +
      " code=" + String(data?.code || "-") +
      ": " + String(data?.error || raw || "invalid response").slice(0, 500)
    );
  }

  const candidates = [];
  const seen = new Set();
  const add = (url, kind, score) => {
    const value = String(url || "").trim();
    if (!/^https?:\/\//i.test(value) || seen.has(value)) return;
    seen.add(value);
    candidates.push({ url: value, kind, score });
  };

  for (const item of Array.isArray(data?.media) ? data.media : []) {
    const type = String(item?.type || "").toLowerCase();
    const kind = type === "audio" ? "audio" : "video";
    for (const variant of Array.isArray(item?.variants) ? item.variants : []) {
      const contentType = String(variant?.contentType || "").toLowerCase();
      const variantKind = contentType.startsWith("audio/") ? "audio" : kind;
      const score =
        (variantKind === "video" ? 2000 : 1700) +
        Math.min(Number(variant?.bitrate || 0) / 10000, 500);
      add(variant?.url, variantKind, score);
    }
    add(item?.url, kind, kind === "video" ? 1800 : 1600);
  }

  candidates.sort((a, b) => b.score - a.score);
  let lastError = "curl-x returned no usable audio media";
  for (const candidate of candidates.slice(0, 16)) {
    try {
      const downloaded = await downloadThreadsHybridAsset(
        candidate, prefix, "", { referer: "https://www.threads.com/" }
      );
      console.log(
        `[SOCIAL-WORKER] threads curl-x ready kind=${candidate.kind} bytes=${downloaded.bytes} codec=${downloaded.codec} ms=${Date.now() - started}`
      );
      return {
        filePath: downloaded.filePath,
        metadata: {
          title: String(data?.text || data?.title || "").trim().slice(0, 180) ||
            (candidate.kind === "audio" ? "Threads music" : "Threads audio"),
          performer: String(data?.author || "").trim() || "Threads",
          duration: null,
        },
        mediaKind: candidate.kind,
      };
    } catch (error) {
      lastError = String(error?.message || error).slice(0, 500);
    }
  }

  console.warn(
    "[SOCIAL-WORKER] threads curl-x no media platform=" +
    String(data?.platform || "-") + " media_count=" +
    (Array.isArray(data?.media) ? data.media.length : 0)
  );
  throw new Error(lastError);
}

async function fetchMicrolinkThreadsMedia(mediaUrl, prefix) {
  const started = Date.now();
  const endpoint = "https://api.microlink.io?url=" + encodeURIComponent(mediaUrl);
  const response = await fetch(endpoint, {
    headers: { "accept": "application/json", "user-agent": "AbangRender-MusicBot/1.0" },
    signal: AbortSignal.timeout(30000),
  });
  const raw = await response.text();
  let payload = null;
  try { payload = JSON.parse(raw); } catch {}
  if (!response.ok || payload?.status !== "success") {
    throw new Error(
      "Microlink failed status=" + response.status + ": " +
      String(payload?.message || raw || "invalid response").slice(0, 500)
    );
  }

  const data = payload?.data || {};
  const resolvedUrl = String(data?.url || "").trim();
  const candidates = [];
  const seen = new Set();
  const add = (value, kind, score) => {
    const url = String(value?.url || value || "").trim();
    if (!/^https?:\/\//i.test(url) || seen.has(url)) return;
    seen.add(url);
    candidates.push({ url, kind, score });
  };

  add(data?.video, "video", 2200);
  add(data?.audio, "audio", 2100);

  const scan = (value, path = "") => {
    if (typeof value === "string") {
      if (!/^https?:\/\//i.test(value)) return;
      if (/\.(?:mp4|mov|webm|m3u8)(?:[?#]|$)/i.test(value) || /video|fbcdn|cdninstagram/i.test(path)) {
        add(value, "video", 1500);
      } else if (/\.(?:m4a|mp3|aac|ogg|opus)(?:[?#]|$)/i.test(value) || /audio|music|sound/i.test(path)) {
        add(value, "audio", 1400);
      }
      return;
    }
    if (Array.isArray(value)) {
      value.forEach((item, index) => scan(item, path + "." + index));
      return;
    }
    if (value && typeof value === "object") {
      for (const [key, item] of Object.entries(value)) {
        scan(item, path ? path + "." + key : key);
      }
    }
  };
  scan(data);
  candidates.sort((a, b) => b.score - a.score);

  let lastError = "Microlink returned no usable media";
  for (const candidate of candidates.slice(0, 12)) {
    try {
      const downloaded = await downloadThreadsHybridAsset(
        candidate, prefix, "", { referer: resolvedUrl || mediaUrl }
      );
      console.log(
        `[SOCIAL-WORKER] threads Microlink ready kind=${candidate.kind} bytes=${downloaded.bytes} codec=${downloaded.codec} ms=${Date.now() - started}`
      );
      return {
        filePath: downloaded.filePath,
        metadata: {
          title: String(data?.title || "").trim() ||
            (candidate.kind === "audio" ? "Threads music" : "Threads audio"),
          performer: String(data?.author || "").trim() || "Threads",
          duration: null,
        },
        mediaKind: candidate.kind,
      };
    } catch (error) {
      lastError = String(error?.message || error).slice(0, 500);
    }
  }

  if (
    resolvedUrl &&
    resolvedUrl !== mediaUrl &&
    /https:\/\/(?:www\.)?threads\.(?:com|net)\/@[^/?#]+\/post\/[A-Za-z0-9_-]+/i.test(resolvedUrl)
  ) {
    console.log("[SOCIAL-WORKER] threads Microlink canonical ready -> " + resolvedUrl);
    for (const fallback of [fetchFxThreadsMedia, fetchVxThreadsMedia, fetchThreadsDlMedia, fetchDlpandaThreadsMedia]) {
      try { return await fallback(resolvedUrl, prefix); }
      catch (error) { lastError = String(error?.message || error).slice(0, 500); }
    }
  }

  console.warn(
    "[SOCIAL-WORKER] threads Microlink no media resolved=" +
    (resolvedUrl || "-") + " keys=" + Object.keys(data).slice(0, 30).join(",")
  );
  throw new Error(lastError);
}

async function fetchVxThreadsMedia(mediaUrl, prefix) {
  const started = Date.now();
  const original = new URL(mediaUrl);
  const telegramUa = "Mozilla/5.0 (compatible; TelegramBot)";

  const metaValue = (html, property) => {
    const escaped = property;
    const a = html.match(
      new RegExp(
        '<meta[^>]+(?:property|name)=["\\\']' + escaped +
        '["\\\'][^>]+content=["\\\']([^"\\\']+)["\\\']',
        "i"
      )
    );
    if (a?.[1]) return a[1];
    const b = html.match(
      new RegExp(
        '<meta[^>]+content=["\\\']([^"\\\']+)["\\\'][^>]+(?:property|name)=["\\\']' +
        escaped + '["\\\']',
        "i"
      )
    );
    return b?.[1] || "";
  };

  const clean = (value) =>
    String(value || "")
      .replace(/&amp;/gi, "&")
      .replace(/&#0?64;/gi, "@")
      .replace(/\\u0026/g, "&")
      .replace(/\\\//g, "/")
      .trim();

  const pages = [];
  pages.push("https://vxthreads.com" + original.pathname);

  const loadPage = async (url) => {
    const response = await fetch(url, {
      redirect: "follow",
      headers: {
        "user-agent": telegramUa,
        "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
      },
      signal: AbortSignal.timeout(12000),
    });
    return { response, html: await response.text() };
  };

  let first = null;
  try {
    first = await loadPage(pages[0]);
  } catch (error) {
    throw new Error(
      "vxThreads share fetch failed: " +
      String(error?.message || error).slice(0, 300)
    );
  }

  const canonicalRaw =
    metaValue(first.html, "og:url") ||
    (first.html.match(
      /<link[^>]+rel=["']canonical["'][^>]+href=["']([^"']+)["']/i
    ) || [,""])[1] ||
    first.response.url;
  const canonical = clean(canonicalRaw);

  try {
    const cu = new URL(canonical);
    if (
      /(^|\.)threads\.(?:com|net)$/i.test(cu.hostname) &&
      /^\/@[^/]+\/post\/[A-Za-z0-9_-]+\/?$/i.test(cu.pathname)
    ) {
      const canonicalMirror = "https://vxthreads.com" + cu.pathname;
      if (!pages.includes(canonicalMirror)) pages.push(canonicalMirror);
    } else if (
      /(^|\.)vxthreads\.com$/i.test(cu.hostname) &&
      /^\/@[^/]+\/post\/[A-Za-z0-9_-]+\/?$/i.test(cu.pathname)
    ) {
      const canonicalMirror = "https://vxthreads.com" + cu.pathname;
      if (!pages.includes(canonicalMirror)) pages.push(canonicalMirror);
    }
  } catch {}

  const loaded = [{ url: pages[0], ...first }];
  if (pages.length > 1) {
    try {
      loaded.push({ url: pages[1], ...(await loadPage(pages[1])) });
    } catch {}
  }

  let lastError = "vxThreads returned no media";
  for (const page of loaded) {
    const html = page.html;
    const candidates = [];
    const seen = new Set();
    const add = (value, kind, score) => {
      const url = clean(value);
      if (!/^https?:\/\//i.test(url) || seen.has(url)) return;
      seen.add(url);
      candidates.push({ url, kind, score });
    };

    for (const key of [
      "og:video:secure_url",
      "og:video",
      "twitter:player:stream",
    ]) {
      add(metaValue(html, key), "video", 2000);
    }
    for (const key of ["og:audio:secure_url", "og:audio"]) {
      add(metaValue(html, key), "audio", 1900);
    }
    for (const match of html.matchAll(/https?:[^\s"'<>]+/gi)) {
      const raw = clean(match[0]).replace(/[),.;}]+$/g, "");
      if (/\.mp4(?:[?#]|$)|cdninstagram|fbcdn/i.test(raw)) {
        add(raw, "video", 1500);
      } else if (/\.(?:m4a|mp3|aac|ogg|opus)(?:[?#]|$)/i.test(raw)) {
        add(raw, "audio", 1400);
      }
    }

    candidates.sort((a, b) => b.score - a.score);
    for (const candidate of candidates.slice(0, 10)) {
      try {
        const downloaded = await downloadThreadsHybridAsset(
          candidate,
          prefix,
          "",
          { referer: page.url }
        );
        const title = clean(metaValue(html, "og:title"));
        console.log(
          `[SOCIAL-WORKER] threads vxThreads ready kind=${candidate.kind} bytes=${downloaded.bytes} codec=${downloaded.codec} ms=${Date.now() - started}`
        );
        return {
          filePath: downloaded.filePath,
          metadata: {
            title: title || (candidate.kind === "audio" ? "Threads music" : "Threads audio"),
            performer: "Threads",
            duration: null,
          },
          mediaKind: candidate.kind,
        };
      } catch (error) {
        lastError = String(error?.message || error).slice(0, 400);
      }
    }
  }

  throw new Error(lastError);
}

async function fetchThreadsDlMedia(mediaUrl, prefix) {
  const apiUrl = "https://www.threadsdl.app/api/threads";
  const proxyBase = "https://www.threadsdl.app/api/proxy";
  const started = Date.now();

  const apiResponse = await fetch(apiUrl, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "accept": "application/json",
      "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    },
    body: JSON.stringify({ url: mediaUrl }),
    signal: AbortSignal.timeout(20000),
  });

  const raw = await apiResponse.text();
  let data = null;
  try { data = JSON.parse(raw); } catch {}

  if (!apiResponse.ok || !data) {
    throw new Error(
      `ThreadsDL API failed status=${apiResponse.status}: ${String(raw || "invalid response").slice(0, 500)}`
    );
  }

  const candidates = collectThreadsHybridAssets(data);
  if (!candidates.length) {
    throw new Error("ThreadsDL returned no video or standalone audio asset");
  }

  // Hybrid rule:
  // 1) Prefer a video that already contains audio.
  // 2) If there is no usable video audio, use the standalone Threads
  //    music/audio asset (important for image + music posts).
  const ordered = [
    ...candidates.filter((item) => item.kind === "video"),
    ...candidates.filter((item) => item.kind === "audio"),
  ];

  let lastError = "no usable Threads audio";
  for (const candidate of ordered.slice(0, 16)) {
    try {
      const downloaded = await downloadThreadsHybridAsset(candidate, prefix, proxyBase);
      const metadata = threadsMetadataFromPayload(data, candidate.kind);
      console.log(
        `[SOCIAL-WORKER] threads hybrid ready kind=${candidate.kind} bytes=${downloaded.bytes} codec=${downloaded.codec} route=${downloaded.route} ms=${Date.now() - started}`
      );
      return {
        filePath: downloaded.filePath,
        metadata,
        mediaKind: candidate.kind,
      };
    } catch (error) {
      lastError = String(error?.message || error).slice(0, 500);
    }
  }

  throw new Error(`ThreadsDL hybrid extraction failed: ${lastError}`);
}

function parsePostCopilotMcpPayload(raw) {
  const values = [];
  for (const line of String(raw || "").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("event:")) continue;
    const payload = trimmed.startsWith("data:")
      ? trimmed.slice(5).trim()
      : trimmed;
    if (!payload || payload === "[DONE]") continue;
    try { values.push(JSON.parse(payload)); } catch {}
  }
  if (!values.length) {
    try { values.push(JSON.parse(String(raw || ""))); } catch {}
  }
  return values.length ? values[values.length - 1] : null;
}

function collectPostCopilotUrls(value, output = []) {
  if (typeof value === "string") {
    const matches = value.match(/https?:\/\/[^\s"'<>\\]+/g) || [];
    for (let item of matches) {
      item = item.replace(/[),.;\]}]+$/g, "");
      try { item = item.replace(/\\u0026/g, "&").replace(/\\\//g, "/"); } catch {}
      if (!output.includes(item)) output.push(item);
    }
    const trimmed = value.trim();
    if ((trimmed.startsWith("{") || trimmed.startsWith("[")) && trimmed.length < 200000) {
      try { collectPostCopilotUrls(JSON.parse(trimmed), output); } catch {}
    }
    return output;
  }
  if (Array.isArray(value)) {
    for (const item of value) collectPostCopilotUrls(item, output);
    return output;
  }
  if (value && typeof value === "object") {
    for (const item of Object.values(value)) collectPostCopilotUrls(item, output);
  }
  return output;
}

async function fetchPostCopilotThreadsMedia(mediaUrl, prefix) {
  const endpoint = "https://postcopilot.ai/mcp";
  const protocolVersion = "2025-06-18";
  const commonHeaders = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
    "mcp-protocol-version": protocolVersion,
    "user-agent": "AbangRender-MusicBot/1.0",
  };
  const started = Date.now();

  const initResponse = await fetch(endpoint, {
    method: "POST",
    headers: commonHeaders,
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: "init",
      method: "initialize",
      params: {
        protocolVersion,
        capabilities: {},
        clientInfo: { name: "AbangRender-MusicBot", version: "1.0" },
      },
    }),
    signal: AbortSignal.timeout(15000),
  });
  const initRaw = await initResponse.text();
  const initPayload = parsePostCopilotMcpPayload(initRaw);
  if (!initResponse.ok || initPayload?.error) {
    throw new Error(
      `PostCopilot MCP initialize failed status=${initResponse.status}: ${String(
        initPayload?.error?.message || initRaw || "invalid response"
      ).slice(0, 400)}`
    );
  }

  const sessionId = String(initResponse.headers.get("mcp-session-id") || "").trim();
  const headers = {
    ...commonHeaders,
    ...(sessionId ? { "mcp-session-id": sessionId } : {}),
  };

  try {
    await fetch(endpoint, {
      method: "POST",
      headers,
      body: JSON.stringify({
        jsonrpc: "2.0",
        method: "notifications/initialized",
        params: {},
      }),
      signal: AbortSignal.timeout(5000),
    });
  } catch {}

  let toolName = "postcopilot_download_video";
  try {
    const listResponse = await fetch(endpoint, {
      method: "POST",
      headers,
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: "threads-tools",
        method: "tools/list",
        params: {},
      }),
      signal: AbortSignal.timeout(15000),
    });
    const listRaw = await listResponse.text();
    const listPayload = parsePostCopilotMcpPayload(listRaw);
    const tools = Array.isArray(listPayload?.result?.tools)
      ? listPayload.result.tools
      : [];
    const names = tools
      .map((tool) => String(tool?.name || "").trim())
      .filter(Boolean);
    if (names.includes("download_video")) {
      toolName = "download_video";
    } else if (names.includes("postcopilot_download_video")) {
      toolName = "postcopilot_download_video";
    }
    console.log(
      `[SOCIAL-WORKER] PostCopilot tools=${names.join(",").slice(0,500)} selected=${toolName}`
    );
  } catch (error) {
    console.warn(
      "[SOCIAL-WORKER] PostCopilot tools/list failed:",
      String(error?.message || error).slice(0, 400)
    );
  }

  const callResponse = await fetch(endpoint, {
    method: "POST",
    headers,
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: "threads-download",
      method: "tools/call",
      params: {
        name: toolName,
        arguments: { url: mediaUrl },
      },
    }),
    signal: AbortSignal.timeout(90000),
  });
  const callRaw = await callResponse.text();
  const callPayload = parsePostCopilotMcpPayload(callRaw);
  if (!callResponse.ok || callPayload?.error) {
    throw new Error(
      `PostCopilot MCP call failed status=${callResponse.status}: ${String(
        callPayload?.error?.message || callRaw || "invalid response"
      ).slice(0, 500)}`
    );
  }

  const urls = collectPostCopilotUrls(callPayload)
    .filter((value) => {
      try {
        const parsed = new URL(value);
        return parsed.protocol === "https:" &&
          !/(^|\.)threads\.(com|net)$/i.test(parsed.hostname) &&
          parsed.hostname !== "postcopilot.ai";
      } catch {
        return false;
      }
    })
    .sort((a, b) => {
      const score = (value) =>
        (/\.mp4(?:[?#]|$)/i.test(value) ? 8 : 0) +
        (/video|fbcdn|cdninstagram/i.test(value) ? 4 : 0);
      return score(b) - score(a);
    });

  if (!urls.length) {
    const preview = JSON.stringify(callPayload || {}).slice(0, 900);
    throw new Error(`PostCopilot returned no media URL: ${preview}`);
  }

  let lastError = "no usable media URL";
  for (const candidate of urls.slice(0, 8)) {
    let filePath = null;
    try {
      const mediaResponse = await fetch(candidate, {
        redirect: "follow",
        headers: {
          "user-agent": "Mozilla/5.0",
          "referer": "https://www.threads.com/",
          "accept": "video/*,audio/*,application/octet-stream;q=0.9,*/*;q=0.1",
        },
        signal: AbortSignal.timeout(90000),
      });
      if (!mediaResponse.ok || !mediaResponse.body) {
        lastError = `HTTP ${mediaResponse.status}`;
        continue;
      }
      const contentType = String(
        mediaResponse.headers.get("content-type") || ""
      ).toLowerCase();
      if (
        contentType.includes("json") ||
        contentType.includes("html") ||
        contentType.startsWith("text/")
      ) {
        lastError = `non-media ${contentType || "unknown"}`;
        continue;
      }

      filePath = `${prefix}.postcopilot.mp4`;
      const file = fs.openSync(filePath, "w");
      let total = 0;
      try {
        const reader = mediaResponse.body.getReader();
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          if (!value?.byteLength) continue;
          total += value.byteLength;
          if (total > YOUTUBE_WORKER_MAX_SOURCE_BYTES) {
            try { await reader.cancel(); } catch {}
            throw new Error("PostCopilot media exceeded safety limit");
          }
          fs.writeSync(file, Buffer.from(value));
        }
      } finally {
        fs.closeSync(file);
      }
      if (total <= 0) {
        try { fs.rmSync(filePath, { force: true }); } catch {}
        lastError = "empty media";
        continue;
      }

      console.log(
        `[SOCIAL-WORKER] threads PostCopilot ready bytes=${total} ms=${Date.now() - started}`
      );
      return {
        filePath,
        metadata: {
          title: "Threads audio",
          performer: "Threads",
          duration: null,
        },
      };
    } catch (error) {
      if (filePath) {
        try { fs.rmSync(filePath, { force: true }); } catch {}
      }
      lastError = String(error?.message || error).slice(0, 300);
    }
  }

  throw new Error(`PostCopilot media download failed: ${lastError}`);
}


function decodeHtmlAttr(value) {
  return String(value || "")
    .replace(/&amp;/gi, "&")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'")
    .replace(/&#x2F;/gi, "/")
    .replace(/&#47;/g, "/")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">");
}

function extractDlpandaDownloadCandidates(html) {
  const candidates = [];
  const seen = new Set();
  const patterns = [
    ["data-download-url", /data-download-url=["']([^"']+)["']/gi],
    ["data-bridge-url", /data-bridge-url=["']([^"']+)["']/gi],
    ["data-worker-url", /data-worker-url=["']([^"']+)["']/gi],
  ];

  for (const [attr, rx] of patterns) {
    for (const match of html.matchAll(rx)) {
      const url = decodeHtmlAttr(match[1]);
      if (!/^https?:\/\//i.test(url) || seen.has(url)) continue;
      seen.add(url);
      const contextStart = Math.max(0, (match.index || 0) - 500);
      const contextEnd = Math.min(
        html.length,
        (match.index || 0) + match[0].length + 500
      );
      const context = html.slice(contextStart, contextEnd).toLowerCase();
      const kind =
        /audio|music|sound|song|track/.test(context) ||
        /\.(?:m4a|mp3|aac|ogg|opus|wav)(?:[?#]|$)/i.test(url)
          ? "audio"
          : "video";
      candidates.push({
        url,
        kind,
        score:
          attr === "data-download-url"
            ? 1800
            : attr === "data-bridge-url"
              ? 1700
              : 1600,
        pathHint: "dlpanda." + attr,
      });
    }
  }

  for (const match of html.matchAll(/https?:[^\s"'<>]+/gi)) {
    const url = decodeHtmlAttr(match[0]).replace(/[),.;\]}]+$/g, "");
    if (seen.has(url)) continue;
    const lower = url.toLowerCase();
    const mediaish = [
      ".mp4", ".mov", ".webm", ".m4a", ".mp3", ".aac",
      "video", "audio", "media", "download", "bridge", "proxy",
    ].some((needle) => lower.includes(needle));
    if (!mediaish) continue;

    seen.add(url);
    const audioish = [
      ".m4a", ".mp3", ".aac", "audio", "music", "sound",
    ].some((needle) => lower.includes(needle));
    candidates.push({
      url,
      kind: audioish ? "audio" : "video",
      score: 1200,
      pathHint: "dlpanda.raw-url",
    });
  }

  return candidates.sort((a, b) => b.score - a.score);
}

async function fetchDlpandaThreadsMedia(mediaUrl, prefix) {
  const pageUrl = "https://dlpanda.com/threads";
  const started = Date.now();
  const browserHeaders = {
    "user-agent":
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
  };

  const pageResponse = await fetch(pageUrl, {
    redirect: "follow",
    headers: browserHeaders,
    signal: AbortSignal.timeout(15000),
  });
  const pageHtml = await pageResponse.text();
  if (!pageResponse.ok) {
    throw new Error(`DLPanda page HTTP ${pageResponse.status}`);
  }

  const marker = pageHtml.indexOf("data-download-form");
  const formStart = marker >= 0 ? pageHtml.lastIndexOf("<form", marker) : -1;
  const formEnd = formStart >= 0 ? pageHtml.indexOf("</form>", formStart) : -1;
  if (formStart < 0 || formEnd < 0) {
    throw new Error("DLPanda Threads form not found");
  }
  const formHtml = pageHtml.slice(formStart, formEnd + 7);
  const tokenMatch =
    formHtml.match(/<input[^>]*name=["']_token["'][^>]*value=["']([^"']+)["']/i) ||
    formHtml.match(/<input[^>]*value=["']([^"']+)["'][^>]*name=["']_token["']/i);
  const csrfToken = String(tokenMatch?.[1] || "").trim();
  if (!csrfToken) {
    throw new Error("DLPanda CSRF token missing");
  }

  let cookieHeader = "";
  try {
    const values = typeof pageResponse.headers.getSetCookie === "function"
      ? pageResponse.headers.getSetCookie()
      : [pageResponse.headers.get("set-cookie")].filter(Boolean);
    cookieHeader = values
      .map((value) => String(value).split(";")[0])
      .filter(Boolean)
      .join("; ");
  } catch {}

  const body = new FormData();
  body.set("_token", csrfToken);
  body.set("url", mediaUrl);

  const postHeaders = {
    ...browserHeaders,
    "accept": "text/html",
    "x-requested-with": "XMLHttpRequest",
    "referer": pageUrl,
  };
  if (cookieHeader) postHeaders.cookie = cookieHeader;

  const parseResponse = await fetch(pageUrl, {
    method: "POST",
    redirect: "follow",
    headers: postHeaders,
    body,
    signal: AbortSignal.timeout(25000),
  });
  const resultHtml = await parseResponse.text();
  if (!parseResponse.ok) {
    throw new Error(
      `DLPanda parse HTTP ${parseResponse.status}: ${resultHtml.replace(/\\s+/g, " ").slice(0, 400)}`
    );
  }

  const candidates = extractDlpandaDownloadCandidates(resultHtml);
  if (!candidates.length) {
    const stateTag =
      (resultHtml.match(/<[^>]+data-download-state[^>]*>/i) || [""])[0];
    const state =
      (stateTag.match(/data-state=["']([^"']+)["']/i) || [,""])[1];

    console.warn(
      `[SOCIAL-WORKER] DLPanda parse no media status=${parseResponse.status} type=${parseResponse.headers.get("content-type") || "-"} bytes=${resultHtml.length} state=${state || "unknown"}`
    );
    throw new Error(
      `DLPanda returned no downloadable media state=${state || "unknown"}`
    );
  }

  let lastError = "no DLPanda candidate had audio";
  for (const candidate of candidates.slice(0, 12)) {
    try {
      const downloaded = await downloadThreadsHybridAsset(
        candidate,
        prefix,
        "",
        { referer: pageUrl }
      );
      console.log(
        `[SOCIAL-WORKER] threads DLPanda ready kind=${candidate.kind} bytes=${downloaded.bytes} codec=${downloaded.codec} ms=${Date.now() - started}`
      );
      return {
        filePath: downloaded.filePath,
        metadata: {
          title: candidate.kind === "audio" ? "Threads music" : "Threads audio",
          performer: "Threads",
          duration: null,
        },
        mediaKind: candidate.kind,
      };
    } catch (error) {
      lastError = String(error?.message || error).slice(0, 500);
    }
  }

  throw new Error(`DLPanda media extraction failed: ${lastError}`);
}

async function probeDlpandaThreadsAssets(mediaUrl) {
  try {
    const pageUrl = "https://dlpanda.com/threads";
    const response = await fetch(pageUrl, {
      redirect: "follow",
      headers: {
        "user-agent":
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
        "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
      },
      signal: AbortSignal.timeout(15000),
    });
    const html = await response.text();

    const formNeedleIndex = html.indexOf("data-download-form");
    if (formNeedleIndex >= 0) {
      const formStart = html.lastIndexOf("<form", formNeedleIndex);
      const formOpenEnd = formStart >= 0 ? html.indexOf(">", formStart) : -1;
      const formClose = formOpenEnd >= 0 ? html.indexOf("</form>", formOpenEnd) : -1;
      if (formStart >= 0 && formOpenEnd > formStart && formClose > formOpenEnd) {
        const tag = html.slice(formStart, formOpenEnd + 1);
        const formHtml = html.slice(formStart, formClose + 7);
        const action = (tag.match(/\saction=["']([^"']*)["']/i) || [,""])[1];
        const method = (tag.match(/\smethod=["']([^"']*)["']/i) || [,"GET"])[1];
        const requiresSecurity = (tag.match(/data-requires-security-check=["']([^"']*)["']/i) || [,""])[1];
        const platform = (tag.match(/data-platform=["']([^"']*)["']/i) || [,""])[1];
        const inputs = [...formHtml.matchAll(/<input\b[^>]*>/gi)].map((match) => {
          const input = match[0];
          const name = (input.match(/\sname=["']([^"']+)["']/i) || [,""])[1];
          const type = (input.match(/\stype=["']([^"']+)["']/i) || [,"text"])[1];
          const rawValue = (input.match(/\svalue=["']([^"']*)["']/i) || [,""])[1];
          const value = /token|csrf|turnstile|auth|secret/i.test(name)
            ? (rawValue ? "[present]" : "")
            : rawValue;
          return { name, type, value };
        }).filter((item) => item.name);
        console.log(
          `[DLPANDA-FORM] action=${action || "-"} method=${method || "-"} platform=${platform || "-"} security=${requiresSecurity || "-"} inputs=${JSON.stringify(inputs).slice(0,3000)}`
        );
      } else {
        console.log(
          `[DLPANDA-FORM] marker_found=true form_start=${formStart} open_end=${formOpenEnd} close=${formClose}`
        );
      }
    }

    const scriptSources = [...html.matchAll(/<script[^>]+src=["']([^"']+)["']/gi)]
      .map((match) => {
        try { return new URL(match[1], pageUrl).toString(); } catch { return ""; }
      })
      .filter(Boolean);
    const htmlHints = [...html.matchAll(/["']([^"']{0,180}(?:\/api\/|api\.|download|parse|threads)[^"']{0,240})["']/gi)]
      .map((match) => match[1])
      .filter((value, index, array) => array.indexOf(value) === index)
      .slice(0, 60);

    const formNeedles = [
      "data-download-form",
      "data-parse-submit",
      "download_type",
      "name=\"url\"",
      "data-download-url",
    ];
    const formSamples = formNeedles
      .map((needle) => {
        const index = html.indexOf(needle);
        return index >= 0
          ? needle + "::" + html.slice(Math.max(0, index - 1400), index + 2600).replace(/\\s+/g, " ")
          : needle + "::-";
      })
      .join(" || ");
    console.log(
      `[DLPANDA-DIAG] page status=${response.status} bytes=${html.length} scripts=${scriptSources.slice(0,20).join(" | ")} html_hints=${htmlHints.join(" | ").slice(0,6000)} form_samples=${formSamples.slice(0,12000)}`
    );

    for (const scriptUrl of scriptSources.slice(0, 20)) {
      try {
        const jsResponse = await fetch(scriptUrl, {
          headers: {
            "user-agent": "Mozilla/5.0",
            "accept": "*/*",
            "referer": pageUrl,
          },
          signal: AbortSignal.timeout(15000),
        });
        const js = await jsResponse.text();
        if (!/threads|\/api\/|download|parse/i.test(js)) continue;
        const hints = [...js.matchAll(/["']([^"']{0,180}(?:\/api\/|https?:\/\/[^"' ]+|download|parse|threads)[^"']{0,260})["']/gi)]
          .map((match) => match[1])
          .filter((value, index, array) => array.indexOf(value) === index)
          .slice(0, 80);
        const codeNeedles = [
          "fetch(n.url",
          "data-parse-submit",
          "parse_attempt",
          "download_type",
          "new FormData",
          "URLSearchParams",
        ];
        const codeSamples = codeNeedles
          .map((needle) => {
            const index = js.indexOf(needle);
            return index >= 0
              ? needle + "::" + js.slice(Math.max(0, index - 2200), index + 5200)
              : needle + "::-";
          })
          .join(" || ");
        console.log(
          `[DLPANDA-DIAG] asset=${scriptUrl} status=${jsResponse.status} bytes=${js.length} hints=${hints.join(" | ").slice(0,9000)} code_samples=${codeSamples.slice(0,18000)}`
        );
      } catch (error) {
        console.log(
          `[DLPANDA-DIAG] asset_error=${scriptUrl} error=${String(error?.message || error).slice(0,300)}`
        );
      }
    }
  } catch (error) {
    console.log(
      `[DLPANDA-DIAG] page_error=${String(error?.message || error).slice(0,500)} url=${mediaUrl}`
    );
  }
}

function probeAudioCodec(filePath) {
  const result = spawnSync(
    "ffprobe",
    [
      "-v", "error",
      "-select_streams", "a:0",
      "-show_entries", "stream=codec_name",
      "-of", "default=noprint_wrappers=1:nokey=1",
      filePath,
    ],
    {
      encoding: "utf8",
      timeout: 15000,
      maxBuffer: 256 * 1024,
    }
  );
  if (result.status !== 0) return "";
  return String(result.stdout || "").trim().toLowerCase();
}

function prepareSocialAudioFile(sourcePath, prefix) {
  const codec = probeAudioCodec(sourcePath);
  let targetPath = "";
  let args = [];
  let transcoded = false;

  if (codec === "aac") {
    targetPath = `${prefix}.fast.m4a`;
    args = [
      "-hide_banner", "-loglevel", "error", "-y",
      "-i", sourcePath,
      "-map", "0:a:0?",
      "-vn",
      "-c:a", "copy",
      "-movflags", "+faststart",
      targetPath,
    ];
  } else if (codec === "mp3") {
    targetPath = `${prefix}.fast.mp3`;
    args = [
      "-hide_banner", "-loglevel", "error", "-y",
      "-i", sourcePath,
      "-map", "0:a:0?",
      "-vn",
      "-c:a", "copy",
      targetPath,
    ];
  } else {
    targetPath = `${prefix}.fast.m4a`;
    transcoded = true;
    args = [
      "-hide_banner", "-loglevel", "error", "-y",
      "-i", sourcePath,
      "-map", "0:a:0?",
      "-vn",
      "-ac", "2",
      "-c:a", "aac",
      "-b:a", "192k",
      "-movflags", "+faststart",
      targetPath,
    ];
  }

  const result = spawnSync("ffmpeg", args, {
    encoding: "utf8",
    timeout: 120000,
    maxBuffer: 2 * 1024 * 1024,
  });
  if (
    result.status !== 0 ||
    !fs.existsSync(targetPath) ||
    fs.statSync(targetPath).size <= 0
  ) {
    throw new Error(
      "social audio prepare failed" +
        (result.stderr ? `: ${String(result.stderr).slice(-700)}` : "")
    );
  }
  return { filePath: targetPath, codec, transcoded };
}

async function runSocialWorkerAudio(mediaUrl, source) {
  if (!fs.existsSync(YOUTUBE_WORKER_YTDLP_BIN)) {
    throw new Error("yt-dlp worker binary is missing");
  }

  source = String(source || "").trim().toLowerCase();
  if (!isAllowedSocialUrl(source, mediaUrl)) {
    throw new Error("invalid social media URL");
  }

  const id = crypto.randomUUID();
  const prefix = path.join(os.tmpdir(), `social-worker-${source}-${id}`);
  const outputTemplate = `${prefix}.%(ext)s`;
  const titlePath = `${prefix}.title.txt`;
  const trackPath = `${prefix}.track.txt`;
  const uploaderPath = `${prefix}.uploader.txt`;
  const uploaderIdPath = `${prefix}.uploader-id.txt`;
  const durationPath = `${prefix}.duration.txt`;
  let preparedPath = null;

  const baseArgs = [
    "--no-playlist",
    "--no-warnings",
    "--quiet",
    "--socket-timeout", "15",
    "--retries", "1",
    "--fragment-retries", "1",
    "--max-filesize", "150M",
    "--js-runtimes", "node",
    "--plugin-dirs", path.join(process.cwd(), "render"),
    "--format", "bestaudio/best",
    "--output", outputTemplate,
    "--print-to-file", "after_move:%(title)s", titlePath,
    "--print-to-file", "after_move:%(track)s", trackPath,
    "--print-to-file", "after_move:%(uploader)s", uploaderPath,
    "--print-to-file", "after_move:%(uploader_id)s", uploaderIdPath,
    "--print-to-file", "after_move:%(duration)s", durationPath,
  ];

  const clearOutputs = (keep = null) => {
    try {
      for (const name of fs.readdirSync(os.tmpdir())) {
        if (!name.startsWith(`social-worker-${source}-${id}.`)) continue;
        const full = path.join(os.tmpdir(), name);
        if (keep && full === keep) continue;
        try { fs.rmSync(full, { force: true }); } catch {}
      }
    } catch {}
  };

  const findMediaOutput = () => {
    const ignored = new Set([
      titlePath, trackPath, uploaderPath, uploaderIdPath, durationPath,
    ]);
    const matches = fs
      .readdirSync(os.tmpdir())
      .filter((name) =>
        name.startsWith(`social-worker-${source}-${id}.`)
      )
      .map((name) => path.join(os.tmpdir(), name))
      .filter((full) => !ignored.has(full) && !full.endsWith(".part"))
      .filter((full) => {
        try { return fs.statSync(full).isFile(); } catch { return false; }
      });
    if (!matches.length) {
      throw new Error("social worker produced no media file");
    }
    matches.sort((a, b) => fs.statSync(b).size - fs.statSync(a).size);
    return matches[0];
  };

  try {
    let threadsProvider = null;
    let providerRawPath = null;

    let effectiveMediaUrl = mediaUrl;
    if (source === "threads") {
      const shareAlias = isThreadsShareAlias(mediaUrl);

      // Keep the hot path short. Render's current IP gets a generic Threads
      // shell for /share/ aliases, so avoid the old multi-attempt crawler
      // waterfall. Try the preview UA once, then media providers directly.
      if (shareAlias) {
        // Resolve the opaque mobile /share/ token at an edge location first.
        // Render's own IP currently receives the generic Threads SPA shell.
        effectiveMediaUrl = await resolveThreadsShareViaEdgeResolver(mediaUrl);
        if (effectiveMediaUrl === mediaUrl) {
          effectiveMediaUrl = await resolveThreadsShareViaTelegramBot(mediaUrl);
        }
        if (effectiveMediaUrl === mediaUrl) {
          effectiveMediaUrl = await resolveThreadsShareViaFxThreads(mediaUrl);
        }
        if (effectiveMediaUrl === mediaUrl) {
          effectiveMediaUrl = await resolveThreadsShareViaJina(mediaUrl);
        }
      }

      const providers = effectiveMediaUrl !== mediaUrl
        ? [
            ["FixThreads", fetchFixThreadsMedia],
            ["FxThreads", fetchFxThreadsMedia],
            ["vxThreads", fetchVxThreadsMedia],
            ["PostCopilot", fetchPostCopilotThreadsMedia],
            ["ThreadsDL", fetchThreadsDlMedia],
            ["DLPanda", fetchDlpandaThreadsMedia],
          ]
        : shareAlias
          ? [
              ...(String(process.env.EASYDOWN_API_KEY || "").trim()
                ? [["EasyDown", fetchEasyDownThreadsMedia]]
                : []),
              ["FixThreads", fetchFixThreadsMedia],
              ["curl-x", fetchCurlXThreadsMedia],
              ["Microlink", fetchMicrolinkThreadsMedia],
              ["FxThreads", fetchFxThreadsMedia],
              ["vxThreads", fetchVxThreadsMedia],
              ["PostCopilot", fetchPostCopilotThreadsMedia],
              ["DLPanda", fetchDlpandaThreadsMedia],
              ["ThreadsDL", fetchThreadsDlMedia],
            ]
          : [
              ["FixThreads", fetchFixThreadsMedia],
              ["FxThreads", fetchFxThreadsMedia],
              ["ThreadsDL", fetchThreadsDlMedia],
              ["vxThreads", fetchVxThreadsMedia],
              ["DLPanda", fetchDlpandaThreadsMedia],
            ];

      for (const [providerName, provider] of providers) {
        try {
          console.log(`[SOCIAL-WORKER] threads ${providerName} hybrid start`);
          threadsProvider = await provider(effectiveMediaUrl, prefix);
          providerRawPath = threadsProvider.filePath;
          break;
        } catch (error) {
          console.warn(
            `[SOCIAL-WORKER] threads ${providerName} hybrid failed:`,
            String(error?.message || error).slice(0, 1200)
          );
          clearOutputs();
        }
      }
    }

    if (!providerRawPath) {
      console.log(`[SOCIAL-WORKER] ${source} direct download start`);
      let directError = null;
      try {
        await runYoutubeWorkerProcess([...baseArgs, effectiveMediaUrl], 120000);
      } catch (error) {
        directError = error;
        console.warn(
          `[SOCIAL-WORKER] ${source} direct failed:`,
          String(error?.message || error).slice(0, 1200)
        );
        clearOutputs();
      }

      if (directError) {
        console.log(`[SOCIAL-WORKER] ${source} WARP fallback start`);
        await runYoutubeWorkerProcess(
          [...baseArgs, "--proxy", YOUTUBE_WORKER_PROXY, effectiveMediaUrl],
          120000
        );
      }
    }

    const rawPath = providerRawPath || findMediaOutput();
    const rawStat = fs.statSync(rawPath);
    if (rawStat.size <= 0) {
      throw new Error("social worker media is empty");
    }
    if (rawStat.size > YOUTUBE_WORKER_MAX_SOURCE_BYTES) {
      throw new Error("social worker media exceeded safety limit");
    }

    const prepared = prepareSocialAudioFile(rawPath, prefix);
    preparedPath = prepared.filePath;
    const stat = fs.statSync(preparedPath);
    if (stat.size > 49 * 1024 * 1024) {
      throw new Error("social fast audio exceeds Telegram audio limit");
    }

    const track = socialReadMeta(trackPath, "");
    const uploader = socialReadMeta(
      uploaderPath,
      socialSourceLabel(source)
    );
    const uploaderId = socialReadMeta(uploaderIdPath, "")
      .replace(/^@+/, "");
    const rawTitle = socialReadMeta(titlePath, "");

    let resolvedSound = null;
    if (source === "threads" && threadsProvider?.metadata) {
      resolvedSound = {
        title: threadsProvider.metadata.title,
        performer: threadsProvider.metadata.performer,
      };
    } else if (source === "instagram") {
      resolvedSound = await fetchInstagramSoundMetadata(mediaUrl);
    }

    const uploaderHandle = String(uploader || "")
      .trim()
      .replace(/^@+/, "");
    const friendlyHandle = (
      uploaderHandle &&
      !/^\d+$/.test(uploaderHandle) &&
      !/^instagram$/i.test(uploaderHandle)
    )
      ? uploaderHandle
      : "";

    const fallbackHandle = friendlyHandle || (
      uploaderId && !/^\d+$/.test(uploaderId)
        ? uploaderId
        : ""
    );

    const title = (
      resolvedSound?.title ||
      track ||
      (
        fallbackHandle
          ? `Original audio — @${fallbackHandle}`
          : rawTitle || `${socialSourceLabel(source)} audio`
      )
    );
    const performer = (
      resolvedSound?.performer ||
      (
        fallbackHandle
          ? `@${fallbackHandle}`
          : socialSourceLabel(source)
      )
    );
    const rawDuration = socialReadMeta(durationPath, "");
    const duration = Number(rawDuration);

    console.log(
      `[SOCIAL-WORKER] ${source} audio ready bytes=${stat.size} codec=${prepared.codec || "unknown"} transcoded=${prepared.transcoded}`
    );

    clearOutputs(preparedPath);
    return {
      filePath: preparedPath,
      metadata: {
        title,
        performer,
        duration:
          Number.isFinite(duration) && duration > 0 ? duration : null,
      },
      qualityLabel: prepared.transcoded
        ? "Fast Audio"
        : "Original Quality",
    };
  } catch (error) {
    clearOutputs();
    throw error;
  }
}

async function runYoutubeWorkerAudio(
  videoUrl,
  cookiesText,
  { captureMetadata = false } = {}
) {
  if (!fs.existsSync(YOUTUBE_WORKER_YTDLP_BIN)) {
    throw new Error("yt-dlp worker binary is missing");
  }
  if (!fs.existsSync(YOUTUBE_WORKER_PLUGIN_DIR)) {
    throw new Error("bgutil yt-dlp plugin directory is missing");
  }

  const id = crypto.randomUUID();
  const prefix = path.join(os.tmpdir(), `youtube-worker-${id}`);
  const cookiePath = `${prefix}.cookies.txt`;
  const outputTemplate = `${prefix}.%(ext)s`;
  const titlePath = `${prefix}.title.txt`;
  const performerPath = `${prefix}.performer.txt`;
  const durationPath = `${prefix}.duration.txt`;
  let outputPath = null;

  const metadataArgs = captureMetadata
    ? [
        "--print-to-file",
        "after_move:%(title)s",
        titlePath,
        "--print-to-file",
        "after_move:%(uploader)s",
        performerPath,
        "--print-to-file",
        "after_move:%(duration)s",
        durationPath,
      ]
    : [];

  const baseArgs = [
    "--no-playlist",
    "--no-warnings",
    "--quiet",
    "--socket-timeout", "15",
    "--retries", "1",
    "--fragment-retries", "1",
    "--max-filesize", "150M",
    "--proxy", YOUTUBE_WORKER_PROXY,
    "--plugin-dirs", YOUTUBE_WORKER_PLUGIN_DIR,
    "--js-runtimes", "node",
    "--extractor-args",
    "youtube:player_client=mweb;fetch_pot=always",
    "--extractor-args",
    "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416",
    "--format",
    "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
    "--output", outputTemplate,
    ...metadataArgs,
  ];

  const clearOutputs = () => {
    for (const name of fs.readdirSync(os.tmpdir())) {
      if (name.startsWith(`youtube-worker-${id}.`) && name !== path.basename(cookiePath)) {
        try { fs.rmSync(path.join(os.tmpdir(), name), { force: true }); } catch {}
      }
    }
  };

  try {
    if (String(cookiesText || "").trim()) {
      fs.writeFileSync(cookiePath, cookiesText, { mode: 0o600 });
    }

    // First try a clean guest session. Account-bound/stale cookies can make
    // YouTube return LOGIN_REQUIRED before the PO token path is useful.
    console.log("[YOUTUBE-WORKER] guest download start");
    let guestError = null;
    try {
      await runYoutubeWorkerProcess([...baseArgs, videoUrl]);
    } catch (error) {
      guestError = error;
      console.warn(
        "[YOUTUBE-WORKER] guest download failed:",
        String(error?.message || error).slice(0, 1200)
      );
      clearOutputs();
    }

    // Only fall back to the supplied cookie session when guest mode failed.
    if (guestError) {
      if (!fs.existsSync(cookiePath)) throw guestError;
      console.log("[YOUTUBE-WORKER] cookie fallback start");
      await runYoutubeWorkerProcess([
        ...baseArgs,
        "--cookies", cookiePath,
        videoUrl,
      ]);
    }

    outputPath = findYoutubeWorkerOutput(prefix);
    const stat = fs.statSync(outputPath);
    if (stat.size <= 0) throw new Error("YouTube worker audio file is empty");
    if (stat.size > YOUTUBE_WORKER_MAX_SOURCE_BYTES) {
      throw new Error("YouTube worker audio exceeded safety limit");
    }
    console.log(`[YOUTUBE-WORKER] download ready bytes=${stat.size}`);

    if (!captureMetadata) {
      return outputPath;
    }

    const readMeta = (metaPath, fallback = "") => {
      try {
        return fs.readFileSync(metaPath, "utf8").trim() || fallback;
      } catch {
        return fallback;
      }
    };
    const rawDuration = readMeta(durationPath, "");
    const parsedDuration = Number(rawDuration);

    return {
      filePath: outputPath,
      metadata: {
        title: readMeta(titlePath, "Audio"),
        performer: readMeta(performerPath, "YouTube"),
        duration:
          Number.isFinite(parsedDuration) && parsedDuration > 0
            ? parsedDuration
            : null,
      },
    };
  } catch (error) {
    clearOutputs();
    throw error;
  } finally {
    try { fs.rmSync(cookiePath, { force: true }); } catch {}
    try { fs.rmSync(titlePath, { force: true }); } catch {}
    try { fs.rmSync(performerPath, { force: true }); } catch {}
    try { fs.rmSync(durationPath, { force: true }); } catch {}
  }
}

async function streamYoutubeWorkerAudio(videoUrl, cookiesText, response) {
  if (!fs.existsSync(YOUTUBE_WORKER_YTDLP_BIN)) {
    throw new Error("yt-dlp worker binary is missing");
  }
  if (!fs.existsSync(YOUTUBE_WORKER_PLUGIN_DIR)) {
    throw new Error("bgutil yt-dlp plugin directory is missing");
  }

  const id = crypto.randomUUID();
  const cookiePath = path.join(os.tmpdir(), `youtube-stream-${id}.cookies.txt`);
  if (String(cookiesText || "").trim()) {
    fs.writeFileSync(cookiePath, cookiesText, { mode: 0o600 });
  }

  const baseArgs = [
    "--no-playlist",
    "--no-warnings",
    "--quiet",
    "--socket-timeout", "15",
    "--retries", "1",
    "--fragment-retries", "1",
    "--max-filesize", "150M",
    "--proxy", YOUTUBE_WORKER_PROXY,
    "--plugin-dirs", YOUTUBE_WORKER_PLUGIN_DIR,
    "--js-runtimes", "node",
    "--extractor-args",
    "youtube:player_client=mweb;fetch_pot=always",
    "--extractor-args",
    "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416",
    "--format",
    "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
    "--output", "-",
  ];

  const runAttempt = (extraArgs = []) => new Promise((resolve, reject) => {
    const child = spawn(
      YOUTUBE_WORKER_YTDLP_BIN,
      [...baseArgs, ...extraArgs, videoUrl],
      {
        cwd: process.cwd(),
        env: {
          ...process.env,
          NO_PROXY: "127.0.0.1,localhost",
          no_proxy: "127.0.0.1,localhost",
        },
        stdio: ["ignore", "pipe", "pipe"],
      }
    );

    let stderr = "";
    let total = 0;
    let started = false;
    let settled = false;

    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      if (!started && !settled) {
        settled = true;
        reject(new Error("YouTube stream worker timed out"));
      } else if (started) {
        response.destroy(new Error("YouTube stream worker timed out"));
      }
    }, 120000);
    timer.unref?.();

    const failBeforeStart = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(error);
    };

    child.stderr.on("data", (chunk) => {
      stderr = (stderr + chunk.toString("utf8")).slice(-12000);
    });

    child.stdout.on("data", (chunk) => {
      if (!chunk?.length) return;
      if (!started) {
        started = true;
        response.writeHead(200, {
          "content-type": "application/octet-stream",
          "cache-control": "no-store",
          "transfer-encoding": "chunked",
        });
      }

      total += chunk.length;
      if (total > YOUTUBE_WORKER_MAX_SOURCE_BYTES) {
        child.kill("SIGKILL");
        response.destroy(new Error("YouTube worker audio exceeded safety limit"));
        return;
      }

      if (!response.write(chunk)) {
        child.stdout.pause();
        response.once("drain", () => child.stdout.resume());
      }
    });

    child.once("error", (error) => {
      if (!started) {
        failBeforeStart(error);
      } else {
        response.destroy(error);
      }
    });

    child.once("exit", (code, signal) => {
      clearTimeout(timer);
      const detail = stderr.trim().slice(-1200);
      if (!started) {
        failBeforeStart(
          new Error(
            `yt-dlp stream failed code=${code} signal=${signal || ""}` +
              (detail ? `: ${detail}` : "")
          )
        );
        return;
      }

      if (code !== 0) {
        response.destroy(
          new Error(
            `yt-dlp stream failed after start code=${code} signal=${signal || ""}`
          )
        );
      } else {
        response.end();
      }
      if (!settled) {
        settled = true;
        console.log(`[YOUTUBE-WORKER] stream complete bytes=${total}`);
        resolve();
      }
    });

    response.once("close", () => {
      if (!child.killed && child.exitCode == null) {
        try { child.kill("SIGTERM"); } catch {}
      }
    });
  });

  try {
    console.log("[YOUTUBE-WORKER] guest stream start");
    try {
      await runAttempt();
    } catch (guestError) {
      if (!fs.existsSync(cookiePath) || response.headersSent) throw guestError;
      console.warn(
        "[YOUTUBE-WORKER] guest stream failed before media:",
        String(guestError?.message || guestError).slice(0, 1200)
      );
      console.log("[YOUTUBE-WORKER] cookie stream fallback start");
      await runAttempt(["--cookies", cookiePath]);
    }
  } finally {
    try { fs.rmSync(cookiePath, { force: true }); } catch {}
  }
}

function telegramText(value, fallback = "Audio", max = 64) {
  const text = String(value || fallback)
    .replace(/[\u0000-\u001f\u007f]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return (text || fallback).slice(0, max);
}

function telegramFilename(value, ext) {
  const stem = telegramText(value, "Audio", 96)
    .replace(/[\\/:*?"<>|]+/g, " ")
    .replace(/\.+$/g, "")
    .trim() || "Audio";
  return `${stem}.${ext}`;
}

function escapeHtml(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

async function sendWorkerAudioUrlToTelegram({
  botToken,
  chatId,
  audioUrl,
  title,
  performer,
  duration,
  businessConnectionId,
}) {
  if (!(await validateTelegramBotToken(botToken))) {
    throw new Error("invalid Telegram bot token");
  }

  const parsed = new URL(String(audioUrl || ""));
  if (parsed.protocol !== "https:") {
    throw new Error("Telegram direct audio URL must be HTTPS");
  }

  const form = new FormData();
  form.append("chat_id", String(chatId));
  form.append("audio", audioUrl);
  form.append("title", telegramText(title, "Audio", 64));
  form.append("performer", telegramText(performer, "YouTube", 64));
  if (Number(duration) > 0) {
    form.append("duration", String(Math.round(Number(duration))));
  }
  form.append(
    "caption",
    `🎵 ${escapeHtml(telegramText(title, "Audio", 200))}\n${escapeHtml(qualityLabel)}`
  );
  form.append("parse_mode", "HTML");
  if (businessConnectionId) {
    form.append("business_connection_id", String(businessConnectionId));
  }

  const started = Date.now();
  const tgResponse = await fetch(
    `https://api.telegram.org/bot${botToken}/sendAudio`,
    {
      method: "POST",
      body: form,
      signal: AbortSignal.timeout(120000),
    }
  );
  const raw = await tgResponse.text();
  let data = null;
  try { data = JSON.parse(raw); } catch {}

  if (!tgResponse.ok || !data?.ok || !data?.result?.audio?.file_id) {
    throw new Error(
      `Telegram URL sendAudio failed status=${tgResponse.status}: ${String(
        data?.description || raw || "unknown"
      ).slice(0, 500)}`
    );
  }

  console.log(
    `[YOUTUBE-WORKER] Telegram URL send ready ms=${Date.now() - started}`
  );
  return {
    message_id: data.result.message_id,
    file_id: data.result.audio.file_id,
    file_unique_id: data.result.audio.file_unique_id || null,
    file_size: data.result.audio.file_size || null,
    duration: data.result.audio.duration || null,
  };
}

async function sendWorkerAudioToTelegram({
  botToken,
  chatId,
  filePath,
  title,
  performer,
  duration,
  businessConnectionId,
  qualityLabel = "Original Quality",
}) {
  if (!(await validateTelegramBotToken(botToken))) {
    throw new Error("invalid Telegram bot token");
  }

  const ext = path.extname(filePath).replace(/^\./, "").toLowerCase();
  if (!["m4a", "mp4", "mp3"].includes(ext)) {
    throw new Error(
      `fast Telegram audio requires m4a/mp4/mp3 source, got ${ext || "unknown"}`
    );
  }

  const stat = fs.statSync(filePath);
  if (stat.size <= 0 || stat.size > 49 * 1024 * 1024) {
    throw new Error("fast Telegram audio file is outside safe size");
  }

  const mime = ext === "mp3" ? "audio/mpeg" : "audio/mp4";
  const bytes = fs.readFileSync(filePath);
  const form = new FormData();
  form.append("chat_id", String(chatId));
  form.append(
    "audio",
    new Blob([bytes], { type: mime }),
    telegramFilename(title, ext === "mp3" ? "mp3" : "m4a")
  );
  form.append("title", telegramText(title, "Audio", 64));
  form.append("performer", telegramText(performer, "YouTube", 64));
  if (Number(duration) > 0) {
    form.append("duration", String(Math.round(Number(duration))));
  }
  form.append(
    "caption",
    `🎵 ${escapeHtml(telegramText(title, "Audio", 200))}\nOriginal Quality`
  );
  form.append("parse_mode", "HTML");
  if (businessConnectionId) {
    form.append("business_connection_id", String(businessConnectionId));
  }

  const started = Date.now();
  const tgResponse = await fetch(
    `https://api.telegram.org/bot${botToken}/sendAudio`,
    {
      method: "POST",
      body: form,
      signal: AbortSignal.timeout(120000),
    }
  );
  const raw = await tgResponse.text();
  let data = null;
  try { data = JSON.parse(raw); } catch {}

  if (!tgResponse.ok || !data?.ok || !data?.result?.audio?.file_id) {
    throw new Error(
      `Telegram sendAudio failed status=${tgResponse.status}: ${String(
        data?.description || raw || "unknown"
      ).slice(0, 500)}`
    );
  }

  console.log(
    `[YOUTUBE-WORKER] Telegram direct send ready bytes=${stat.size} ms=${Date.now() - started}`
  );

  return {
    message_id: data.result.message_id,
    file_id: data.result.audio.file_id,
    file_unique_id: data.result.audio.file_unique_id || null,
    file_size: data.result.audio.file_size || stat.size,
    duration: data.result.audio.duration || null,
  };
}

function enqueueYoutubeWorker(task) {
  const run = youtubeWorkerTail.then(task, task);
  youtubeWorkerTail = run.catch(() => {});
  return run;
}

function enqueueSocialWorker(task) {
  const run = socialWorkerTail.then(task, task);
  socialWorkerTail = run.catch(() => {});
  return run;
}

function streamWorkerFile(response, filePath) {
  const stat = fs.statSync(filePath);
  const ext = path.extname(filePath).replace(/^\./, "") || "audio";
  const contentTypes = {
    m4a: "audio/mp4",
    mp4: "audio/mp4",
    webm: "audio/webm",
    opus: "audio/ogg",
    ogg: "audio/ogg",
  };
  response.writeHead(200, {
    "content-type": contentTypes[ext] || "application/octet-stream",
    "content-length": stat.size,
    "content-disposition": `attachment; filename="source.${ext}"`,
    "x-audio-ext": ext,
    "cache-control": "no-store",
  });

  const stream = fs.createReadStream(filePath);
  let cleaned = false;
  const cleanup = () => {
    if (cleaned) return;
    cleaned = true;
    try { fs.rmSync(filePath, { force: true }); } catch {}
  };
  stream.once("error", (error) => {
    console.error("[YOUTUBE-WORKER] stream error:", error?.message || error);
    response.destroy(error);
    cleanup();
  });
  response.once("close", cleanup);
  response.once("finish", cleanup);
  stream.pipe(response);
}

const server = http.createServer(async (request, response) => {
  const url = new URL(request.url || "/", `http://${request.headers.host || HOST}`);

  if (request.method === "GET" && url.pathname === "/ping") {
    const payload = JSON.stringify({ ok: true });
    response.writeHead(200, {
      "content-type": "application/json",
      "content-length": Buffer.byteLength(payload),
    });
    response.end(payload);
    return;
  }

  if (
    request.method === "POST" &&
    url.pathname === "/social-telegram-audio"
  ) {
    if (!(await workerAuthorized(request))) {
      response.writeHead(401, { "content-type": "application/json" });
      response.end(JSON.stringify({ error: "unauthorized" }));
      return;
    }

    let preparedPath = null;
    try {
      const body = await readJsonBody(request);
      const source = String(body?.source || "").trim().toLowerCase();
      const mediaUrl = String(body?.url || "").trim();
      const botToken = String(body?.telegram_bot_token || "").trim();
      const chatId = String(body?.chat_id || "").trim();

      if (!isAllowedSocialUrl(source, mediaUrl)) {
        throw new Error("invalid social media URL");
      }
      if (!/^-?\d+$/.test(chatId)) {
        throw new Error("invalid Telegram chat id");
      }
      if (!/^\d+:[A-Za-z0-9_-]+$/.test(botToken)) {
        throw new Error("invalid Telegram bot token format");
      }

      console.log(
        `[SOCIAL-WORKER] ${source} direct Telegram audio start`
      );

      const downloaded = await enqueueSocialWorker(() =>
        runSocialWorkerAudio(mediaUrl, source)
      );
      preparedPath = downloaded.filePath;
      const metadata = downloaded.metadata || {};
      const qualityLabel =
        downloaded.qualityLabel || "Original Quality";

      const sent = await sendWorkerAudioToTelegram({
        botToken,
        chatId,
        filePath: preparedPath,
        title: metadata.title || "Audio",
        performer: metadata.performer || socialSourceLabel(source),
        duration: metadata.duration,
        businessConnectionId: body?.business_connection_id,
        qualityLabel,
      });

      const payload = JSON.stringify({
        ok: true,
        ...sent,
        title: telegramText(metadata.title, "Audio", 200),
        performer: telegramText(
          metadata.performer,
          socialSourceLabel(source),
          200
        ),
        duration:
          Number(metadata.duration) > 0
            ? Number(metadata.duration)
            : sent?.duration || null,
        quality_label: qualityLabel,
      });
      response.writeHead(200, {
        "content-type": "application/json",
        "content-length": Buffer.byteLength(payload),
        "cache-control": "no-store",
      });
      response.end(payload);
    } catch (error) {
      console.error(
        "[SOCIAL-WORKER] request failed:",
        String(error?.message || error).slice(0, 1500)
      );
      if (!response.headersSent) {
        const payload = JSON.stringify({
          error: error?.message || "social audio worker unavailable",
        });
        response.writeHead(502, {
          "content-type": "application/json",
          "content-length": Buffer.byteLength(payload),
        });
        response.end(payload);
      }
    } finally {
      if (preparedPath) {
        try { fs.rmSync(preparedPath, { force: true }); } catch {}
      }
    }
    return;
  }

  if (request.method === "POST" && url.pathname === "/telegram-audio") {
    if (!(await workerAuthorized(request))) {
      response.writeHead(401, { "content-type": "application/json" });
      response.end(JSON.stringify({ error: "unauthorized" }));
      return;
    }

    let filePath = null;
    try {
      const body = await readJsonBody(request);
      const videoUrl = String(body?.url || "").trim();
      const cookiesText = String(body?.cookies || "");
      const botToken = String(body?.telegram_bot_token || "").trim();
      const chatId = String(body?.chat_id || "").trim();

      if (!isAllowedYoutubeUrl(videoUrl)) throw new Error("invalid YouTube URL");
      if (!/^-?\d+$/.test(chatId)) throw new Error("invalid Telegram chat id");
      if (!/^\d+:[A-Za-z0-9_-]+$/.test(botToken)) {
        throw new Error("invalid Telegram bot token format");
      }

      console.log("[YOUTUBE-WORKER] direct Telegram audio start");
      let sent = null;
      const urlFastPathEnabled = ["1", "true", "yes", "on"].includes(
        String(process.env.YOUTUBE_TELEGRAM_URL_FAST_PATH || "")
          .trim()
          .toLowerCase()
      );

      if (urlFastPathEnabled) {
        try {
          const audioUrl = await enqueueYoutubeWorker(() =>
            resolveYoutubeWorkerAudioUrl(videoUrl, cookiesText)
          );
          sent = await sendWorkerAudioUrlToTelegram({
            botToken,
            chatId,
            audioUrl,
            title: body?.title,
            performer: body?.performer,
            duration: body?.duration,
            businessConnectionId: body?.business_connection_id,
          });
          console.log("[YOUTUBE-WORKER] direct URL Telegram path succeeded");
        } catch (urlError) {
          console.warn(
            "[YOUTUBE-WORKER] direct URL Telegram path failed; using file fallback:",
            String(urlError?.message || urlError).slice(0, 1200)
          );
        }
      }

      let resolvedTitle = body?.title;
      let resolvedPerformer = body?.performer;
      let resolvedDuration = body?.duration;

      if (!sent) {
        const downloaded = await enqueueYoutubeWorker(() =>
          runYoutubeWorkerAudio(videoUrl, cookiesText, { captureMetadata: true })
        );
        filePath = downloaded.filePath;
        const downloadedMetadata = downloaded.metadata || {};
        resolvedTitle = resolvedTitle || downloadedMetadata.title || "Audio";
        resolvedPerformer =
          resolvedPerformer || downloadedMetadata.performer || "YouTube";
        resolvedDuration =
          Number(resolvedDuration) > 0
            ? Number(resolvedDuration)
            : Number(downloadedMetadata.duration) > 0
              ? Number(downloadedMetadata.duration)
              : null;

        sent = await sendWorkerAudioToTelegram({
          botToken,
          chatId,
          filePath,
          title: resolvedTitle,
          performer: resolvedPerformer,
          duration: resolvedDuration,
          businessConnectionId: body?.business_connection_id,
        });
      }

      const payload = JSON.stringify({
        ok: true,
        ...sent,
        title: telegramText(resolvedTitle, "Audio", 200),
        performer: telegramText(resolvedPerformer, "YouTube", 200),
        duration:
          Number(resolvedDuration) > 0
            ? Number(resolvedDuration)
            : sent?.duration || null,
      });
      response.writeHead(200, {
        "content-type": "application/json",
        "content-length": Buffer.byteLength(payload),
        "cache-control": "no-store",
      });
      response.end(payload);
    } catch (error) {
      console.error(
        "[YOUTUBE-WORKER] direct Telegram audio failed:",
        String(error?.message || error).slice(0, 1500)
      );
      if (!response.headersSent) {
        const payload = JSON.stringify({
          error: error?.message || "direct Telegram audio unavailable",
        });
        response.writeHead(502, {
          "content-type": "application/json",
          "content-length": Buffer.byteLength(payload),
        });
        response.end(payload);
      }
    } finally {
      if (filePath) {
        try { fs.rmSync(filePath, { force: true }); } catch {}
      }
    }
    return;
  }

  if (request.method === "POST" && url.pathname === "/audio-stream") {
    if (!(await workerAuthorized(request))) {
      response.writeHead(401, { "content-type": "application/json" });
      response.end(JSON.stringify({ error: "unauthorized" }));
      return;
    }

    try {
      const body = await readJsonBody(request);
      const videoUrl = String(body?.url || "").trim();
      const cookiesText = String(body?.cookies || "");
      if (!isAllowedYoutubeUrl(videoUrl)) {
        throw new Error("invalid YouTube URL");
      }
      await enqueueYoutubeWorker(() =>
        streamYoutubeWorkerAudio(videoUrl, cookiesText, response)
      );
    } catch (error) {
      console.error(
        "[YOUTUBE-WORKER] stream request failed:",
        String(error?.message || error).slice(0, 1500)
      );
      if (!response.headersSent) {
        const payload = JSON.stringify({
          error: error?.message || "YouTube stream worker unavailable",
        });
        response.writeHead(502, {
          "content-type": "application/json",
          "content-length": Buffer.byteLength(payload),
        });
        response.end(payload);
      }
    }
    return;
  }

  if (request.method === "POST" && url.pathname === "/audio") {
    if (!(await workerAuthorized(request))) {
      response.writeHead(401, { "content-type": "application/json" });
      response.end(JSON.stringify({ error: "unauthorized" }));
      return;
    }

    try {
      const body = await readJsonBody(request);
      const videoUrl = String(body?.url || "").trim();
      const cookiesText = String(body?.cookies || "");
      if (!isAllowedYoutubeUrl(videoUrl)) {
        throw new Error("invalid YouTube URL");
      }
      const filePath = await enqueueYoutubeWorker(() =>
        runYoutubeWorkerAudio(videoUrl, cookiesText)
      );
      streamWorkerFile(response, filePath);
    } catch (error) {
      console.error(
        "[YOUTUBE-WORKER] request failed:",
        String(error?.message || error).slice(0, 1500)
      );
      if (!response.headersSent) {
        const payload = JSON.stringify({
          error: error?.message || "YouTube worker unavailable",
        });
        response.writeHead(502, {
          "content-type": "application/json",
          "content-length": Buffer.byteLength(payload),
        });
        response.end(payload);
      }
    }
    return;
  }

  if (request.method !== "POST" || url.pathname !== "/get_pot") {
    response.writeHead(404);
    response.end();
    return;
  }

  try {
    const payload = await fetchSession();
    response.writeHead(200, {
      "content-type": "application/json",
      "content-length": Buffer.byteLength(payload),
    });
    response.end(payload);
  } catch (error) {
    const payload = JSON.stringify({
      error: error?.message || "provider unavailable",
    });
    response.writeHead(503, {
      "content-type": "application/json",
      "content-length": Buffer.byteLength(payload),
    });
    response.end(payload);
  }
});

writeRuntimeProxyEnv(false);

function usesRemoteSessionServer() {
  const value = String(process.env.YOUTUBE_SESSION_SERVER || "").trim();
  if (!value) return false;
  try {
    const url = new URL(value);
    return !["127.0.0.1", "localhost", "::1"].includes(url.hostname);
  } catch {
    return false;
  }
}

server.listen(PORT, HOST, () => {
  console.log(`[POT-ADAPTER] ready at http://${HOST}:${PORT}`);

  // TEMP diagnostic: validate the exact Threads /share/ URL end-to-end on
  // Render without depending on Telegram or the bot service rollout.
  setTimeout(async () => {
    let testFile = null;
    try {
      const result = await runSocialWorkerAudio(
        "https://www.threads.com/share/BAV6glx_i6/",
        "threads"
      );
      testFile = result?.filePath || null;
      const size = testFile && fs.existsSync(testFile)
        ? fs.statSync(testFile).size
        : 0;
      console.log(
        `[THREADS-SHARE-SELFTEST] PASS bytes=${size} title=${String(result?.metadata?.title || "").slice(0, 120)}`
      );
    } catch (error) {
      console.error(
        "[THREADS-SHARE-SELFTEST] FAIL:",
        String(error?.message || error).slice(0, 1500)
      );
    } finally {
      if (testFile) {
        try { fs.rmSync(testFile, { force: true }); } catch {}
      }
    }
  }, 1500).unref?.();

  if (usesRemoteSessionServer()) {
    const timer = setTimeout(() => {
      stopProviderProcesses();
      console.log("[POT-ADAPTER] external session server active; released local bgutil memory");
    }, 10000);
    timer.unref?.();
  }
});

startWarp().catch((error) => {
  console.error("[WARP] unhandled startup error:", error);
});
