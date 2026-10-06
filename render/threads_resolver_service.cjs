const http = require("node:http");

const PORT = Number(process.env.PORT || 10000);
const DEFAULT_TEST = "https://www.threads.com/share/BAV6glx_i6/";
const POST_RE = /https:\/\/(?:www\.)?threads\.(?:com|net)\/@[^\s/?#]+\/post\/[A-Za-z0-9_-]+/i;

function cleanHtmlValue(value) {
  return String(value || "")
    .replace(/&amp;/gi, "&")
    .replace(/&#0?64;/gi, "@")
    .replace(/\\u0026/g, "&")
    .replace(/\\\//g, "/")
    .trim();
}

function extractCanonical(html, finalUrl) {
  const candidates = [finalUrl];
  const patterns = [
    /<meta[^>]+property=["']og:url["'][^>]+content=["']([^"']+)["']/i,
    /<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:url["']/i,
    /<link[^>]+rel=["']canonical["'][^>]+href=["']([^"']+)["']/i,
    /<link[^>]+href=["']([^"']+)["'][^>]+rel=["']canonical["']/i,
  ];
  for (const rx of patterns) {
    const m = html.match(rx);
    if (m?.[1]) candidates.push(cleanHtmlValue(m[1]));
  }
  for (const value of candidates) {
    const m = cleanHtmlValue(value).match(POST_RE);
    if (m) return m[0];
  }
  return null;
}

async function probe(url) {
  const agents = [
    ["telegram", "Mozilla/5.0 (compatible; TelegramBot)"],
    ["iphone", "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1"],
    ["googlebot", "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"],
  ];
  const attempts = [];
  for (const [name, ua] of agents) {
    try {
      const response = await fetch(url, {
        redirect: "follow",
        headers: {
          "user-agent": ua,
          "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
          "accept-language": "en-US,en;q=0.9",
        },
        signal: AbortSignal.timeout(12000),
      });
      const html = await response.text();
      const canonical = extractCanonical(html, response.url);
      attempts.push({
        name,
        status: response.status,
        finalUrl: response.url,
        bytes: html.length,
        canonical,
      });
      if (canonical) return { ok:true, canonical, attempts };
    } catch (error) {
      attempts.push({name, error:String(error?.message || error).slice(0,300)});
    }
  }
  return { ok:false, attempts };
}

const server = http.createServer(async (req, res) => {
  const u = new URL(req.url, "http://127.0.0.1");
  if (u.pathname === "/health") {
    res.writeHead(200, {"content-type":"application/json"});
    return res.end(JSON.stringify({ok:true, region:"singapore"}));
  }
  if (u.pathname === "/resolve") {
    const url = u.searchParams.get("url") || "";
    if (!/^https:\/\/(?:www\.)?threads\.(?:com|net)\//i.test(url)) {
      res.writeHead(400, {"content-type":"application/json"});
      return res.end(JSON.stringify({ok:false,error:"invalid Threads URL"}));
    }
    const result = await probe(url);
    res.writeHead(result.ok ? 200 : 422, {"content-type":"application/json"});
    return res.end(JSON.stringify(result));
  }
  res.writeHead(200, {"content-type":"text/plain"});
  res.end("Threads resolver ready");
});

server.listen(PORT, "0.0.0.0", async () => {
  console.log("[THREADS-RESOLVER] ready port=" + PORT + " region=singapore");
  const result = await probe(DEFAULT_TEST);
  console.log("[THREADS-RESOLVER-SELFTEST] " + (result.ok ? "PASS" : "FAIL") + " " + JSON.stringify(result));
});
