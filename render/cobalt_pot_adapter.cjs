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

const WARP_ENABLED = /^(1|true|yes|on)$/i.test(
  String(process.env.COBALT_WARP_ENABLED || "")
);
const WARP_HOME = process.env.COBALT_WARP_HOME || "/tmp/cobalt-warp";
const WARP_SOCKS_HOST = "127.0.0.1";
const WARP_SOCKS_PORT = Number(process.env.COBALT_WARP_SOCKS_PORT || "1080");
const WARP_HTTP_HOST = "127.0.0.1";
const WARP_HTTP_PORT = Number(process.env.COBALT_WARP_HTTP_PORT || "3128");
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
    return { wire, proxy };
  } catch (error) {
    console.error("[WARP] startup failed:", error?.message || error);
    return null;
  }
}

async function fetchSession() {
  let lastError = "provider unavailable";

  for (let attempt = 1; attempt <= ATTEMPTS; attempt += 1) {
    try {
      const response = await fetch(PROVIDER_URL, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: "{}",
      });
      const text = await response.text();
      let data;

      try {
        data = JSON.parse(text);
      } catch {
        data = null;
      }

      if (response.ok && data?.poToken && data?.contentBinding) {
        return text;
      }

      lastError =
        data?.error ||
        `provider status ${response.status}`;
    } catch (error) {
      lastError = error?.message || String(error);
    }

    if (attempt < ATTEMPTS) {
      await sleep(RETRY_MS);
    }
  }

  throw new Error(lastError);
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

server.listen(PORT, HOST, () => {
  console.log(`[POT-ADAPTER] ready at http://${HOST}:${PORT}`);
});

startWarp().catch((error) => {
  console.error("[WARP] unhandled startup error:", error);
});
