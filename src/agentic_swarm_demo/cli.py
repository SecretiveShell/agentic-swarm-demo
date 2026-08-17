from __future__ import annotations

import argparse
from collections import deque
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import anyio
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text
from rich.table import Table

from .task_queue import (
    Assignment,
    Proposal,
    apply_evaluation,
    assign,
    claim_proposal,
    evaluation_decision,
    evaluation_path,
    finalize,
    ingest_bootstrap_tasks,
    initialize,
    proposal_counts,
    ready_count,
    task_counts,
)


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
    activity: str = "waiting for Codex output"
    exit_code: int | None = None


STATUS_STYLES = {
    "starting": "yellow",
    "working": "cyan",
    "evaluating": "magenta",
    "completed": "green",
    "blocked": "red",
}


def initialize_task_queue(
    *,
    run_dir: Path,
    objective: str,
    tasks: list[str],
    task_dir: Path | None,
    allow_empty: bool = False,
) -> Path:
    """Create an orchestrator-owned Markdown task queue once."""
    shared_dir = run_dir.resolve() / "shared"
    shared_dir.mkdir(parents=True, exist_ok=True)
    initialize(shared_dir, objective, tasks, task_dir, allow_empty=allow_empty)
    return shared_dir / "tasks"


async def run_agent(
    *,
    agent_id: str,
    run_dir: Path,
    image: str = DEFAULT_IMAGE,
    model: str,
    api_base: str,
    context_window: int,
    assignment: Assignment | None = None,
    prompt: str | None = None,
    show_output: bool = True,
    on_output: Callable[[str], None] | None = None,
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

    if prompt is None:
        if assignment is None:
            raise ValueError("run_agent requires an assignment or an explicit prompt")
        prompt = f"""You are worker {agent_id}. Your private working directory is /workspace.

You share /shared with other workers. Read /shared/objective.md, then work only
on the task assigned below. The orchestrator owns task assignment and task state.

Assigned task:
---
{assignment.body}
---

Protocol:
1. Make a useful bounded advance on this task only.
2. Write /shared/reports/{agent_id}.md. Start it with exactly Status: completed
   or Status: blocked, then explain progress, sources or results, and blockers.
3. You may propose at most two follow-up tasks by writing Markdown files named
   /shared/proposals/pending/{agent_id}-<slug>.md. Proposals are not work: an
   independent evaluator can approve or reject them, and only the orchestrator
   can create or schedule a task. Each proposal needs a # title, a bounded
   deliverable, and clear acceptance criteria. Do not propose work requiring
   host root, SSH credentials, Docker sockets, service changes, or unavailable
   authority; record those as blockers in your report.
4. Do not modify /shared/tasks/, another agent's report, or /shared/objective.md.
5. Do not modify source files outside /workspace.

When the report is written, retire. The orchestrator validates it and updates task state."""

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
        prompt,
    ]

    process = await anyio.open_process(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    async def drain(
        stream: anyio.abc.ByteReceiveStream | None,
        write: Callable[[bytes], object],
    ) -> None:
        if stream is None:
            return
        async for chunk in stream:
            if show_output:
                write(chunk)
            if on_output is not None:
                on_output(chunk.decode(errors="replace"))

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(drain, process.stdout, sys.stdout.buffer.write)
        task_group.start_soon(drain, process.stderr, sys.stderr.buffer.write)
        exit_code = await process.wait()

    return AgentResult(
        exit_code=exit_code,
        reply=reply_file.read_text().strip() if reply_file.exists() else "",
        workspace=workspace,
    )


async def bootstrap_tasks(
    *,
    agent_id: str,
    run_dir: Path,
    image: str,
    model: str,
    api_base: str,
    context_window: int,
    show_output: bool,
) -> int:
    """Use one bounded planner container to turn the objective into ready tasks."""
    planner_id = f"{agent_id}-planner"
    prompt = f"""You are the bootstrap planner {planner_id}. Your private working directory is /workspace.

Read /shared/objective.md. Design a small, concrete, independently executable
work queue that advances that objective. Do not perform the work yourself.

Write between 3 and 12 task files to /shared/bootstrap. Each filename must end
in .md and be unique. Each file must have a short Markdown # title followed by
specific completion criteria, useful constraints, and any inputs or outputs.
Make tasks appropriately bounded for one worker; include a synthesis task only
when it has clear inputs from other tasks. Do not create tasks requiring host
root access, SSH, Docker sockets, installing or starting services, or changing
the swarm runner. Do not modify anything outside /workspace and /shared/bootstrap.

When the task files are written, retire."""
    result = await run_agent(
        agent_id=planner_id,
        run_dir=run_dir,
        image=image,
        model=model,
        api_base=api_base,
        context_window=context_window,
        prompt=prompt,
        show_output=show_output,
    )
    return result.exit_code


async def summarize_results(
    *,
    agent_id: str,
    run_dir: Path,
    image: str,
    model: str,
    api_base: str,
    context_window: int,
) -> int:
    """Run one final agent after the queue drains to produce the user-facing answer."""
    summarizer_id = f"{agent_id}-summarizer"
    prompt = f"""You are the final summarizer {summarizer_id}. Your private working directory is /workspace.

Read /shared/objective.md, all files in /shared/reports, and the task outcomes
under /shared/tasks/completed and /shared/tasks/blocked. Produce the final,
user-facing answer to the overall objective. Do not perform new research or
execute unfinished tasks: accurately synthesize the work already completed.

Write the answer to /shared/final.md. It must state the recommendation or
result first, distinguish evidence from inference, cite report filenames or
source URLs when available, and clearly call out gaps or blocked work. Do not
modify task files, reports, the objective, or anything outside /workspace and
/shared/final.md. When final.md is written, retire."""
    result = await run_agent(
        agent_id=summarizer_id,
        run_dir=run_dir,
        image=image,
        model=model,
        api_base=api_base,
        context_window=context_window,
        prompt=prompt,
        show_output=True,
    )
    return result.exit_code


async def evaluate_proposal(
    *,
    run_dir: Path,
    image: str,
    model: str,
    api_base: str,
    context_window: int,
    proposal: Proposal,
    show_output: bool,
    on_output: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    """Run a constrained evaluator; the orchestrator applies its decision."""
    evaluator_id = f"evaluator-{proposal.proposal_id}"[:96]
    decision_file = f"/shared/evaluations/{proposal.proposal_id}.md"
    prompt = f"""You are proposal evaluator {evaluator_id}. Read /shared/objective.md.

Evaluate this proposed follow-up, but do not perform the task itself:

Parent task: {proposal.parent_task}
Proposal:
---
{proposal.body}
---

Approve only if it is concrete, bounded, materially advances the objective,
has a clear deliverable and acceptance criteria, is not a duplicate of the
parent task, and needs no host root, SSH, Docker socket, service change, or
other unavailable authority. Reject speculative research expansion, vague work,
or work whose value does not justify delaying final synthesis.

Write exactly one decision file at {decision_file}. Its first line must be
exactly `Decision: approved` or `Decision: rejected`; follow with a concise
reason. Do not modify /shared/tasks, /shared/proposals, reports, the objective,
or any path outside /workspace and that decision file. The orchestrator alone
will apply your decision. Retire after writing it."""
    result = await run_agent(
        agent_id=evaluator_id,
        run_dir=run_dir,
        image=image,
        model=model,
        api_base=api_base,
        context_window=context_window,
        prompt=prompt,
        show_output=show_output,
        on_output=on_output,
    )
    return result.exit_code, evaluator_id


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
    activity_feed: deque[tuple[str, str, str]] = deque(maxlen=80)
    evaluation_lock = anyio.Lock()

    def add_event(agent: str, event: str, detail: str = "") -> None:
        activity_feed.append((agent, event, detail[:180]))

    def dashboard() -> Layout:
        counts = task_counts(shared_dir)
        active = sum(state.exit_code is None for state in states.values())
        completed = counts["completed"]
        blocked = counts["blocked"]
        assigned = counts["assigned"]
        created = counts["created"]
        proposals = proposal_counts(shared_dir)
        header = Text(justify="center")
        header.append("AGENT SWARM", style="bold cyan")
        header.append("  •  ")
        header.append(f"{active}/{workers} active", style="bold" if active else "dim")
        header.append("  •  ")
        header.append(f"{counts['scheduled']} scheduled", style="yellow" if counts["scheduled"] else "dim")
        header.append("  •  ")
        header.append(f"{created} created", style="dim")
        header.append("  •  ")
        header.append(f"{assigned} assigned", style="cyan" if assigned else "dim")
        header.append("  •  ")
        header.append(f"{completed} completed", style="green" if completed else "dim")
        header.append("  •  ")
        header.append(f"{blocked} blocked", style="red" if blocked else "dim")
        header.append("  •  ")
        header.append(
            f"{proposals['pending']} proposals",
            style="magenta" if proposals["pending"] else "dim",
        )

        table = Table(expand=True, header_style="bold", pad_edge=True)
        table.add_column("Agent", style="bold", no_wrap=True, width=18)
        table.add_column("State", no_wrap=True, width=12)
        table.add_column("Elapsed", justify="right", no_wrap=True, width=9)
        table.add_column("Assigned task", ratio=3, overflow="fold")
        table.add_column("Latest activity", ratio=4, overflow="fold")
        now = time.monotonic()
        for slot in sorted(states):
            state = states[slot]
            elapsed = now - state.started_monotonic
            status = state.status
            if state.exit_code is not None:
                status = f"{status} ({state.exit_code})"
            style = STATUS_STYLES.get(state.status, "yellow" if state.status.startswith("error") else "dim")
            table.add_row(
                state.agent_id,
                Text(status, style=style),
                f"{elapsed:.1f}s",
                state.task or "—",
                state.activity,
            )
        if not states:
            table.add_row("—", "idle", "—", "Waiting for a scheduled task", "")

        feed = Table(expand=True, header_style="bold")
        feed.add_column("Agent", width=18, no_wrap=True)
        feed.add_column("Event", width=12, no_wrap=True)
        feed.add_column("Detail", overflow="fold")
        for agent, event, detail in list(activity_feed)[-7:][::-1]:
            event_style = "green" if event == "retired" else "red" if event == "error" else "cyan" if event == "output" else "yellow"
            feed.add_row(agent, Text(event, style=event_style), detail)
        if not activity_feed:
            feed.add_row("—", "idle", "Waiting for agent activity")

        footer = Text(
            f"Run: {run_dir}    Shared task board: {shared_dir / 'tasks'}",
            style="dim",
            overflow="ellipsis",
        )
        layout = Layout()
        layout.split_column(
            Layout(Panel(header, border_style="cyan"), name="header", size=3),
            Layout(Panel(table, title="Workers", border_style="blue"), name="workers", ratio=3),
            Layout(Panel(feed, title="Event feed", border_style="magenta"), name="events", size=12),
            Layout(footer, name="footer", size=1),
        )
        return layout

    async def live_dashboard() -> None:
        with Live(dashboard(), refresh_per_second=4, screen=True) as live:
            while True:
                live.update(dashboard())
                await anyio.sleep(0.25)

    async def run_worker(slot: int, assignment: Assignment) -> None:
        slot_id = agent_id if workers == 1 else f"{agent_id}-{slot:02d}"
        states[slot] = AgentState(
            agent_id=slot_id,
            started_at=time.time(),
            started_monotonic=time.monotonic(),
            status="working",
            task=assignment.title,
        )
        add_event(slot_id, "started", assignment.title)
        exit_code = 1

        def record_output(chunk: str) -> None:
            for line in chunk.splitlines():
                message = " ".join(line.split())
                if not message or message in {"exec", "codex", "user", "assistant"}:
                    continue
                message = message[:180]
                states[slot].activity = message
                add_event(slot_id, "output", message)

        async def evaluate_followups() -> None:
            """Serialize evaluator decisions so task creation stays auditable."""
            async with evaluation_lock:
                while True:
                    proposal = claim_proposal(
                        shared_dir,
                        proposer=slot_id,
                        parent_task=assignment.title,
                    )
                    if proposal is None:
                        return
                    states[slot].status = "evaluating"
                    states[slot].activity = f"Evaluating proposal: {proposal.title}"
                    add_event(slot_id, "evaluating", proposal.title)
                    evaluator_exit, _evaluator_id = await evaluate_proposal(
                        run_dir=run_dir,
                        image=image,
                        model=model,
                        api_base=api_base,
                        context_window=context_window,
                        proposal=proposal,
                        show_output=not show_tui,
                        on_output=record_output,
                    )
                    decision, reason = evaluation_decision(evaluation_path(shared_dir, proposal))
                    if evaluator_exit != 0:
                        decision = "rejected"
                        reason = f"Evaluator exited with code {evaluator_exit}."
                    outcome = apply_evaluation(shared_dir, proposal, decision, reason)
                    states[slot].activity = f"Proposal {outcome}: {proposal.title}"
                    add_event(slot_id, outcome, proposal.title)

        try:
            result = await run_agent(
                agent_id=slot_id,
                run_dir=run_dir,
                image=image,
                model=model,
                api_base=api_base,
                context_window=context_window,
                assignment=assignment,
                show_output=not show_tui,
                on_output=record_output,
            )
            exit_code = result.exit_code
            states[slot].exit_code = exit_code
            final_state = finalize(shared_dir, assignment, slot_id, exit_code)
            states[slot].status = final_state
            if final_state == "completed":
                await evaluate_followups()
            states[slot].status = final_state
            states[slot].activity = "Task report validated; worker retired"
            add_event(slot_id, "retired", final_state)
            if not show_tui:
                print(f"[{slot_id}] retired with exit code {exit_code}", file=sys.stderr)
        except Exception as exc:
            # A launch failure occurs after assignment, so return the task to a
            # visible terminal state instead of stranding it in ``assigned``.
            try:
                states[slot].status = finalize(shared_dir, assignment, slot_id, exit_code)
            except OSError:
                states[slot].status = f"error: {exc}"
            states[slot].exit_code = exit_code
            states[slot].activity = "Launcher error; task marked blocked"
            add_event(slot_id, "error", str(exc))
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
            ready = ready_count(shared_dir)
            while ready > 0 and available_slots:
                slot = available_slots.pop()
                slot_id = agent_id if workers == 1 else f"{agent_id}-{slot:02d}"
                assignment = assign(shared_dir, slot_id)
                if assignment is None:
                    available_slots.add(slot)
                    break
                active_slots.add(slot)
                task_group.start_soon(run_worker, slot, assignment)
                ready -= 1

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
        help="Durable overall goal stored in the shared task board",
    )
    parser.add_argument(
        "--task",
        action="append",
        default=[],
        help="Initial independent task; repeat this flag to add more items",
    )
    parser.add_argument("--task-dir", type=Path, help="Directory of Markdown task files")
    parser.add_argument("--run-dir", type=Path, default=Path(".agent-runs"))
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--context-window", type=int, required=True)
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Maximum number of simultaneous orchestrator-assigned agent containers",
    )
    parser.add_argument("--tui", action="store_true", help="Show a live worker dashboard")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.workers < 1:
        raise SystemExit("--workers must be at least 1")
    auto_plan = not args.task and args.task_dir is None
    try:
        board_path = initialize_task_queue(
            run_dir=args.run_dir,
            objective=args.objective,
            tasks=args.task,
            task_dir=args.task_dir,
            allow_empty=auto_plan,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Unable to initialize task board: {exc}") from exc
    print(f"task board: {board_path}")

    if auto_plan:
        print(f"starting bootstrap planner: {args.agent_id}-planner")
        planner_exit_code = anyio.run(
            lambda: bootstrap_tasks(
                agent_id=args.agent_id,
                run_dir=args.run_dir,
                image=args.image,
                model=args.model,
                api_base=args.api_base,
                context_window=args.context_window,
                show_output=True,
            )
        )
        if planner_exit_code != 0:
            raise SystemExit(f"bootstrap planner exited with code {planner_exit_code}")
        accepted = ingest_bootstrap_tasks(args.run_dir.resolve() / "shared")
        if accepted == 0:
            raise SystemExit("bootstrap planner produced no valid task files")
        print(f"bootstrap planner queued {accepted} tasks")

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

    print(f"starting final summarizer: {args.agent_id}-summarizer")
    summarizer_exit_code = anyio.run(
        lambda: summarize_results(
            agent_id=args.agent_id,
            run_dir=args.run_dir,
            image=args.image,
            model=args.model,
            api_base=args.api_base,
            context_window=args.context_window,
        )
    )
    final_path = args.run_dir.resolve() / "shared" / "final.md"
    if summarizer_exit_code != 0:
        raise SystemExit(f"final summarizer exited with code {summarizer_exit_code}")
    if not final_path.exists() or not final_path.read_text().strip():
        raise SystemExit("final summarizer did not write shared/final.md")
    print(f"final answer: {final_path}")
    print(final_path.read_text().strip())
