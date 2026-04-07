# Health AI Assistant V1 Deployment

This deployment keeps nanobot in single-user, single-workspace health mode. Only Caddy is exposed on `80/443`. The nanobot gateway, onboarding service, and WhatsApp bridge stay on the Docker network.

## Target Host

- Hetzner shared vCPU instance class: 2 vCPU / 4 GB RAM / 40 GB SSD
- Current live SKU on April 6, 2026: `CX23`
- If you started from the older `CX22` wording, treat it as the same instance class and confirm the exact SKU when ordering

## Host Setup

1. Provision Ubuntu or Debian on Hetzner.
2. Open only `80/tcp` and `443/tcp` in the Hetzner firewall.
3. Install Docker Engine and the Compose plugin.
4. Clone this repo to the server.
5. Create persistent storage:
   - `~/.nanobot` for config, workspace, sessions, health files, and WhatsApp auth
   - Caddy named volumes for TLS state

## Required Environment

Create a `.env` file next to `docker-compose.yml`:

```env
DOMAIN=health.example.com
MINIMAX_API_KEY=your_minimax_key
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
HEALTH_VAULT_KEY=replace_with_a_long_random_secret
WHATSAPP_BRIDGE_TOKEN=replace_with_a_long_random_secret
```

Set the onboarding base URL so `/onboard` links point to the public domain:

```env
HEALTH_ONBOARDING_BASE_URL=https://health.example.com
HEALTH_TELEGRAM_BOT_URL=https://t.me/your_bot_username
HEALTH_WHATSAPP_CHAT_URL=https://wa.me/your_whatsapp_number_or_entrypoint
```

## Reference Health Config

Use `examples/health/minimax.config.example.json` as `~/.nanobot/config.json` without putting secrets in the file.

Important runtime adjustments:

- `providers.minimax.apiKey` resolves from `MINIMAX_API_KEY`
- `channels.telegram.token` resolves from `TELEGRAM_BOT_TOKEN`
- Telegram is enabled by default in the health example, so the operator path is:
  - set `TELEGRAM_BOT_TOKEN`
  - start the stack
  - tell the end user to open Telegram and tap `/start`
- Point WhatsApp to the bridge service:
  - `channels.whatsapp.bridgeUrl = "ws://whatsapp-bridge:3001"`
  - `channels.whatsapp.bridgeToken = "ENV:WHATSAPP_BRIDGE_TOKEN"`
- Keep the workspace at `~/.nanobot/workspace`
- `ENV:...` placeholders are resolved at load time and preserved on save, so later config refreshes do not write real secrets back into `config.json`

## Bring-Up

1. Run `docker compose build`.
2. Run `docker compose up -d nanobot-gateway onboarding whatsapp-bridge caddy`.
3. Verify edge health with `curl -I https://$DOMAIN/healthz`.
4. Trigger `/onboard` from Telegram or WhatsApp after the gateway is online.

## Channel Handoff In The Web App

If you set `HEALTH_TELEGRAM_BOT_URL` and/or `HEALTH_WHATSAPP_CHAT_URL`, the onboarding page will render direct "Open Telegram" and "Open WhatsApp" actions and will show "Continue in ..." buttons after the form is submitted.

## Notes

- `health/profile.json` stores pseudonymized health state.
- `health/vault.json.enc` stores raw identifiers encrypted with `HEALTH_VAULT_KEY`.
- Re-onboarding is done by issuing a fresh `/onboard` link for the same chat.
