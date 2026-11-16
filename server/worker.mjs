const VERSION = "43.10";
const POINT_COUNT = 965;
const HOURS = 24 * 7;
const HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "X-Content-Type-Options": "nosniff",
};

function json(value, status = 200, headers = {}) {
  return Response.json(value, { status, headers: { ...HEADERS, ...headers } });
}

async function readSubmission(request) {
  if (!request.headers.get("Content-Type")?.startsWith("application/json")) return null;
  const reader = request.body?.getReader();
  if (!reader) return null;
  let body = "";
  const decoder = new TextDecoder();
  let size = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    size += value.length;
    if (size > 256) {
      await reader.cancel();
      return null;
    }
    body += decoder.decode(value, { stream: true });
  }
  try {
    return JSON.parse(body + decoder.decode());
  } catch {
    return null;
  }
}

export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (path !== "/observations") {
      return json({ error: "not_found" }, 404);
    }
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: {
        ...HEADERS, "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
      } });
    }
    const hour = Math.floor(Date.now() / 3600000);
    if (request.method === "GET") {
      const { results } = await env.DB.prepare(
        "SELECT point_id, COUNT(*) AS observations FROM sightings WHERE hour_bucket > ? GROUP BY point_id ORDER BY point_id",
      ).bind(hour - HOURS).all();
      return json({ version: VERSION, points: results }, 200, {
        "Cache-Control": "public, max-age=60",
      });
    }
    if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405, { Allow: "GET, POST, OPTIONS" });
    const body = await readSubmission(request);
    if (body?.version !== VERSION || !Number.isInteger(body.point_id) ||
        body.point_id < 0 || body.point_id >= POINT_COUNT) {
      return json({ error: "invalid_observation" }, 400);
    }
    // One anonymous observation per known point per hour. No device identifier or IP is stored.
    await env.DB.batch([
      env.DB.prepare("INSERT OR IGNORE INTO sightings(point_id, hour_bucket) VALUES (?, ?)").bind(body.point_id, hour),
      env.DB.prepare("DELETE FROM sightings WHERE hour_bucket <= ?").bind(hour - HOURS),
    ]);
    return json({ accepted: true }, 202, { "Cache-Control": "no-store" });
  },
};
