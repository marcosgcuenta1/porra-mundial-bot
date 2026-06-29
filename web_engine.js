// Motor de puntuación = el de la WEB de César, no uno propio.
// El bot le pasa por stdin {porras, fixtures} (los mismos datos que ya baja de Supabase y
// de la API). Este script descarga el HTML EN VIVO de la web, ejecuta SU calcPuntos sobre
// esos datos y devuelve por stdout la clasificación que produce la web (por persona).
// Si la web no está accesible o su motor falla, sale con código != 0 y el bot reintenta.
const fs = require('fs');
const vm = require('vm');

const WEB_URL = process.env.WEB_URL || 'https://cesaresteban.github.io/NFQ-WORLD-CUP/';

(async () => {
  let input = '';
  try { input = fs.readFileSync(0, 'utf8'); } catch (e) { console.error('sin stdin'); process.exit(2); }
  const { porras, fixtures } = JSON.parse(input);

  // 1) Bajar el motor real de la web.
  let html;
  try {
    const resp = await fetch(WEB_URL, { headers: { 'User-Agent': 'mundial-bot' } });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    html = await resp.text();
  } catch (e) {
    console.error('No pude bajar la web: ' + e.message);
    process.exit(3);
  }

  // 2) Extraer el <script> grande y quitar el arranque automático (red/DOM).
  const m = html.match(/<script>([\s\S]*)<\/script>\s*<\/body>/);
  if (!m) { console.error('No encuentro el script de la web'); process.exit(4); }
  const script = m[1].replace(/\(function init\(\)\{[\s\S]*?\}\)\(\);\s*$/, '');

  // 3) Sandbox: DOM y red como no-ops para que el script cargue sin navegador.
  const sink = new Proxy(function () {}, {
    get: (t, p) => (p === Symbol.toPrimitive ? () => '' : sink), apply: () => sink, set: () => true,
  });
  const documentStub = {
    getElementById: () => sink, querySelector: () => sink, querySelectorAll: () => [],
    createElement: () => sink, addEventListener: () => {}, body: sink, documentElement: sink,
  };
  const ctx = {
    console, Date, Math, JSON, parseInt, parseFloat, isNaN, String, Number, Array, Object, RegExp,
    setTimeout: () => 0, setInterval: () => 0, clearInterval: () => {}, clearTimeout: () => {},
    document: documentStub, localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    fetch: () => Promise.resolve({ json: () => Promise.resolve({}), text: () => Promise.resolve('') }),
    location: { hash: '' }, addEventListener: () => {}, removeEventListener: () => {},
  };
  ctx.window = ctx; ctx.globalThis = ctx;
  vm.createContext(ctx);

  try {
    vm.runInContext(script, ctx, { filename: 'nfq_web.js' });
    ctx.PORRAS = porras;
    ctx.__fixtures = fixtures;
    // Ejecutar el motor de la web tal cual: construye los resultados y puntúa.
    vm.runInContext('processFixtures(__fixtures);', ctx);
  } catch (e) {
    console.error('Error ejecutando el motor de la web: ' + e.message);
    process.exit(5);
  }

  const out = (ctx.PORRAS || []).map(p => ({
    id: p.id,
    grupos: p.pts ? p.pts.grupos : 0,
    ff: p.pts ? p.pts.ff : 0,
    indiv: p.pts ? p.pts.indiv : 0,
    total: p.pts ? p.pts.total : 0,
  }));
  process.stdout.write(JSON.stringify(out));
})();
