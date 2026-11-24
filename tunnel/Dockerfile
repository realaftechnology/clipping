# The hub's tunnel connector on Railway: connects out to Cloudflare (TUNNEL_TOKEN) and carries the front-door
# Worker's traffic to hub.railway.internal:8080. Nothing listens publicly.
FROM cloudflare/cloudflared:2026.10.2
CMD ["tunnel", "--no-autoupdate", "run"]
