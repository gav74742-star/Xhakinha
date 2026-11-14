// Entrada do Worker (wrangler): junta o núcleo testável (worker.js) com os arquivos estáticos.
import { Coord } from "./core.js";
import { handle } from "./worker.js";
import SCHEMA from "./schema.sql";
import DASH from "../dashboard/index.html";
let ready = false;
async function init(db) { if (ready) return; for (const st of SCHEMA.split(";").map(s => s.trim()).filter(Boolean)) await db.prepare(st).run(); ready = true; }
export default {
  async fetch(req, env) { await init(env.DB); return handle(req, env, new Coord(env.DB), DASH); },
  async scheduled(_e, env) { await init(env.DB); await new Coord(env.DB).reap(); },
};
