// Cloudflare Worker: expõe a MESMA API do coordenador Python + painel estático. Segredos: EXEC_TOKEN, ADMIN_TOKEN (wrangler secret).
// Bindings: DB (D1). Cron: reap a cada 5 min (leases vencidos, executores mudos).
import { Coord, Fenced } from "./core.js";

const eq = (a, b) => { if (!a || !b || a.length !== b.length) return false; let r = 0; for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i); return r === 0; };
const json = (o, s = 200) => new Response(JSON.stringify(o), { status: s, headers: { "content-type": "application/json", "cache-control": "no-store" } });

export async function handle(req, env, coord, DASH = "") {
  const url = new URL(req.url), path = url.pathname;
  if (path === "/") return new Response(DASH, { headers: { "content-type": "text/html; charset=utf-8", "content-security-policy": "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'" } });
  if (path === "/api/health") return json({ ok: true });
  const tok = (req.headers.get("authorization") || "").slice(7);
  const isAdmin = eq(tok, env.ADMIN_TOKEN), isExec = eq(tok, env.EXEC_TOKEN);
  if (!(isAdmin || isExec) || req.headers.get("x-requested-with") !== "XHK") return json({ error: "unauthorized" }, 401);
  try {
    await coord.reap();
    if (path === "/api/state") return json(await coord.state());
    let m;
    if ((m = path.match(/^\/api\/exec\/task\/(\w+)$/))) { const t = await coord.db.prepare("SELECT * FROM tasks WHERE id=?").bind(m[1]).first(); return t ? json(t) : json({ error: "not_found" }, 404); }
    if (req.method !== "POST") return json({ error: "método" }, 405);
    const b = await req.json();
    if (path.startsWith("/api/admin/") && !isAdmin) return json({ error: "unauthorized" }, 401);
    // O Brain usa EXEC_TOKEN para seu lock operacional normal.
    // Somente o lock reservado do disparo do Kaggle exige ADMIN_TOKEN.
    if (path.startsWith("/api/exec/lock/") && b.name === "kaggle-launch" && !isAdmin)
      return json({ error: "unauthorized" }, 401);
    switch (path) {
      case "/api/admin/kaggle-launch":
        if (typeof b.enabled !== "boolean") return json({ error: "enabled deve ser boolean" }, 400);
        return json(await coord.setLaunchEnabled(b.enabled));
      case "/api/exec/register": return json(await coord.register(b.executor, b.provider, b.caps, b.session_seconds));
      case "/api/exec/heartbeat": return json(await coord.heartbeat(b.executor, +b.used_seconds || 0));
      case "/api/exec/claim": return json({ task: await coord.claim(b.executor, b.time_left) });
      case "/api/exec/renew": return json(await coord.renew(b.task, b.executor, b.fence));
      case "/api/exec/checkpoint": return json(await coord.checkpoint(b.task, b.executor, b.fence, b.data));
      case "/api/exec/complete": return json(await coord.complete(b.task, b.executor, b.fence, b.result, +b.seconds || 0));
      case "/api/exec/fail": return json(await coord.fail(b.task, b.executor, b.fence, b.error, b.kind, b.retry_after));
      case "/api/exec/release": return json(await coord.release(b.task, b.executor, b.fence, b.note));
      case "/api/exec/deregister": await coord.db.prepare("UPDATE executors SET status='stopped' WHERE id=?").bind(b.executor).run(); return json({ ok: true });
      case "/api/exec/submit": return json(await coord.submit(b.kind, b.payload, b));
      case "/api/exec/bykey": return json({ task: await coord.db.prepare("SELECT * FROM tasks WHERE key=?").bind(b.key).first() });
      case "/api/exec/cancel": return json({ cancelled: !!(await coord.db.prepare("UPDATE tasks SET status='cancelled' WHERE key=? AND status='queued' RETURNING id").bind(b.key).first()) });
      case "/api/exec/lock/acquire": return json(await coord.lockAcquire(b.name, b.owner, +b.ttl || 120));
      case "/api/exec/lock/renew": return json(await coord.lockRenew(b.name, b.owner, b.fence, +b.ttl || 120));
      case "/api/exec/lock/release": return json(await coord.lockRelease(b.name, b.owner, b.fence));
      case "/api/admin/retry": if (!isAdmin) return json({ error: "unauthorized" }, 401); await coord.db.prepare("UPDATE tasks SET status='queued',attempts=0,error=NULL,run_after=0 WHERE id=? AND status='dead'").bind(b.task).run(); return json({ ok: true });
    }
    return json({ error: "not_found" }, 404);
  } catch (e) {
    if (e instanceof Fenced) return json({ error: "fenced", detail: e.message }, 409);
    if (e.status) return json({ error: e.message }, e.status);
    return json({ error: "interno", detail: String(e.message).slice(0, 120) }, 500);
  }
}
