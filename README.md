# arb

Cross-exchange cryptocurrency arbitrage. v0.5.0: no live trading.

    uv sync
    cp .env.example .env
    docker compose up --build

Collect then analyse (with real fees):

    docker compose up -d postgres
    PERSIST=true EPOCH=my-run uv run arb
    uv run arb-report --epoch my-run --fee coinbase=0.006 --fee okx=0.001

Coins on both venues, busiest first (one collector per coin; see compose.yaml):

    uv run arb-pairs

Coinbase fee tier (read-only CDP API key, ECDSA, View permission only):

    uv run arb-fees-cb --key-file ~/cdp_api_key.json

Deploy (e.g. a small VPS in Singapore; set PERSIST=true and an EPOCH in .env):

    cp .env.example .env
    docker compose up -d --build
    docker compose logs -f arb
    docker compose exec arb arb-report --epoch run1 --fee coinbase=0.006 --fee okx=0.001
    docker compose stop    # graceful: records open episodes, flushes the backlog

Test:

    uv sync
    uv run pytest
    uv run ruff check
    uv run pyright
