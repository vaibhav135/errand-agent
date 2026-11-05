"""Deterministic workflow: state machine, approval gate, booking executor, limits.

Must not import errand_agent.agent or errand_agent.api: the LLM proposes, the workflow decides."""
