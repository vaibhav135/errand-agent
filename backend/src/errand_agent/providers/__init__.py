"""Provider contract: the ToolEnv protocol, its request/response models and errors.

Everything that talks to home-service providers (workflow, agent, proactive) depends
on this contract only; `mocks/` implements it, and only `api/` wires a concrete one.
"""
