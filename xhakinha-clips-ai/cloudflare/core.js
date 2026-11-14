// Núcleo do coordenador (mesmas regras do Python: lease + fencing, locks, breaker, cotas). Recebe `db` com a API do D1:
// db.prepare(sql).bind(...).first() | .all() | .run().  Escrita crítica = UM statement atômico (UPDATE ... RETURNING).
export const LEASE_S = 300, EXEC_TIMEOUT_S = 180, PREFER_WAIT_S = 600, START_MARGIN_S = 120, PRIORITY_RESERVED = 8;
export class Fenced extends Error {}
export const DEFAULTS = {
  "kaggle-cpu": { session_seconds: 39600, week_seconds: 0, day_seconds: 0, safety: 0.9, reserve_frac: 0.1, known: false },
  "kaggle-gpu": { session_seconds: 39600, week_seconds: 97200, day_seconds: 0, safety: 0.9, reserve_frac: 0.15, known: false, gpu: true },
  colab: { session_seconds: 21600, week_seconds: 0, day_seconds: 0, safety: 0.8, reserve_frac: 0, known: false },
  github: { session_seconds: 1200, week_seconds: 0, day_seconds: 7200, safety: 0.8, reserve_frac: 0, known: false },
  local: { session_seconds: 86400, week_seconds: 0, day_seconds: 0, safety: 1.0, reserve_frac: 0, known: true },
};
const J = (x) => JSON.stringify(x ?? null);
const first = (db, sql, ...a) => db.prepare(sql).bind(...a).first();
const all = async (db, sql, ...a) => (await db.prepare(sql).bind(...a).all()).results;
const run = (db, sql, ...a) => db.prepare(sql).bind(...a).run();

export function periodKeys(now) {
  const d = new Date(now * 1000), day = d.toISOString().slice(0, 10);
  const t = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  const dn = t.getUTCDay() || 7; t.setUTCDate(t.getUTCDate() + 4 - dn);
  const y0 = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
  const wk = Math.ceil(((t - y0) / 86400000 + 1) / 7);
  return { day, week: `${t.getUTCFullYear()}-W${String(wk).padStart(2, "0")}` };
}
export function decide(cfg, usage, est, prio, gpuTask, timeLeft) {
  const safety = cfg.safety ?? 0.85, reserve = prio < PRIORITY_RESERVED ? (cfg.reserve_frac ?? 0) : 0;
  if (timeLeft != null) {
    let cap = timeLeft - START_MARGIN_S; if (cfg.session_seconds) cap = Math.min(cap, cfg.session_seconds * safety);
    if (est > cap) return false;
  }
  for (const [per, key] of [["day", "day_seconds"], ["week", "week_seconds"]]) {
    const lim = cfg[key] || 0; if (!lim) continue;
    if (per === "week" && !gpuTask && cfg.gpu) continue;
    if (est > lim * safety * (1 - reserve) - (usage[per] || 0)) return false;
  }
  return true;
}
export function breakerNext(state, failures, opens, openedAt, now, ok, threshold = 3, base = 120, cap = 3600) {
  const cool = opens ? Math.min(cap, base * 2 ** Math.max(0, opens - 1)) : 0;
  if (ok === true) return ["closed", 0, 0, null, true];
  if (ok === false) { failures += 1; if (state === "half_open" || failures >= threshold) return ["open", failures, opens + 1, now, false]; return [state, failures, opens, openedAt, true]; }
  if (state === "open") return (openedAt != null && now - openedAt >= cool) ? ["half_open", failures, opens, openedAt, true] : ["open", failures, opens, openedAt, false];
  return [state, failures, opens, openedAt, true];
}
export function backoff(attempt, base = 30, cap = 3600) { return Math.min(cap, base * 2 ** Math.max(0, attempt - 1)) * (0.85 + Math.random() * 0.3); }

