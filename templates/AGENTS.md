# Tracker Instructions

- Apply `task-folder-workflow` for placement and naming of artifacts under `TASKS_FOLDER`.
- Use `tracker-mcp` to register projects, create tasks, and create any additional task directory. Edit ordinary files inside existing task directories directly.
- Treat task folders as non-repository storage. Do not stage their artifacts in Git.
- Preserve source artifacts in `origin/`; place new deliverables in `update/`, AI action logs in `ai_actions/`, and retrospectives in `retro/`.
- Leave `.protected` to the Tracker service.
- Use subagents concurrently for independent work when this materially reduces latency. On Windows, assign shell, Git, filesystem writes, and tests to one agent and run them sequentially; direct read-only file access may run concurrently.
- A single test runner may use its own workers unless repository instructions prohibit them. Parallelize independent read-only remote tool calls that do not share mutable state.
- Respect `agents.max_concurrent_threads_per_session` for spawned-agent threads.
