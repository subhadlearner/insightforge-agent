# LangGraph is the outer graph; the Planner is one node

The spec has two orchestrators: a LangGraph state machine and a DeepAgents Planner that spawns researchers via `task`. We made LangGraph the outer graph and the Planner a node in it. Researcher fan-out happens inside the research stage, and the graph advances only once all branches return. Typed graph state is the contract between stages, so "parallel branches finish before synthesis" is enforced by the graph and not left to the Planner's judgement.
