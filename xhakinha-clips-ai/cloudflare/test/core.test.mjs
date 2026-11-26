// Teste do núcleo JS contra SQLite REAL (node:sqlite) com uma casca que imita a API do D1.
// REAL: lógica/SQL do coordenador. NÃO REAL: o D1 da Cloudflare em si (depende de conta/deploy).
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import test from "node:test";
import assert from "node:assert/strict";
import { Coord, Fenced, LEASE_S } from "../core.js";
import { handle } from "../worker.js";

function d1() {
  const db = new DatabaseSync(":memory:");
  db.exec(readFileSync(new URL("../schema.sql", import.meta.url), "utf8"));
  return { prepare(sql) { const st = db.prepare(sql); return { bind(...a) { return {
    first: async () => st.get(...a) ?? null, all: async () => ({ results: st.all(...a) }), run: async () => st.run(...a) }; } }; } };
}
const mk = () => { const c = { t: 1_800_000_000 }; const co = new Coord(d1(), () => c.t); co.c = c; return co; };

test("claim atômico: 6 executores, 1 tarefa => 1 vencedor", async () => {
  const co = mk(); for (let i = 0; i < 6; i++) await co.register("e" + i, "colab", {}, 21600);
  await co.submit("x", {}, { key: "k" });
  const r = await Promise.all([0, 1, 2, 3, 4, 5].map(i => co.claim("e" + i)));
  assert.equal(r.filter(Boolean).length, 1);
});
test("lease expira, fencing barra executor antigo, checkpoint sobrevive", async () => {
  const co = mk(); await co.register("a", "kaggle-cpu", {}, 40000); await co.register("b", "colab", {}, 21600);
  await co.submit("x", {}, { key: "k" }); const t1 = await co.claim("a");
  await co.checkpoint(t1.id, "a", t1.fence, { n: 3 });
  co.c.t += LEASE_S + 10; assert.deepEqual((await co.reap()).requeued, [t1.id]);
  co.c.t += 10; await co.heartbeat("b"); const t2 = await co.claim("b");
  assert.equal(t2.fence, t1.fence + 1); assert.equal(JSON.parse(t2.checkpoint).n, 3);
  await assert.rejects(co.complete(t1.id, "a", t1.fence, {}), Fenced);
  await co.complete(t2.id, "b", t2.fence, { ok: 1 });
});
test("sem GPU, cota esgotada e circuit breaker", async () => {
  const co = mk(); await co.register("cpu", "kaggle-cpu", { gpu: false }, 40000); await co.register("gpu", "kaggle-gpu", { gpu: true }, 40000);
  await co.submit("g", {}, { key: "g", needs: ["gpu"] }); assert.equal(await co.claim("cpu"), null); assert.ok(await co.claim("gpu"));
  await co.register("gh", "github", {}, 20000); await co.heartbeat("gh", 7000);
  await co.submit("q", {}, { key: "q", est_seconds: 600 }); assert.equal(await co.claim("gh"), null);   // 7000+600 > 7200*0.8
  const k = mk(); await k.register("k", "kaggle-cpu", {}, 40000);
  for (let i = 0; i < 3; i++) { await k.submit("x", {}, { key: "t" + i }); const t = await k.claim("k"); await k.fail(t.id, "k", t.fence, "503", "provider", 0); }
  await k.submit("y", {}, { key: "y" }); assert.equal(await k.claim("k"), null);
});
test("lock distribuído com fencing", async () => {
  const co = mk(); const a = await co.lockAcquire("brain", "A", 100); assert.equal(a.fence, 1);
  assert.equal((await co.lockAcquire("brain", "B", 100)).fence, null);
  co.c.t += 101; const b = await co.lockAcquire("brain", "B", 100); assert.equal(b.fence, 2);
  await assert.rejects(co.lockRenew("brain", "A", 1), Fenced);
});
test("HTTP: auth, X-Requested-With, 409 em fenced, painel", async () => {
  const co = mk(), env = { EXEC_TOKEN: "E".repeat(24), ADMIN_TOKEN: "A".repeat(24), DB: co.db };
  const call = (p, body, tok = env.EXEC_TOKEN, xr = "XHK") => handle(new Request("http://x" + p, { method: body ? "POST" : "GET", body: body ? JSON.stringify(body) : undefined, headers: { authorization: "Bearer " + tok, ...(xr ? { "x-requested-with": xr } : {}) } }), env, co);
  const lockBody = { name: "kaggle-launch", owner: "github-action", ttl: 60 };
  assert.equal((await (await call("/api/state")).json()).launch_enabled, false);
  assert.equal((await call("/api/admin/kaggle-launch", { enabled: true }, env.EXEC_TOKEN)).status, 401);
  assert.equal((await call("/api/admin/kaggle-launch", { enabled: "true" }, env.ADMIN_TOKEN)).status, 400);
  assert.equal((await call("/api/admin/kaggle-launch", { enabled: true }, env.ADMIN_TOKEN)).status, 200);
  assert.equal((await (await call("/api/state")).json()).launch_enabled, true);
  assert.equal((await call("/api/admin/kaggle-launch", { enabled: false }, env.ADMIN_TOKEN)).status, 200);
  assert.equal((await (await call("/api/state")).json()).launch_enabled, false);
  for (const op of ["acquire", "renew", "release"]) {
    assert.equal((await call(`/api/exec/lock/${op}`, lockBody, env.EXEC_TOKEN)).status, 401);
  }
  const acquired = await call("/api/exec/lock/acquire", lockBody, env.ADMIN_TOKEN);
  assert.equal(acquired.status, 200);
  const lock = await acquired.json();
  assert.equal(typeof lock.fence, "number");
  const fencedBody = { ...lockBody, fence: lock.fence };
  assert.equal((await call("/api/exec/lock/renew", fencedBody, env.ADMIN_TOKEN)).status, 200);
  assert.equal((await call("/api/exec/lock/release", fencedBody, env.ADMIN_TOKEN)).status, 200);
  assert.equal((await call("/api/state", null, "ruim")).status, 401); assert.equal((await call("/api/state", null, env.EXEC_TOKEN, "")).status, 401);
  await call("/api/exec/register", { executor: "e", provider: "local", caps: {}, session_seconds: 3600 });
  await call("/api/exec/submit", { kind: "x", payload: {}, key: "k" });
  const t = (await (await call("/api/exec/claim", { executor: "e" })).json()).task;
  assert.equal((await call("/api/exec/complete", { task: t.id, executor: "e", fence: t.fence, result: {} })).status, 200);
  assert.equal((await call("/api/exec/complete", { task: t.id, executor: "e", fence: t.fence, result: {} })).status, 409);
  assert.equal((await (await call("/api/state")).json()).counts.done, 1);
});
