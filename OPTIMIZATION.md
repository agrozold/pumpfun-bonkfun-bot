# Latency Optimization Log

## Overview

Whale copy-trading bot latency optimization — reducing time from whale TX detection to our buy confirmation.

**Baseline (before optimization):** ~2850ms total latency
**Current (after Phase 1 + 5.1 + 3.1):** ~1900ms estimated
**Target:** ~1505ms

| Stage | Baseline | Current | Target |
|-------|----------|---------|--------|
| gRPC catch TX | ~700ms | ~700ms | ~700ms |
| TX parsing | ~650ms | **~1ms** ✅ | ~1ms |
| gRPC stability | RST every 20-40min | **Stable** ✅ | Stable |
| Buy (build+send) | ~1500ms | **~1200ms** ✅ | ~800ms |
| **Total to buy sent** | **~2850ms** | **~1900ms** | **~1505ms** |

---

## Completed Phases

### Phase 1: Local TX Parser (~649ms saved)
**Commit:** `d3167bd` | **Date:** 2026-02-10 | **Status:** ✅ Production

**Problem:** gRPC caught whale TX in ~700ms, then `whale_geyser.py` made HTTP POST to `api.helius.xyz/v0/transactions/` for parsing — adding 650-880ms per TX.

**Solution:** Created `src/monitoring/local_tx_parser.py` (578 lines) — full local swap parser from gRPC protobuf data.

**Key features:**
- Two parsing methods: Pump.fun discriminator (first 8 bytes) + Universal balance diff (any DEX)
- 11 DEX program IDs recognized (pump_fun, pumpswap, jupiter, raydium, orca, meteora, etc.)
- 54-token comprehensive blacklist (stablecoins, LSTs, wrapped assets, infra tokens)
- Address Lookup Tables (ALT) support for extended account keys
- SOL balance change calculation with TX fee correction
- Integrated into `whale_geyser.py` with Helius fallback if local parse returns None

**Result:** Parsing latency dropped from 650-880ms to **1ms** (confirmed in production logs).

---

### Phase 5.1: Bidirectional gRPC Keepalive Ping (stability)
**Commit:** `d3167bd` | **Date:** 2026-02-10 | **Status:** ✅ Production

**Problem:** gRPC stream used unidirectional iterator — sent one SubscribeRequest and closed write-half. PublicNode disconnected with RST_STREAM every 20-40 minutes.

**Solution:** Replaced `iter([request])` with async generator + `asyncio.Queue`. Bidirectional ping/pong:
- Client sends proactive ping every 10s via queue
- Server pings handled with immediate pong response
- RST_STREAM fast reconnect (0.5s instead of exponential backoff)

**Result:** Zero RST_STREAM disconnections after deployment. Continuous ping/pong confirmed in logs.

---

### Phase 3.1: Parallel Jito + RPC Transaction Send (~200-400ms saved)
**Commit:** `fdcf2c5` | **Date:** 2026-02-10 | **Status:** ✅ Production

**Problem:** `build_and_send_transaction()` sent TX sequentially: Jito first, wait for response, then RPC on failure. Jito HTTP response alone takes 200-500ms.

**Solution:** `asyncio.wait(FIRST_COMPLETED)` — fire both Jito and RPC simultaneously, first successful response wins.

**Key features:**
- New method `_parallel_send_jito_rpc()` in `SolanaClient`
- Both channels get identical signed TX (same signature = Solana idempotency)
- Non-retryable errors (0x1775, insufficient funds, slippage) correctly prioritized and propagated to retry loop
- Proper task cleanup: cancel + await pending on success/failure/cancellation
- `use_jito=False` path (sell operations) unchanged — RPC only
- Jito tip included in TX regardless of which channel lands it (negligible cost: 0.00001 SOL)

**Result:** Buy TX submission ~200-400ms faster. Log marker: `[TX-PARALLEL] Sent via JITO/RPC (first/fallback)`.

---

### Phase 3.2: Skip Preflight for Whale Copy (NOT NEEDED)
**Status:** ⏭️ Skipped — already implemented

**Analysis:** Whale copy path uses `_buy_any_dex(jupiter_first=True)` → `FallbackSeller.buy_via_jupiter()` which already has `skip_preflight=True`. No change needed.

---

## Pending Phases

### Phase 2: Parallel gRPC + Webhook with Deduplication
**Status:** 🔲 Planned | **Risk:** Medium | **Benefit:** Reliability

Run both gRPC and Webhook receivers simultaneously. First to catch whale TX triggers buy. Deduplication by TX signature via `SignalDedup` class. If gRPC has RST_STREAM during whale buy — webhook catches it.

**Files:** `src/monitoring/signal_dedup.py` (new), `src/trading/universal_trader.py`

### Phase 5.3: Watchdog
**Status:** 🔲 Planned | **Risk:** Zero | **Benefit:** Monitoring

Monitor that at least one channel (gRPC or Webhook) receives data. Alarm if both silent > 5 minutes.

**Files:** `src/monitoring/watchdog.py` (new)

### Phase 4: Real-time Price Stream (SL/TP)
**Status:** 🔲 Planned | **Risk:** Medium | **Benefit:** Real-time SL/TP

Replace 1-second polling with gRPC account subscriptions for bonding curve / pool accounts. Instant price updates (~400ms gRPC latency) instead of 1s polling interval.

**Files:** `src/monitoring/price_stream.py` (new)

### Phase 3.3: Pre-fetch Jupiter Quote
**Status:** 🔲 Planned | **Risk:** Medium | **Benefit:** ~300ms

Start Jupiter quote request in parallel with DexScreener check, using token mint from gRPC data.

**Files:** `src/trading/universal_trader.py`

---

## Backup Files (VPS)

| Backup | Description |
|--------|-------------|
| `src/monitoring/whale_geyser.py.bak.20260210_152815` | Before Phase 1 |
| `src/monitoring/whale_geyser.py.bak.20260210_154723` | Before Phase 5.1 |
| `src/core/client.py.bak.20260210_161256` | Before Phase 3.1 |
