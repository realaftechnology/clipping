// Front door for the AI editor hub: a Cloudflare Worker on a free workers.dev address, behind Cloudflare Access.
// Access (Google sign-in, allowlist; service tokens for worker machines) runs BEFORE this code; nobody else gets in.
// This Worker passes each allowed request through a Cloudflare Tunnel (Workers VPC) to the hub on Railway, which has
// no public address. It adds a shared secret so the hub answers only traffic that came through here, and the hub
// re-checks the signed Access token itself on every request.
export default {
  async fetch(request, env) {
    if (!request.headers.get('Cf-Access-Jwt-Assertion')) {
      return new Response('Not approved.', { status: 403 });
    }
    const url = new URL(request.url);
    const headers = new Headers(request.headers);
    headers.set('X-Hub-Gate', env.GATE_SECRET);
    headers.set('X-Forwarded-Host', url.host);
    headers.set('Cf-Connecting-IP', request.headers.get('Cf-Connecting-IP') || '');
    headers.set('Cf-IPCountry', (request.cf && request.cf.country) || request.headers.get('Cf-IPCountry') || '');
    const init = { method: request.method, headers, redirect: 'manual' };
    if (!['GET', 'HEAD'].includes(request.method)) init.body = request.body;
    try {
      return await env.HUB.fetch(`http://${env.HUB_HOST}${url.pathname}${url.search}`, init);
    } catch (e) {
      return new Response(JSON.stringify({ error: 'hub unreachable', temporary: true }), {
        status: 502, headers: { 'Content-Type': 'application/json' } });
    }
  },
};
