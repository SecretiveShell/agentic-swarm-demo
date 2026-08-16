"""Orchestrator-owned Markdown task queue for swarm workers."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


TASK_STATES = ("ready", "assigned", "completed", "blocked")
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


def initialize(
    shared_dir: Path,
    objective: str,
    tasks: list[str],
    task_file: Path | None,
    *,
    allow_empty: bool = False,
) -> None:
    shared_dir.mkdir(parents=True, exist_ok=True)
    for state in TASK_STATES:
        (shared_dir / "tasks" / state).mkdir(parents=True, exist_ok=True)
    (shared_dir / "proposals" / "rejected").mkdir(parents=True, exist_ok=True)
    (shared_dir / "bootstrap").mkdir(exist_ok=True)
    (shared_dir / "reports").mkdir(exist_ok=True)

    objective_path = shared_dir / "objective.md"
    if not objective_path.exists():
        objective_path.write_text(f"# Overall objective\n\n{objective.strip()}\n")

    if any(
        any((shared_dir / "tasks" / state).glob("*.md"))
        for state in TASK_STATES
    ):
        return
    source_tasks = tasks
    if task_file is not None:
        source_tasks = [
            path.read_text().strip()
            for path in sorted(task_file.glob("*.md"))
            if path.is_file()
        ]
    if not source_tasks:
        if allow_empty:
            return
        raise ValueError("provide at least one --task or a --task-dir for a new run")
    for index, task in enumerate(source_tasks, start=1):
        write_task(shared_dir / "tasks" / "ready" / f"{index:03d}-{slug(task)}.md", task)


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


def ready_count(shared_dir: Path) -> int:
    return task_counts(shared_dir)["ready"]


def task_counts(shared_dir: Path) -> dict[str, int]:
    """Return the number of task files in every orchestrator-owned state."""
    return {
        state: len(list((shared_dir / "tasks" / state).glob("*.md")))
        for state in TASK_STATES
    }


def ingest_bootstrap_tasks(shared_dir: Path) -> int:
    """Validate task files produced by the bootstrap agent and queue them."""
    bootstrap_dir = shared_dir / "bootstrap"
    ready_dir = shared_dir / "tasks" / "ready"
    accepted = 0
    next_number = next_task_number(shared_dir)
    for candidate in sorted(bootstrap_dir.glob("*.md")):
        content = candidate.read_text().strip()
        title = task_title(content)
        lowered = content.lower()
        if not title or any(term in lowered for term in FORBIDDEN_PROPOSAL_TERMS):
            shutil.move(candidate, shared_dir / "proposals" / "rejected" / candidate.name)
            continue
        destination = ready_dir / f"{next_number:03d}-{slug(title)}.md"
        shutil.move(candidate, destination)
        next_number += 1
        accepted += 1
    return accepted


def assign(shared_dir: Path, agent_id: str) -> Assignment | None:
    ready_dir = shared_dir / "tasks" / "ready"
    task_path = next(iter(sorted(ready_dir.glob("*.md"))), None)
    if task_path is None:
        return None
    assigned_path = shared_dir / "tasks" / "assigned" / f"{agent_id}--{task_path.name}"
    os.replace(task_path, assigned_path)
    body = assigned_path.read_text()
    return Assignment(assigned_path, task_title(body), body)


def finalize(shared_dir: Path, assignment: Assignment, agent_id: str, exit_code: int) -> str:
    report_path = shared_dir / "reports" / f"{agent_id}.md"
    status = report_status(report_path) if report_path.exists() else "blocked"
    target_state = "completed" if exit_code == 0 and status == "completed" else "blocked"
    target = shared_dir / "tasks" / target_state / assignment.path.name
    os.replace(assignment.path, target)
    if target_state == "completed":
        promote_proposals(shared_dir, agent_id)
    return target_state


def report_status(report_path: Path) -> str:
    first_lines = report_path.read_text().splitlines()[:8]
    for line in first_lines:
        if line.strip().lower() == "status: completed":
            return "completed"
        if line.strip().lower() == "status: blocked":
            return "blocked"
    return "blocked"


def promote_proposals(shared_dir: Path, agent_id: str) -> None:
    proposal_dir = shared_dir / "proposals"
    candidates = sorted(proposal_dir.glob(f"{agent_id}-*.md"))[:2]
    for proposal in candidates:
        content = proposal.read_text()
        lowered = content.lower()
        destination = proposal_dir / "rejected" / proposal.name
        if any(term in lowered for term in FORBIDDEN_PROPOSAL_TERMS):
            shutil.move(proposal, destination)
            continue
        title = task_title(content)
        if not title:
            shutil.move(proposal, destination)
            continue
        ready_dir = shared_dir / "tasks" / "ready"
        number = next_task_number(shared_dir)
        shutil.move(proposal, ready_dir / f"{number:03d}-{slug(title)}.md")


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
