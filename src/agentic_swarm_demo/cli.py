from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import anyio
from rich.live import Live
from rich.table import Table

from .task_board import pending_count


DEFAULT_IMAGE = "agentic-swarm-runner:latest"
PROVIDER_ID = "swarm-provider"
PROVIDER_NAME = "Swarm provider"
WIRE_API = "responses"


@dataclass(frozen=True)
class AgentResult:
    exit_code: int
    reply: str
    workspace: Path


@dataclass
class AgentState:
    agent_id: str
    started_at: float
    started_monotonic: float
    status: str = "starting"
    task: str = ""
    exit_code: int | None = None


def initialize_task_board(
    *, run_dir: Path, objective: str, tasks: list[str], todo_file: Path | None
) -> Path:
    """Create the shared task board once, without overwriting an active run."""
    shared_dir = run_dir.resolve() / "shared"
    todo_path = shared_dir / "todo.md"
    objective_path = shared_dir / "objective.md"
    shared_dir.mkdir(parents=True, exist_ok=True)
    (shared_dir / "reports").mkdir(exist_ok=True)
    (shared_dir / "completed.md").touch(exist_ok=True)
    helper_source = Path(__file__).with_name("task_board.py")
    helper_target = shared_dir / "task_board.py"
    shutil.copyfile(helper_source, helper_target)
    if not objective_path.exists():
        objective_path.write_text(f"# Overall objective\n\n{objective.strip()}\n")

    if todo_path.exists():
        return todo_path
    if todo_file is not None:
        todo_path.write_text(todo_file.read_text())
        return todo_path
    if not tasks:
        raise ValueError("provide at least one --task or a --todo-file for a new run")

    todo_path.write_text("# Todo\n\n" + "\n".join(f"- [ ] {task}" for task in tasks) + "\n")
    return todo_path


async def run_agent(
    *,
    agent_id: str,
    run_dir: Path,
    image: str = DEFAULT_IMAGE,
    model: str,
    api_base: str,
    context_window: int,
    show_output: bool = True,
) -> AgentResult:
    """Run one Codex agent container and retain its private/shared files."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", agent_id):
        raise ValueError("agent_id may contain only letters, numbers, underscores, and hyphens")

    run_dir = run_dir.resolve()
    workspace = run_dir / "workspaces" / agent_id
    shared_dir = run_dir / "shared"
    workspace.mkdir(parents=True, exist_ok=True)
    shared_dir.mkdir(parents=True, exist_ok=True)
    reply_file = workspace / "final.txt"

    coordination = f"""You are worker {agent_id}. Your private working directory is /workspace.

You share /shared with the other workers. First read /shared/objective.md. It is
the durable overall goal for this swarm and takes priority when interpreting,
scoping, or creating todo items. Work on exactly one task, then retire.

Task protocol:
1. Atomically claim one unchecked item with
   `python3 /shared/task_board.py claim --agent-id {agent_id}`.
   It prints the claimed task and removes it from todo.md. If it exits with code 2,
   no work remains, so retire without doing work. Never edit todo.md directly.
2. You do not need to complete the entire claimed task. Make a useful, bounded
   advance and preserve the shared state for the next worker. If work remains,
   add a specific follow-up with `python3 /shared/task_board.py add 'follow-up task'`.
3. Write a concise result report to /shared/reports/{agent_id}.md. State what you
   changed or learned, how it advances the overall objective, what remains, and
   any follow-up task you added. Do not overwrite another worker's report.
4. Append one completed-task summary with
   `python3 /shared/task_board.py complete --agent-id {agent_id} --task 'claimed task'
   --summary 'outcome' --report 'reports/{agent_id}.md'`.
5. Do not modify source files outside /workspace, and never delete or overwrite
   another worker's files.