export class Coord {
  constructor(db, clock = () => Date.now() / 1000) { this.db = db; this.now = clock; }
  async provider(name) {
    let p = await first(this.db, "SELECT * FROM providers WHERE name=?", name);
    if (!p) { await run(this.db, "INSERT OR IGNORE INTO providers(name,config) VALUES(?,?)", name, J(DEFAULTS[name] || DEFAULTS.colab)); p = await first(this.db, "SELECT * FROM providers WHERE name=?", name); }
    p.cfg = JSON.parse(p.config); return p;
  }
  async breaker(prov, ok) {
    const p = await this.provider(prov), now = this.now();
    const [st, fl, op, oa, allow] = breakerNext(p.breaker, p.failures, p.opens, p.opened_at, now, ok);
    if (st !== p.breaker || fl !== p.failures || op !== p.opens)
      await run(this.db, "UPDATE providers SET breaker=?,failures=?,opens=?,opened_at=? WHERE name=?", st, fl, op, oa, prov);
    if (ok == null && p.paused_until > now) return false;
    return allow;
  }
  async usage(prov) {
    const pk = periodKeys(this.now()), out = {};
    for (const per of ["day", "week"]) { const r = await first(this.db, "SELECT used FROM usage WHERE provider=? AND period=? AND unit='seconds'", prov, pk[per]); out[per] = r ? r.used : 0; }
    return out;
  }
  async addUsage(prov, s) {
    if (s <= 0) return; const pk = periodKeys(this.now());
    for (const per of ["day", "week"]) await run(this.db, "INSERT INTO usage(provider,period,unit,used) VALUES(?,?,?,?) ON CONFLICT(provider,period,unit) DO UPDATE SET used=used+excluded.used", prov, pk[per], "seconds", s);
  }
  async register(id, provider, caps = {}, sessionSeconds = null) {
    const now = this.now(); await this.provider(provider);
    await run(this.db, "INSERT INTO executors(id,provider,caps,status,registered,last_seen,session_deadline) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET provider=excluded.provider,caps=excluded.caps,status='active',last_seen=excluded.last_seen,session_deadline=excluded.session_deadline",
      id, provider, J(caps), "active", now, now, sessionSeconds ? now + sessionSeconds : null);
    return { executor: id, lease_s: LEASE_S };
  }
  async heartbeat(id, used = 0) {
    const ex = await first(this.db, "SELECT * FROM executors WHERE id=?", id); if (!ex) throw Object.assign(new Error("executor não registrado"), { status: 404 });
    await run(this.db, "UPDATE executors SET last_seen=?, status='active' WHERE id=?", this.now(), id); await this.addUsage(ex.provider, used);
    return { ok: true, now: this.now() };
  }
  async submit(kind, payload = {}, o = {}) {
    const now = this.now(), id = crypto.randomUUID().replaceAll("-", "").slice(0, 16);
    if (o.key) { const ex = await first(this.db, "SELECT * FROM tasks WHERE key=?", o.key); if (ex) return { ...ex, duplicate: true }; }
    await run(this.db, "INSERT INTO tasks(id,kind,key,payload,priority,needs,prefer,est_seconds,run_after,max_attempts,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
      id, kind, o.key ?? null, J(payload), o.priority ?? 5, J(o.needs || []), J(o.prefer || []), o.est_seconds ?? 60, o.run_after ?? 0, o.max_attempts ?? 6, now, now);
    return first(this.db, "SELECT * FROM tasks WHERE id=?", id);
  }
  async claim(id, timeLeft = null) {
    const now = this.now(), ex = await first(this.db, "SELECT * FROM executors WHERE id=?", id);
    if (!ex || ex.status !== "active") return null;
    const caps = JSON.parse(ex.caps), prov = ex.provider;
    if (!(await this.breaker(prov, null))) return null;
    const p = await this.provider(prov);
    if (timeLeft == null && ex.session_deadline) timeLeft = ex.session_deadline - now;
    const usage = await this.usage(prov);
    for (const t of await all(this.db, "SELECT * FROM tasks WHERE status='queued' AND run_after<=? ORDER BY priority DESC, created ASC LIMIT 50", now)) {
      if (caps.kinds && !caps.kinds.includes(t.kind)) continue;
      const needs = JSON.parse(t.needs), pref = JSON.parse(t.prefer);
      if (needs.includes("gpu") && !caps.gpu) continue;
      if (pref.length && !pref.includes(prov) && now - t.created < PREFER_WAIT_S) continue;
      if (!decide(p.cfg, usage, t.est_seconds, t.priority, needs.includes("gpu"), timeLeft)) continue;
      const row = await first(this.db, "UPDATE tasks SET status='running', owner=?, fence=fence+1, lease_until=?, attempts=attempts+1, updated=? WHERE id=? AND status='queued' RETURNING *", id, now + LEASE_S, now, t.id);
      if (row) { await run(this.db, "INSERT INTO history(task_id,executor,provider,fence,outcome,ts) VALUES(?,?,?,?,?,?)", row.id, id, prov, row.fence, "claimed", now); return row; }
    }
    return null;
  }
  async own(tid, ex, fence) {
    const t = await first(this.db, "SELECT * FROM tasks WHERE id=?", tid);
    if (!t || t.status !== "running" || t.owner !== ex || t.fence !== fence) throw new Fenced(`${ex} não é mais dono de ${tid}`);
    return t;
  }
  async renew(tid, ex, fence) { await this.own(tid, ex, fence); const u = this.now() + LEASE_S; await run(this.db, "UPDATE tasks SET lease_until=?, updated=? WHERE id=?", u, this.now(), tid); return { lease_until: u }; }
  async checkpoint(tid, ex, fence, data) {
    const raw = J(data); if (raw.length > 200000) throw Object.assign(new Error("checkpoint grande demais"), { status: 400 });
    await this.own(tid, ex, fence); await run(this.db, "UPDATE tasks SET checkpoint=?, lease_until=?, updated=? WHERE id=?", raw, this.now() + LEASE_S, this.now(), tid); return { ok: true };
  }
  async complete(tid, ex, fence, result = {}, seconds = 0) {
    await this.own(tid, ex, fence);
    await run(this.db, "UPDATE tasks SET status='done', result=?, lease_until=NULL, error=NULL, updated=? WHERE id=?", J(result), this.now(), tid);
    const prov = (await first(this.db, "SELECT provider FROM executors WHERE id=?", ex)).provider;
    await run(this.db, "INSERT INTO history(task_id,executor,provider,fence,outcome,ts,seconds) VALUES(?,?,?,?,?,?,?)", tid, ex, prov, fence, "done", this.now(), seconds);
    await this.breaker(prov, true); return { ok: true };
  }
  async requeue(tid, why, penalize, delay = 0, outcome = "requeued", ex = null, fence = null) {
    const now = this.now();
    await run(this.db, "UPDATE tasks SET status='queued', owner=NULL, lease_until=NULL, error=?, run_after=?, attempts=CASE WHEN ? THEN attempts ELSE MAX(0,attempts-1) END, updated=? WHERE id=?", String(why).slice(0, 300), now + delay, penalize ? 1 : 0, now, tid);
    await run(this.db, "INSERT INTO history(task_id,executor,fence,outcome,ts,note) VALUES(?,?,?,?,?,?)", tid, ex, fence, outcome, now, String(why).slice(0, 200));
  }
  async fail(tid, ex, fence, error, kind = "transient", retryAfter = null) {
    const t = await this.own(tid, ex, fence), prov = (await first(this.db, "SELECT provider FROM executors WHERE id=?", ex)).provider;
    if (kind === "provider") await this.breaker(prov, false);
    if (kind === "quota") await run(this.db, "UPDATE providers SET paused_until=? WHERE name=?", this.now() + (retryAfter || 3600), prov);
    if (kind === "permanent" || t.attempts >= t.max_attempts) { await run(this.db, "UPDATE tasks SET status='dead', error=?, owner=NULL, lease_until=NULL, updated=? WHERE id=?", String(error).slice(0, 500), this.now(), tid); return { status: "dead" }; }
    await this.requeue(tid, error, true, retryAfter ?? backoff(t.attempts), "failed", ex, fence); return { status: "queued" };
  }
  async release(tid, ex, fence, note = "liberada") { await this.own(tid, ex, fence); await this.requeue(tid, note, false, 0, "released", ex, fence); return { ok: true }; }
  async reap() {
    const now = this.now(), out = { requeued: [], dead: [], lost_executors: [] };
    for (const e of await all(this.db, "SELECT id FROM executors WHERE status='active' AND last_seen<?", now - EXEC_TIMEOUT_S)) { await run(this.db, "UPDATE executors SET status='lost' WHERE id=?", e.id); out.lost_executors.push(e.id); }
    for (const t of await all(this.db, "SELECT * FROM tasks WHERE status='running' AND lease_until<?", now)) {
      if (t.attempts >= t.max_attempts) { await run(this.db, "UPDATE tasks SET status='dead', error='lease expirou', owner=NULL WHERE id=?", t.id); out.dead.push(t.id); }
      else { await this.requeue(t.id, "lease expirou", true, 5, "lease_expired", t.owner, t.fence); out.requeued.push(t.id); }
    }
    await run(this.db, "UPDATE locks SET owner=NULL WHERE owner IS NOT NULL AND lease_until<?", now);
    return out;
  }
  async lockAcquire(name, owner, ttl = 120) {
    const now = this.now(); await run(this.db, "INSERT OR IGNORE INTO locks(name,owner,fence,lease_until) VALUES(?,NULL,0,0)", name);
    const r = await first(this.db, "UPDATE locks SET owner=?, fence=fence+1, lease_until=? WHERE name=? AND (owner IS NULL OR lease_until<?) RETURNING fence", owner, now + ttl, name, now);
    if (r) return { fence: r.fence };
    const cur = await first(this.db, "SELECT * FROM locks WHERE name=?", name);
    if (cur.owner === owner) { await run(this.db, "UPDATE locks SET lease_until=? WHERE name=?", now + ttl, name); return { fence: cur.fence }; }
    return { fence: null };
  }
  async lockRenew(name, owner, fence, ttl = 120) {
    const cur = await first(this.db, "SELECT * FROM locks WHERE name=?", name);
    if (!cur || cur.owner !== owner || cur.fence !== fence || cur.lease_until < this.now()) throw new Fenced(`lock ${name} perdido`);
    await run(this.db, "UPDATE locks SET lease_until=? WHERE name=?", this.now() + ttl, name); return { ok: true };
  }
  async lockRelease(name, owner, fence) { await run(this.db, "UPDATE locks SET owner=NULL, lease_until=0 WHERE name=? AND owner=? AND fence=?", name, owner, fence); return { ok: true }; }
  async state() {
    const now = this.now(), provs = [];
    for (const p of await all(this.db, "SELECT * FROM providers ORDER BY name")) { const cfg = JSON.parse(p.config); provs.push({ name: p.name, breaker: p.breaker, failures: p.failures, paused: p.paused_until > now, usage_s: await this.usage(p.name), config: cfg, uncertain: !cfg.known }); }
    const counts = {}; for (const r of await all(this.db, "SELECT status, count(*) n FROM tasks GROUP BY status")) counts[r.status] = r.n;
    const exs = (await all(this.db, "SELECT id,provider,status,last_seen,session_deadline FROM executors ORDER BY last_seen DESC LIMIT 30")).map(e => ({ ...e, seen_ago_s: Math.round(now - (e.last_seen || now)), time_left_s: e.session_deadline ? Math.round(e.session_deadline - now) : null }));
    return { now, counts, providers: provs, executors: exs,
      tasks: await all(this.db, "SELECT id,kind,status,priority,owner,fence,attempts,lease_until,error,updated FROM tasks WHERE status IN ('running','queued','dead') ORDER BY priority DESC, created LIMIT 60"),
      locks: await all(this.db, "SELECT name,owner,fence,lease_until FROM locks"), events: await all(this.db, "SELECT ts,level,component,message FROM events ORDER BY id DESC LIMIT 40"),
      history: await all(this.db, "SELECT task_id,executor,provider,outcome,ts,seconds,note FROM history ORDER BY id DESC LIMIT 40") };
  }
}
