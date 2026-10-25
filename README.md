# ᚱ Ragnarok

**A sovereign, always-on personal AI agent that runs entirely on your machine.**
Every model call goes to a local [Ollama](https://ollama.com) server. No cloud AI, no API keys, no data leaving your laptop.

Persistent encrypted memory · reusable skills · multi-step planning with self-correction · background triggers · you approve every risky action.

> Full research proposal and requirements: [docs/PROPOSAL.md](docs/PROPOSAL.md)

## Quick start

```bash
ollama pull llama3.1:8b          # chat + tool calling
ollama pull nomic-embed-text     # embeddings for memory
pip install -e ".[fast]"         # numpy is optional but speeds up recall

ragnarok doctor                  # check Ollama, models, key file, sandbox
ragnarok                         # interactive chat
ragnarok do "plan my study week and save it as a note"
ragnarok daemon --ui             # always-on: triggers, morning brief, maintenance + web UI
ragnarok ui                      # web UI only: memory, approvals, chat, log
ragnarok memory list|add|edit|delete|export|consolidate|forget|wipe
ragnarok approvals list|grant|reject
ragnarok bench                   # check the non-functional requirements
```

Options: `--role viewer|assistant|admin`, `--offline-embeddings` (no Ollama needed for embeddings).
Env: `OLLAMA_URL`, `RAGNAROK_CHAT_MODEL`, `RAGNAROK_EMBED_MODEL`, `RAGNAROK_HOME` (data dir),
`RAGNAROK_PASSPHRASE` (derive the memory key from a passphrase; otherwise a key file is created),
`IMAP_HOST/IMAP_USER/IMAP_PASSWORD`, `SMTP_HOST/SMTP_USER/SMTP_PASSWORD` (optional email).

### Run it always-on

- **Linux:** `cp deploy/ragnarok.service ~/.config/systemd/user/ && systemctl --user enable --now ragnarok` (capped at 30% CPU).
- **Docker:** `docker build -t ragnarok . && docker run -d -v ragnarok-data:/data -p 127.0.0.1:8765:8765 ragnarok` (reaches host Ollama via `host.docker.internal`).
- The UI link with its per-run token is printed at startup. Notifications go to `data/notifications.jsonl`.

## Architecture

```
            ┌────────────── triggers (cron-like, file, email) ──────────────┐
            ▼                                                                │
 CLI / Web UI ─► Runtime ─► Agent loop ─► skill router ─► Skills ─► sandbox  │
                    │          │  ▲            (intent)      │                │
                    │          │  └─ memory recall            └─ RBAC + approvals
                    │          ▼
                    │     Ollama (localhost only)
                    ├─► Planner/Orchestrator (task graph, reflect, retry, checkpoint)
                    └─► Encrypted memory (episodic log + semantic facts)
```

| Module | Role |
|---|---|
| `llm.py` | Ollama client (chat, tool calling, embeddings) |
| `memory/` | Encrypted SQLite store, recall, dedupe, consolidation, forgetting |
| `crypto.py` | Field-level authenticated encryption with a user-held key |
| `skills.py`, `builtin_skills.py`, `integrations.py` | Skill registry; notes, calendar, email, calculator, sandboxed Python |
| `intent.py` | Embedding-based skill shortlist |
| `agent.py` | Tool-calling loop, memory injection, state checkpointing |
| `planner.py` | Goal → task graph, reflection, retry, skip-on-failure, resume after crash |
| `sandbox.py` | Process rlimits (or Docker with no network) for untrusted code |
| `permissions.py` | Role-based access control and one-shot approvals |
| `triggers.py`, `proactive.py` | Always-on sentinel, daily brief |
| `server.py`, `ui.html` | Local-only web UI and API |
| `bench.py` | NFR benchmarks |

## Requirements traceability

| Req | Where | Verified by |
|---|---|---|
| FR1.1 episodic memory | `memory/store.py` episodes | `test_memory.py` |
| FR1.2 semantic memory | facts + embeddings, `recall()` | `test_recall_finds_relevant`, dedupe test |
| FR1.3 consolidation & forgetting | `consolidate()`, `forget()` | `test_consolidation_and_forgetting` |
| FR2.1 skill registration | `SkillRegistry.register` | `test_agent.py` |
| FR2.2 dynamic tool selection | LLM tool calling + `SkillRouter` | `test_runtime.py` |
| FR2.3 sandboxed execution | `sandbox.py` | `test_planner_sandbox.py` |
| FR3.1 multi-step planning | `planner.make_plan` | `test_goal_plan_executes_end_to_end` |
| FR3.2 integrations | `integrations.py` (notes, calendar, email) | `test_always_on.py` |
| FR3.3 self-correction | `reflect()` + retry loop | `test_retry_then_succeed` |
| FR4.1 always-on triggers | `triggers.py`, `ragnarok daemon` | `test_daemon_brief_and_file_trigger` |
| FR4.2 proactivity | `proactive.morning_brief` | `test_morning_brief_offline` |
| NFR1.1 local-first | only `localhost` Ollama; data in `data/` | by design |
| NFR1.2 encryption at rest | `crypto.py` | `test_data_encrypted_at_rest`, `test_wrong_key_fails` |
| NFR1.3 local inference | no external LLM client exists | by design |
| NFR2.1 < 800 ms | `bench.py` | `test_benchmarks_meet_nfr_targets` |
| NFR2.2 < 30% CPU | `bench.py` (background share) | see note below |
| NFR3.1 graceful degradation | tool errors caught, dependents skipped | `test_failure_skips_dependents...` |
| NFR3.2 recovery < 5 s | `state.json`, plan checkpoints | `test_state_recovery`, bench |
| NFR4.1 explainability | `execution_log.jsonl`, UI log panel | `test_runtime.py` |
| NFR4.2 memory control | CLI, UI, API (view/edit/export/delete/wipe) | `test_ui_api_requires_token...` |
| NFR3.1 bad model output | tool args coerced/validated, Ollama errors become one-line messages | `test_hardening.py` |
| NFR4.3 RBAC + approvals | `permissions.py` | `test_rbac`, `test_approvals_one_shot` |

## Honest limitations

- **Encryption** is field-level (Fernet/AES) on content and embeddings; ids, timestamps and scores stay in plaintext. Use full-disk encryption too. Swap in SQLCipher if you need whole-file encryption.
- **Process sandbox** limits CPU, memory, files and processes but does *not* block network access. Use Docker (auto-detected) for untrusted code. `run_python` is disabled for the `assistant` role and needs approval.
- **NFR2.2**: the bench measures the always-on background cost. Model inference load depends on your hardware and model; measure with `ragnarok bench --ollama` plus your system monitor.
- Retrieval latency in the bench uses the offline hash embedder; real embeddings add one Ollama round trip (~tens of ms for `nomic-embed-text`).
- Small local models can pick tools poorly. The skill router and reflection loop reduce this but don't remove it.
- The default test suite uses scripted fake models. `tests/test_live_ollama.py` runs a real end-to-end check automatically when Ollama and the default models are available (`ragnarok doctor` tells you). It has not been run by the author yet.
- Switching embedding models re-embeds all stored facts automatically on next start.

## Development

```bash
pip install -e ".[dev,fast]" && pytest
```
