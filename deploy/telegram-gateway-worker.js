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
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;
    const allowed = [`/bot${env.BOT_TOKEN}/`, `/file/bot${env.BOT_TOKEN}/`];
    if (!env.BOT_TOKEN || !allowed.some((p) => path.startsWith(p))) {
      return new Response("Not found", { status: 404 });
    }
    const target = new URL(path + url.search, "https://api.telegram.org");
    return fetch(new Request(target, request));
  },
};