When your claimed task is complete and the report/summary are written, retire."""

    command = [
        "docker", "run", "--rm",
        "--add-host=host.docker.internal:host-gateway",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--volume", f"{workspace}:/workspace",
        "--volume", f"{shared_dir}:/shared",
        image,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--cd", "/workspace",
        # Docker provides the isolation boundary; nested Bubblewrap is unavailable here.
        "--sandbox", "danger-full-access",
        "--model", model,
        "-c", f'model_provider={PROVIDER_ID!r}',
        "-c", f'model_providers.{PROVIDER_ID}.name={PROVIDER_NAME!r}',
        "-c", f'model_providers.{PROVIDER_ID}.base_url={api_base!r}',
        "-c", f'model_providers.{PROVIDER_ID}.wire_api={WIRE_API!r}',
        "-c", f"model_context_window={context_window}",
        "--output-last-message", "/workspace/final.txt",
        coordination,
    ]

    completed = await anyio.run_process(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if show_output and completed.stdout:
        sys.stdout.buffer.write(completed.stdout)
    if show_output and completed.stderr:
        sys.stderr.buffer.write(completed.stderr)

    return AgentResult(
        exit_code=completed.returncode,
        reply=reply_file.read_text().strip() if reply_file.exists() else "",
        workspace=workspace,
    )


async def run_workers(
    *,
    agent_id: str,
    run_dir: Path,
    workers: int,
    image: str,
    model: str,
    api_base: str,
    context_window: int,
    show_tui: bool,
) -> None:
    """Keep up to ``workers`` task-claiming containers active while work exists."""
    if workers < 1:
        raise ValueError("workers must be at least 1")

    shared_dir = run_dir.resolve() / "shared"
    send, receive = anyio.create_memory_object_stream[tuple[int, int]](workers)
    states: dict[int, AgentState] = {}

    def refresh_claimed_tasks() -> None:
        events_path = shared_dir / "events.jsonl"
        if not events_path.exists():
            return
        for line in events_path.read_text().splitlines():
            try:
                event = json.loads(line)
                timestamp = float(event["timestamp"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            for state in states.values():
                if event.get("agent_id") != state.agent_id or timestamp < state.started_at:
                    continue
                if event.get("event") == "task_claimed":
                    state.task = str(event.get("task", ""))
                    state.status = "working"
                elif event.get("event") == "task_completed":
                    state.status = "completed"

    def dashboard() -> Table:
        refresh_claimed_tasks()
        table = Table(title=f"Agent swarm — {pending_count(shared_dir)} pending tasks")
        table.add_column("Agent")
        table.add_column("Status")
        table.add_column("Elapsed", justify="right")
        table.add_column("Task")
        now = time.monotonic()
        for slot in sorted(states):
            state = states[slot]
            elapsed = now - state.started_monotonic
            status = state.status if state.exit_code is None else f"retired ({state.exit_code})"
            table.add_row(state.agent_id, status, f"{elapsed:.1f}s", state.task or "—")
        return table

    async def live_dashboard() -> None:
        with Live(dashboard(), refresh_per_second=4) as live:
            while True:
                live.update(dashboard())
                await anyio.sleep(0.25)

    async def run_worker(slot: int) -> None:
        slot_id = agent_id if workers == 1 else f"{agent_id}-{slot:02d}"
        states[slot] = AgentState(
            agent_id=slot_id,
            started_at=time.time(),
            started_monotonic=time.monotonic(),
        )
        exit_code = 1
        try:
            result = await run_agent(
                agent_id=slot_id,
                run_dir=run_dir,
                image=image,
                model=model,
                api_base=api_base,
                context_window=context_window,
                show_output=not show_tui,
            )
            exit_code = result.exit_code
            states[slot].exit_code = exit_code
            if not show_tui:
                print(f"[{slot_id}] retired with exit code {exit_code}", file=sys.stderr)
        except Exception as exc:
            states[slot].status = f"error: {exc}"
            states[slot].exit_code = exit_code
            if not show_tui:
                print(f"[{slot_id}] launcher error: {exc}", file=sys.stderr)
        finally:
            await send.send((slot, exit_code))

    async with anyio.create_task_group() as task_group:
        if show_tui:
            task_group.start_soon(live_dashboard)
        available_slots = set(range(1, workers + 1))
        active_slots: set[int] = set()

        while True:
            pending = pending_count(shared_dir)
            while pending > 0 and available_slots:
                slot = available_slots.pop()
                active_slots.add(slot)
                task_group.start_soon(run_worker, slot)
                # Reserve this task for the just-launched worker. The worker removes
                # it from todo.md before doing any work.
                pending -= 1

            if not active_slots:
                task_group.cancel_scope.cancel()
                return

            slot, _exit_code = await receive.receive()
            active_slots.remove(slot)
            available_slots.add(slot)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a task-claiming Codex worker pool in Docker")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument(
        "--objective",
        required=True,
        help="Durable overall goal written to shared/objective.md",
    )
    parser.add_argument(
        "--task",
        action="append",
        default=[],
        help="Initial todo item; repeat this flag to add more items",
    )
    parser.add_argument("--todo-file", type=Path, help="Markdown file used to seed todo.md")
    parser.add_argument("--run-dir", type=Path, default=Path(".agent-runs"))
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--context-window", type=int, required=True)
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Maximum number of simultaneous task-claiming agent containers",
    )
    parser.add_argument("--tui", action="store_true", help="Show a live worker dashboard")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.workers < 1:
        raise SystemExit("--workers must be at least 1")
    try:
        todo_path = initialize_task_board(
            run_dir=args.run_dir,
            objective=args.objective,
            tasks=args.task,
            todo_file=args.todo_file,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Unable to initialize task board: {exc}") from exc
    print(f"task board: {todo_path}")

    anyio.run(
        lambda: run_workers(
            agent_id=args.agent_id,
            run_dir=args.run_dir,
            workers=args.workers,
            image=args.image,
            model=args.model,
            api_base=args.api_base,
            context_window=args.context_window,
            show_tui=args.tui,
        )
    )
