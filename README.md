## Run with one worker

By default, the command permits one automatically removed Docker container at a
time. The worker gets a private `/workspace` mount and a shared `/shared`
coordination mount. Both are kept beneath `--run-dir` on the host after the
container exits.

```bash
uv run agentic-swarm-demo \
  --agent-id researcher \
  --run-dir /tmp/swarm-run \
  --objective 'Produce a well-supported recommendation for our local agent workflow.' \
  --model your-model \
  --api-base http://host.docker.internal:8001/v1 \
  --context-window 131072 \
  --task 'Inspect the project and write a report.'
```

Build the included Debian-based runner image once:

```bash
docker build --tag agentic-swarm-runner:latest .
```

The Docker image defaults to `agentic-swarm-runner:latest`; all model-provider
settings are required arguments:

```bash
uv run agentic-swarm-demo \
  --agent-id implementer \
  --run-dir /tmp/swarm-run \
  --objective 'Produce a well-supported recommendation for our local agent workflow.' \
  --model your-model \
  --api-base http://host.docker.internal:8001/v1 \
  --context-window 131072 \
  --task 'Implement the requested change and write a report.'
```

## Run a 20-agent task pool

Set `--workers 20` as an upper bound. The launcher starts workers only while
unchecked tasks exist, never exceeding twenty simultaneous containers. Each
worker atomically claims one task from `todo.md`, removes it from the todo list,
completes it, writes a report, appends a summary to `completed.md`, and retires.
If a worker adds follow-up tasks, the launcher fills newly available capacity
until the task board is empty. A lock-backed `task_board.py` helper mediates
concurrent task claims and updates.

```bash
uv run agentic-swarm-demo \
  --agent-id worker \
  --workers 20 \
  --run-dir /tmp/swarm-run \
  --objective 'Produce a well-supported recommendation for our local agent workflow.' \
  --model your-model \
  --api-base http://host.docker.internal:8001/v1 \
  --context-window 131072 \
  --task 'Inspect module A and document its public API.' \
  --task 'Inspect module B and document its public API.'
```

To seed a longer Markdown list, use `--todo-file path/to/todo.md` instead of
repeating `--task`. The shared directory is `/tmp/swarm-run/shared` and contains
`todo.md`, `completed.md`, and `reports/`. Private workspaces are under
`/tmp/swarm-run/workspaces`.

`--objective` is written once to `shared/objective.md`. Every worker reads it
before claiming a task and uses it to guide follow-ups and reports.

Add `--tui` to display a live terminal dashboard with active workers, their
claimed tasks, elapsed runtime, and retirement status.
