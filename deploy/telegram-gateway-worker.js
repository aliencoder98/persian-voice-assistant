// Telegram Bot API gateway as a free Cloudflare Worker (no VPS needed).
// Your server cannot reach api.telegram.org, but it can reach Cloudflare. The Worker forwards
// /bot<TOKEN>/... and /file/bot<TOKEN>/... to Telegram, and ONLY for your own bot token.
//
// Setup (Cloudflare dashboard > Workers & Pages > Create > Hello World > Edit code):
//   1. Paste this file, Deploy.
//   2. Settings > Variables and Secrets > add a secret named BOT_TOKEN = your Telegram bot token.
//   3. Settings > Domains & Routes: add a custom domain such as tg.example.com
//      (the workers.dev address is often unreachable from Iran; a custom domain on Cloudflare works better).
//   4. In the server's .env:  TELEGRAM_API_BASE_URL=https://tg.example.com
//
// Under a path of a site that is already on Cloudflare (orange cloud), e.g. https://example.com/tg:
//   - instead of a custom domain, add a ROUTE  example.com/tg/*  to this Worker (Settings > Domains & Routes > Route),
//   - add a plain variable PATH_PREFIX = /tg,
//   - and set TELEGRAM_API_BASE_URL=https://example.com/tg
// This only works if requests to example.com really pass through Cloudflare. A /tg location on your own
// nginx would just call Telegram from the same blocked server and fail.
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const prefix = env.PATH_PREFIX || "";
    // python-telegram-bot percent-encodes file URLs (the ":" in the token becomes "%3A"): compare the decoded path.
    let decoded;
    try {
      decoded = decodeURIComponent(url.pathname);
    } catch (e) {
      return new Response("Not found", { status: 404 });
    }
    const path = prefix && decoded.startsWith(prefix + "/") ? decoded.slice(prefix.length) : decoded;
    const allowed = [`/bot${env.BOT_TOKEN}/`, `/file/bot${env.BOT_TOKEN}/`];
    if (!env.BOT_TOKEN || !allowed.some((p) => path.startsWith(p))) {
      return new Response("Not found", { status: 404 });
    }
    const target = new URL(path + url.search, "https://api.telegram.org");  // Telegram accepts the plain form
    return fetch(new Request(target, request));
  },
};
