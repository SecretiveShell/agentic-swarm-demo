# Agentic Swarm Demo

This is a quick-and-dirty experimental demo of swarm agents using the Codex CLI
and Docker. It is intended for exploration, not production use: expect rough
edges, validate the agents' output, and review the security model before using
it with sensitive code or credentials.

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
scheduled task files exist, never exceeding twenty simultaneous containers. The
orchestrator assigns each task before starting a container. Each worker writes a
report and can propose up to two follow-up Markdown files; it never claims,
renames, completes, creates, or schedules tasks itself.

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

Use `--task-dir path/to/tasks` to seed a run from Markdown files:

```text
tasks/
  001-research.md
  002-synthesis.md
```

## Task and proposal lifecycle

Tasks and proposals have separate, orchestrator-owned state machines:

```text
proposal: pending → evaluating → approved | rejected
task:     created → scheduled → assigned → completed | blocked | cancelled
```

A worker writes proposals only to `proposals/pending/`. After a completed task,
a separate evaluator container reads each proposal and writes an `approved` or
`rejected` decision under `evaluations/`. The orchestrator validates that
decision: approved proposals become new task files, move through `created` and
`scheduled`, and may then be assigned; rejected proposals are retained under
`proposals/rejected/` for audit. Evaluators never modify task state themselves.

The shared directory contains `tasks/{created,scheduled,assigned,completed,
blocked,cancelled}`, `proposals/{pending,evaluating,approved,rejected}`,
`evaluations/`, and `reports/`. Private workspaces are under
`/tmp/swarm-run/workspaces`.

`--objective` is written to `shared/objective.md` and included in every
worker's prompt.

## Generate the initial queue from the objective

Omit both `--task` and `--task-dir` to run one bootstrap planner before any
workers start. It reads the objective, writes 3–12 bounded Markdown task files,
and the orchestrator validates, creates, and schedules them. The planner is
named `<agent-id>-planner`; it plans only and does not execute the tasks.

```bash
uv run agentic-swarm-demo \
  --agent-id researcher \
  --workers 6 \
  --run-dir /tmp/swarm-run \
  --objective 'Produce a source-backed comparison of local coding-agent orchestration patterns.' \
  --model your-model \
  --api-base http://host.docker.internal:8001/v1 \
  --context-window 131072 \
  --tui
```

Add `--tui` to display a live terminal dashboard with active workers, their
claimed tasks, elapsed runtime, retirement status, and a recent feed of Codex
tool output from each container.

After all ready work (including accepted follow-up tasks) has retired, a final
`<agent-id>-summarizer` container reads the objective, worker reports, and task
outcomes. It writes the user-facing result to `shared/final.md`; the launcher
prints that path when the run succeeds.
