"""Orchestrator-owned Markdown task and proposal state machines."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


TASK_STATES = ("created", "scheduled", "assigned", "completed", "blocked", "cancelled")
PROPOSAL_STATES = ("pending", "evaluating", "approved", "rejected")
FORBIDDEN_PROPOSAL_TERMS = (
    "docker.sock",
    "host root",
    "sudo ",
    "ssh ",
    "install docker",
    "start vllm",
)


@dataclass(frozen=True)
class Assignment:
    path: Path
    title: str
    body: str


@dataclass(frozen=True)
class Proposal:
    path: Path
    proposal_id: str
    title: str
    body: str
    proposer: str
    parent_task: str


def initialize(
    shared_dir: Path,
    objective: str,
    tasks: list[str],
    task_dir: Path | None,
    *,
    allow_empty: bool = False,
) -> None:
    """Create a new board without modifying an existing task lifecycle."""
    shared_dir.mkdir(parents=True, exist_ok=True)
    for state in TASK_STATES:
        (shared_dir / "tasks" / state).mkdir(parents=True, exist_ok=True)
    for state in PROPOSAL_STATES:
        (shared_dir / "proposals" / state).mkdir(parents=True, exist_ok=True)
    (shared_dir / "bootstrap").mkdir(exist_ok=True)
    (shared_dir / "evaluations").mkdir(exist_ok=True)
    (shared_dir / "reports").mkdir(exist_ok=True)

    objective_path = shared_dir / "objective.md"
    if not objective_path.exists():
        objective_path.write_text(f"# Overall objective\n\n{objective.strip()}\n")

    if any(any((shared_dir / "tasks" / state).glob("*.md")) for state in TASK_STATES):
        return
    source_tasks = tasks
    if task_dir is not None:
        source_tasks = [
            path.read_text().strip()
            for path in sorted(task_dir.glob("*.md"))
            if path.is_file()
        ]
    if not source_tasks:
        if allow_empty:
            return
        raise ValueError("provide at least one --task or a --task-dir for a new run")
    for task in source_tasks:
        create_task(shared_dir, task)
    schedule_created_tasks(shared_dir)


def write_task(path: Path, task: str) -> None:
    task = task.strip()
    if not task:
        raise ValueError("task cannot be empty")
    title = task.splitlines()[0].lstrip("# ").strip()
    body = task if task.startswith("#") else f"# {title}\n\n{task}\n"
    path.write_text(body)


def slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (value or "task")[:64]


def task_counts(shared_dir: Path) -> dict[str, int]:
    return {
        state: len(list((shared_dir / "tasks" / state).glob("*.md")))
        for state in TASK_STATES
    }


def proposal_counts(shared_dir: Path) -> dict[str, int]:
    return {
        state: len(list((shared_dir / "proposals" / state).glob("*.md")))
        for state in PROPOSAL_STATES
    }


def ready_count(shared_dir: Path) -> int:
    """Compatibility name: scheduled tasks are ready for assignment."""
    return task_counts(shared_dir)["scheduled"]


def create_task(shared_dir: Path, body: str) -> Path:
    """Create a task before it becomes eligible for scheduling."""
    title = task_title(body)
    if not title:
        raise ValueError("task must have a title")
    path = shared_dir / "tasks" / "created" / f"{next_task_number(shared_dir):03d}-{slug(title)}.md"
    write_task(path, body)
    return path


def schedule_created_tasks(shared_dir: Path) -> int:
    """Promote created tasks to the scheduler-owned runnable queue."""
    created_dir = shared_dir / "tasks" / "created"
    scheduled_dir = shared_dir / "tasks" / "scheduled"
    count = 0
    for task_path in sorted(created_dir.glob("*.md")):
        os.replace(task_path, scheduled_dir / task_path.name)
        count += 1
    return count


def ingest_bootstrap_tasks(shared_dir: Path) -> int:
    """Validate bootstrap task files, then create and schedule them."""
    accepted = 0
    for candidate in sorted((shared_dir / "bootstrap").glob("*.md")):
        content = candidate.read_text().strip()
        if not task_title(content) or contains_forbidden_term(content):
            shutil.move(candidate, shared_dir / "proposals" / "rejected" / candidate.name)
            continue
        create_task(shared_dir, content)
        candidate.unlink()
        accepted += 1
    schedule_created_tasks(shared_dir)
    return accepted


def assign(shared_dir: Path, agent_id: str) -> Assignment | None:
    scheduled_dir = shared_dir / "tasks" / "scheduled"
    task_path = next(iter(sorted(scheduled_dir.glob("*.md"))), None)
    if task_path is None:
        return None
    assigned_path = shared_dir / "tasks" / "assigned" / f"{agent_id}--{task_path.name}"
    os.replace(task_path, assigned_path)
    body = assigned_path.read_text()
    return Assignment(assigned_path, task_title(body), body)


def finalize(shared_dir: Path, assignment: Assignment, agent_id: str, exit_code: int) -> str:
    """Finish an assigned task. Proposal evaluation is intentionally separate."""
    report_path = shared_dir / "reports" / f"{agent_id}.md"
    status = report_status(report_path) if report_path.exists() else "blocked"
    target_state = "completed" if exit_code == 0 and status == "completed" else "blocked"
    os.replace(assignment.path, shared_dir / "tasks" / target_state / assignment.path.name)
    return target_state


def claim_proposal(
    shared_dir: Path, *, proposer: str, parent_task: str
) -> Proposal | None:
    """Atomically move one pending proposal into evaluator ownership."""
    pending_dir = shared_dir / "proposals" / "pending"
    proposal_path = next(iter(sorted(pending_dir.glob(f"{proposer}-*.md"))), None)
    if proposal_path is None:
        return None
    evaluating_path = shared_dir / "proposals" / "evaluating" / proposal_path.name
    os.replace(proposal_path, evaluating_path)
    body = evaluating_path.read_text()
    return Proposal(
        path=evaluating_path,
        proposal_id=evaluating_path.stem,
        title=task_title(body),
        body=body,
        proposer=proposer,
        parent_task=parent_task,
    )


def evaluation_path(shared_dir: Path, proposal: Proposal) -> Path:
    return shared_dir / "evaluations" / f"{proposal.proposal_id}.md"


def evaluation_decision(path: Path) -> tuple[str, str]:
    """Read the small evaluator contract; invalid output safely rejects."""
    if not path.exists():
        return "rejected", "Evaluator did not write a decision."
    lines = path.read_text().splitlines()
    decision = "rejected"
    for line in lines[:8]:
        normalized = line.strip().lower()
        if normalized == "decision: approved":
            decision = "approved"
            break
        if normalized == "decision: rejected":
            break
    reason = next((line.strip() for line in lines[1:] if line.strip()), "No reason supplied.")
    return decision, reason


def apply_evaluation(
    shared_dir: Path, proposal: Proposal, decision: str, reason: str
) -> str:
    """Apply the evaluator's verdict; only this function may create a task."""
    if decision == "approved" and proposal.title and not contains_forbidden_term(proposal.body):
        create_task(shared_dir, proposal.body)
        target_state = "approved"
    else:
        target_state = "rejected"
        if decision == "approved":
            reason = "Orchestrator rejected an invalid or disallowed approved proposal."
    os.replace(proposal.path, shared_dir / "proposals" / target_state / proposal.path.name)
    audit_path = evaluation_path(shared_dir, proposal)
    if not audit_path.exists() or target_state != decision:
        audit_path.write_text(f"Decision: {target_state}\n\nReason: {reason}\n")
    schedule_created_tasks(shared_dir)
    return target_state


def report_status(report_path: Path) -> str:
    for line in report_path.read_text().splitlines()[:8]:
        if line.strip().lower() == "status: completed":
            return "completed"
        if line.strip().lower() == "status: blocked":
            return "blocked"
    return "blocked"


def next_task_number(shared_dir: Path) -> int:
    numbers = []
    for state in TASK_STATES:
        for path in (shared_dir / "tasks" / state).glob("*.md"):
            if path.name[:3].isdigit():
                numbers.append(int(path.name[:3]))
    return max(numbers, default=0) + 1


def task_title(content: str) -> str:
    for line in content.splitlines():
        if line.lstrip().startswith("#"):
            return line.lstrip("# ").strip()
        if line.strip():
            return line.strip()
    return ""


def contains_forbidden_term(content: str) -> bool:
    lowered = content.lower()
    return any(term in lowered for term in FORBIDDEN_PROPOSAL_TERMS)
