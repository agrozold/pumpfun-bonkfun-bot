# AGENTS Guidelines for This Repository

This repository contains a Solana trading bot for pump.fun and letsbonk.fun platforms. When working on the project interactively with an agent (e.g. the Codex CLI) please follow the guidelines below for safe development and testing.

## 1. Use Learning Examples for Testing

* **Always test with learning examples first** in `learning-examples/` before modifying the main bot.
* **Do _not_ run the main bot with real funds** during agent development sessions.
* **Test all changes** using manual buy/sell scripts with minimal amounts before production use.
* **Use testnet** or paper trading when available to validate logic.

## 2. Keep Dependencies in Sync

If you add or update dependencies:

1. Use `uv add <package>` to add new dependencies.
2. The `uv.lock` file will be automatically updated.
3. Restart any running bots after dependency changes.
4. Verify compatibility with Python 3.9+ as specified in the project.

## 3. Coding Conventions

* Follow Ruff linting rules defined in `pyproject.toml`.
* Use Google-style docstrings for functions and classes.
* Include type hints for all public functions.
* Use the centralized logger: `from src.utils.logger import get_logger`.
* Keep line length to 88 characters (auto-formatted).
* Use double quotes for strings.

## 4. Code Quality Checks

Before completing any task, run these quality checks:

| Command                 | Purpose                                    |
| ----------------------- | ------------------------------------------ |
| `ruff format`           | Format code to project standards          |
| `ruff check`            | Run linting checks                        |
| `ruff check --fix`      | Auto-fix linting issues where possible    |

## 5. Testing Workflow

Test changes progressively:

1. **Unit testing**: Use individual learning examples
   ```bash
   uv run learning-examples/fetch_price.py
   ```

2. **Integration testing**: Test specific listeners
   ```bash
   uv run learning-examples/listen-new-tokens/listen_logsubscribe.py
   ```

3. **Configuration testing**: Validate YAML configs before running
   ```bash
   # Check syntax and required fields manually
   ```

4. **Dry run**: Use minimal amounts and conservative settings first

## 6. Environment Configuration

Never commit sensitive data:

* Keep private keys in `.env` file (git-ignored).
* Use separate `.env` files for development and production.
* Required environment variables:
  ```env
  SOLANA_RPC_WEBSOCKET=wss://...
  SOLANA_RPC_HTTP=https://...
  PRIVATE_KEY=your_private_key_here
  ```

## 7. Bot Configuration Best Practices

* Edit YAML files in `bots/` directory for bot instances.
* Start with conservative settings:
  - Low `buy_amount`
  - High `min_sol_balance`
  - Strict filters
* Test one bot instance at a time during development.
* Monitor logs in `logs/` directory for debugging.

## 8. Platform-Specific Development

When adding features:

* Check platform compatibility (`pump_fun` vs `lets_bonk`).
* Test with both platforms if changes affect core logic.
* Update platform-specific implementations in `src/platforms/`.
* Verify IDL files match the on-chain programs.

## 9. Safety Reminders

* **Never expose private keys** in code, logs, or commits.
* **Test with minimal amounts** first.
* **Verify transactions** on Solana explorer before scaling up.
* **Monitor rate limits** of your RPC provider.
* **Keep logs** for audit and debugging purposes.

## 10. Useful Commands Recap

| Command                                            | Purpose                           |
| -------------------------------------------------- | --------------------------------- |
| `uv sync`                                          | Install/update dependencies       |
| `source .venv/bin/activate`                       | Activate virtual environment      |
| `uv pip install -e .`                              | Install bot as editable package   |
| `pump_bot`                                         | Run the main bot                  |
| `uv run learning-examples/manual_buy.py`          | Test manual buy                   |
| `uv run learning-examples/manual_sell.py`         | Test manual sell                  |

---

Following these practices ensures safe development, prevents accidental trades, and maintains code quality. Always prioritize testing and security when working with trading bots.