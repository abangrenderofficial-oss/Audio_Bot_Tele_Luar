"use strict";

const http = require("node:http");

const PROVIDER_URL =
  process.env.POT_PROVIDER_URL || "http://127.0.0.1:4416/get_pot";
const HOST = process.env.POT_ADAPTER_HOST || "127.0.0.1";
const PORT = Number(process.env.POT_ADAPTER_PORT || "4417");
const ATTEMPTS = Number(process.env.POT_ADAPTER_ATTEMPTS || "30");
const RETRY_MS = Number(process.env.POT_ADAPTER_RETRY_MS || "500");

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

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
