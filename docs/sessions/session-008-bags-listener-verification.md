# Session 008: Bags.fm Listener Verification and Fixes

**Date:** 2026-01-20
**Priority:** HIGH
**Status:** COMPLETED

## Objective
Verify and fix the Bags.fm (Meteora DBC) listener implementation.

## Problems Found and Fixed

### 1. WSS Endpoint Using Public Solana (CRITICAL)
- Before: wss://api.mainnet-beta.solana.com (slow, unreliable)
- After: wss://solana-mainnet.core.chainstack.com/... (fast, reliable)

### 2. Token Detection Too Broad
- Before: Matched any log with "InitializeVirtualPool" (including swaps)
- After: Only matches exact "Instruction: InitializeVirtualPoolWithSplToken"

### 3. IDL Missing Discriminator Fields
- Added discriminator fields to idl/bags.json for proper IDL parsing

## Files Modified
1. .env - Updated WSS endpoint to Chainstack
2. src/monitoring/bags_logs_listener.py - Fixed token detection logic
3. idl/bags.json - Added discriminator fields

## Verification
- Chainstack WSS connection works
- Swap transactions correctly identified
- Token creation detection now precise
