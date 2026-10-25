# Ragnarok

**A sovereign, always-on personal AI agent: privacy-preserving autonomous workflows with persistent memory, running fully local on an Ollama stack.**

> No cloud AI service. No API key. Nothing leaves your machine.

---

## Context

Commercial AI assistants are cloud-dependent and centralized. Personal data, conversation context and workflow habits are collected by third-party providers. Most are stateless or limited to a context window, so they cannot personalize over the long term and behave as reactive chatbots instead of proactive agents that carry out multi-step tasks.

Small open models now run on an ordinary laptop through [Ollama](https://ollama.com), which serves chat, embeddings and tool calling on a local address. A capable agent can live entirely on the user's own machine.

## Problem Statement

There is a gap between agent autonomy and user data sovereignty. Today's assistants fail on three fronts:

1. **Loss of data sovereignty:** users don't own or control the data that personalizes their assistant.
2. **Contextual amnesia:** no robust persistent memory, so preferences, history and project state must be re-explained.
3. **Siloed, ephemeral execution:** isolated single-turn interactions, with no reusable skill framework or orchestration across real workflows (email triage → calendar scheduling → code generation).

## Research Objective

Design, build and evaluate a local, privacy-preserving, always-on personal AI agent where **every model call goes to a local Ollama instance**. It must keep all data on the user's machine, remember past interactions, use reusable skills and carry out multi-step tasks on its own.

---

## Functional Requirements

### FR1: Persistent Memory
| ID | Requirement |
|----|-------------|
| FR1.1 | **Episodic memory:** chronological log of interactions, tasks and events, enabling recall of specific past occurrences. |
| FR1.2 | **Semantic memory:** extract, store and retrieve preferences and facts in a structured form (vector embeddings or knowledge graph). |
| FR1.3 | **Consolidation & forgetting:** consolidate working memory into long-term storage and prune obsolete or irrelevant data. |

### FR2: Modular Skills and Tools
| ID | Requirement |
|----|-------------|
| FR2.1 | **Skill registration:** define, register and manage reusable skills (Python scripts, API wrappers, local CLI commands) through a standard interface. |
| FR2.2 | **Dynamic tool selection:** analyze intent, choose the right combination of skills and generate their parameters. |
| FR2.3 | **Sandboxed execution:** run skills in isolated environments so bad code can't compromise the host. |

### FR3: Autonomous Workflow Orchestration
| ID | Requirement |
|----|-------------|
| FR3.1 | **Multi-step planning:** decompose ambiguous goals into an execution graph of sub-tasks. |
| FR3.2 | **Cross-application integration:** interact with user-approved apps (email, IDEs, project management). |
| FR3.3 | **Self-correction and reflection:** evaluate skill output against expected criteria, retry or re-plan on failure. |

### FR4: Proactive and Continuous Interaction
| ID | Requirement |
|----|-------------|
| FR4.1 | **Always-on processing:** monitor user-defined triggers (incoming email, file changes, cron) and start tasks unprompted. |
| FR4.2 | **Contextual proactivity:** use memory and current context to suggest actions or surface relevant information. |

## Non-Functional Requirements

### NFR1: Privacy and Data Sovereignty
- **NFR1.1 Local-first:** all raw data, memory stores and execution logs stay on the user's hardware. Zero raw data goes to external servers.
- **NFR1.2 Encrypted memory at rest:** the memory database is encrypted with user-held keys.
- **NFR1.3 Fully local inference:** all reasoning and embedding calls are served by local Ollama models; no external LLM API. Since no prompt leaves the machine, no PII-redaction pipeline is needed.

### NFR2: Performance and Resource Efficiency
- **NFR2.1 Responsiveness:** under 800 ms for memory retrieval and intent classification.
- **NFR2.2 Resource footprint:** background monitoring and local inference use no more than 30% of baseline CPU/GPU.

### NFR3: Reliability and Fault Tolerance
- **NFR3.1 Graceful degradation:** if a tool or API fails, halt that sub-task, notify the user and continue independent sub-tasks.
- **NFR3.2 State recovery:** restore exact operational state (incomplete task graphs, working memory) within 5 seconds of reboot after a crash or power loss.

### NFR4: Usability and Transparency
- **NFR4.1 Explainability:** a transparent execution log / chain of thought showing why a tool was chosen and how a decision was made.
- **NFR4.2 User control over memory:** view, edit, export and permanently delete specific memories.
- **NFR4.3 Permission granularity:** strict role-based access control with explicit just-in-time approval before sensitive or high-risk actions (sending email, deleting files).

---

## Conclusion

Ragnarok proposes a shift from centralized, stateless chatbots to decentralized, stateful, sovereign agents. By running every model, memory store and skill locally through Ollama, and meeting the requirements above, this project aims to establish a foundation for an always-on personal assistant that boosts productivity through persistent memory and autonomous workflows, without giving up privacy or user control.

## Status

Research proposal stage. Architecture, implementation and evaluation to follow.
