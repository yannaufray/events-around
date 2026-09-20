// Worker Cloudflare pour partager les favoris de sortie/index.html entre appareils
// (et avec les personnes à qui on donne le lien du site) et exposer un abonnement
// .ics stable que Google Calendar peut relire automatiquement.
//
// La liste de favoris est une source de vérité UNIQUE et PARTAGÉE dans KV, mais
// réservée aux personnes qui connaissent SYNC_SECRET (un code choisi une fois,
// partagé de vive voix/en privé avec l'unique autre personne concernée — jamais
// écrit dans le repo ni dans la page publique générée par agenda_local.py). Le
// site reste utilisable par tout le monde avec des favoris purement locaux au
// navigateur (localStorage) tant que ce code n'est pas saisi.
//
// Trois routes, toutes protégées par SYNC_SECRET (sauf découverte pure du site) :
//   GET  /favoris?key=…       -> renvoie l'objet complet des favoris partagés
//                                 ({id: {title, when, ...}}), lu par la page au
//                                 chargement pour fusionner avec l'état local
//   POST /favoris              { secret, id, data|null } -> ajoute (data) ou retire
//                                 (data=null) UN SEUL favori (jamais un remplacement
//                                 de toute la liste, pour que les deux personnes
//                                 puissent contribuer sans s'écraser l'une l'autre)
//   GET  /favoris.ics?key=…   -> relit agenda.ics publié, ne garde que les VEVENT
//                                 dont l'UID est dans la liste, et renvoie ce
//                                 sous-ensemble comme calendrier (URL à coller telle
//                                 quelle, avec le code, dans Google Calendar)
//
// Bindings attendus (voir wrangler.toml) :
//   FAVORIS_KV      : KV namespace (stocke l'objet des favoris sous la clé "favoris")
//   AGENDA_ICS_URL  : URL publique de agenda.ics (GitHub Pages)
//   SYNC_SECRET     : code connu des deux seules personnes autorisées — à définir
//                      avec `wrangler secret put SYNC_SECRET`, jamais commité

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders() });
    }
    if (request.method === "GET" && url.pathname === "/favoris") {
      return handleGet(request, env);
    }
    if (request.method === "POST" && url.pathname === "/favoris") {
      return handleToggle(request, env);
    }
    if (request.method === "GET" && url.pathname === "/favoris.ics") {
      return handleIcs(request, env);
    }
    return new Response("Not found", { status: 404 });
  },
};

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

function authorized(env, key) {
  return !!env.SYNC_SECRET && key === env.SYNC_SECRET;
}

async function readFavs(env) {
  const raw = await env.FAVORIS_KV.get("favoris");
  return raw ? JSON.parse(raw) : {};
}

async function handleGet(request, env) {
  const key = new URL(request.url).searchParams.get("key");
  if (!authorized(env, key)) {
    return new Response("Forbidden", { status: 403, headers: corsHeaders() });
  }
  const favs = await readFavs(env);
  return new Response(JSON.stringify(favs), {
    status: 200,
    headers: { ...corsHeaders(), "Content-Type": "application/json" },
  });
}

async function handleToggle(request, env) {
  let body;
  try {
    body = await request.json();
  } catch (err) {
    return new Response("Bad JSON", { status: 400, headers: corsHeaders() });
  }
  if (!authorized(env, body.secret)) {
    return new Response("Forbidden", { status: 403, headers: corsHeaders() });
  }
  if (!body.id || typeof body.id !== "string") {
    return new Response("Missing id", { status: 400, headers: corsHeaders() });
  }
  // lecture-modification-écriture non transactionnelle : suffisant pour un usage
  // perso à faible fréquence, un conflit entre deux clics simultanés est sans
  // conséquence grave (au pire un favori revient au prochain clic).
  const favs = await readFavs(env);
  if (body.data) {
    favs[body.id] = body.data;
  } else {
    delete favs[body.id];
  }
  await env.FAVORIS_KV.put("favoris", JSON.stringify(favs));
  return new Response("OK", { status: 200, headers: corsHeaders() });
}

async function handleIcs(request, env) {
  const key = new URL(request.url).searchParams.get("key");
  if (!authorized(env, key)) {
    return new Response("Forbidden", { status: 403 });
  }
  const favs = await readFavs(env);
  const idSet = new Set(Object.keys(favs));

  const resp = await fetch(env.AGENDA_ICS_URL, { cf: { cacheTtl: 300, cacheEverything: true } });
  if (!resp.ok) {
    return new Response("Erreur de lecture de l'agenda source", { status: 502 });
  }
  const text = await resp.text();

  // chaque VEVENT est délimité par des lignes BEGIN:VEVENT/END:VEVENT non pliées
  // (la ligne UID fait moins de 75 octets, jamais coupée par le repliement RFC5545)
  const blocks = text
    .split("BEGIN:VEVENT")
    .slice(1)
    .map((b) => "BEGIN:VEVENT" + b.split("END:VEVENT")[0] + "END:VEVENT");

  const kept = blocks.filter((b) => {
    const m = b.match(/UID:([^\r\n@]+)@/);
    return m && idSet.has(m[1]);
  });

  const out = [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//agenda-local//FR",
    "X-WR-CALNAME:Mes favoris",
    "CALSCALE:GREGORIAN",
    ...kept,
    "END:VCALENDAR",
    "",
  ].join("\r\n");

  return new Response(out, {
    status: 200,
    headers: {
      "Content-Type": "text/calendar; charset=utf-8",
      "Cache-Control": "public, max-age=1800",
    },
  });
}
