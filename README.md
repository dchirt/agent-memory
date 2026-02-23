# agent-memory

# Memory Layer — Agent Instructions

## Overview

The `kiro-memory` MCP server provides persistent, structured, queryable memory across sessions.
It is backed by SQLite with vector embeddings (all-MiniLM-L6-v2) for semantic search and FTS5 for
keyword search. Data lives at `~/.kiro/memory/agent_memory.db`.

## Categories

| Category | Use For |
|---|---|
| corrections | Mistakes the agent made that the user corrected |
| preferences | User style, tooling, and workflow preferences |
| patterns | Recurring code patterns, architectural conventions |
| decisions | Architectural or design decisions with rationale |
| facts | Project facts, team info, environment details |

## When to Store (Implicit Extraction)

During a session, use `memory_propose` (not `memory_store`) when you observe:
- A user correction (category: corrections)
- A stated preference or workflow habit (category: preferences)
- A recurring pattern across tasks (category: patterns)
- An architectural decision with rationale (category: decisions)
- A project/team fact not in steering files (category: facts)

Do NOT propose memories that duplicate information already in steering files.

## When to Query

Use `memory_query` at the start of non-trivial tasks to check for relevant past context:
- Before making architectural decisions
- When the task domain overlaps with past corrections
- When unsure about user preferences for a specific area

Keep queries focused. Use the `category` filter when you know the type.

## Batched Proposal Workflow

1. Throughout a session, call `memory_propose` for each candidate memory
2. At session end (or when the user says "wrap up" / "done"), call `memory_review` to list pending proposals
3. Present the batch to the user in a readable format
4. User accepts/rejects; call `memory_confirm` with the two ID lists

Never call `memory_store` directly for implicitly extracted memories — always go through the proposal flow.
`memory_store` is reserved for explicit user requests ("remember this").

## Source Field

Always populate the `source` field with context about where the memory came from:
- Session date: `session-2026-02-22`
- Ticket: `LCP-40163`
- File: `steering/java.md`

## Performance Notes

- First query after container start takes ~2-3s (model load under emulation)
- Subsequent queries are fast
- The container runs under Rosetta/QEMU (amd64 on ARM) due to sqlite-vec compatibility
