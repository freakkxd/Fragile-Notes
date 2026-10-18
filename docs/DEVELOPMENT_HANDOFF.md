# Development Handoff — Fragile Notes

## A. State
- Verified source baseline: c91cada (feat(ui-settings): unify LLM Settings UI with scoped primitives and MenuSelect)
- Current version: 0.5.8
- Target version: 0.5.9 (not changed)
- Settings UI: visually accepted by user
- Broader app UI redesign remains pending

## B. What’s Implemented
- Canonical provider kind serde names + legacy aliases
- Ollama discovery/model selection
- Composer and Settings primitives/MenuSelect (scoped tokens, accessible combobox)
- Backend privacy/profile/generation checks
- Request cancellation and race protection
- Credential consistency/compensation
- OpenRouter PKCE + session cloud consent
- Perplexity Agent API + consolidated Sources list

## C. Last Verification (REPORTED)
- Frontend: 102 passed ×3
- Typecheck/build: PASS
- Rust: 601 passed / 0 failed / 6 ignored
- Fresh release executable built (embedded frontend)
- Settings UI accepted

## D. Open Questions
- OpenRouter real login/disconnect E2E not confirmed
- Perplexity live response with sources not confirmed
- ChatGPT subscription integration: BLOCKED/eligibility unresolved (not claimed implemented)
- Gemini subscription access: not implemented
- F-5 adapter routing differences: verify before continuing
- Perplexity Sources list: no inline citation offset linkage
- AppImage packaging blocker (env missing appimage.sh); deb status varies by env
- Further UI redesign must preserve real capabilities, privacy/auth/identifier contracts; no fake controls

## E. Continuation Rules
- Check fresh clone Git state first
- Use real project scripts/dependencies
- Never commit private data/keys to Git
- Do not reuse third-party OAuth client identities
- No mocks presented as live E2E
- No version bump/release until explicit decision
- No secrets in logs/repo
- Build/toolchain differences between PCs must be verified

## F. Setup
- See README for setup/build instructions (referenced as-is)
- Scripts: frontend (dev/build/typecheck/test), src-tauri (cargo test/build)
- No hardcoded absolute paths; avoid temp sandbox paths/hashes as reproducible values
