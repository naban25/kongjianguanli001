# Production deployment configs
#
# Backend: Railway (Dockerfile + railway.json)
# Database: Neon PostgreSQL (managed cloud)
# Frontend: Cloudflare Pages (wrangler.toml)
# LLM: NVIDIA Free Endpoint (already configured)
#
# Prerequisites:
#   1. Neon PostgreSQL instance with its STANDARD postgresql:// connection string
#   2. Railway account (or deploy Docker container anywhere)
#   3. Cloudflare account + API token (for Pages)
