"""Lock-backed operations for the shared Markdown task board."""

from __future__ import annotations

import argparse
import fcntl
import json
import time
from contextlib import contextmanager
from pathlib import Path


DEFAULT_SHARED = Path("/shared")


@contextmanager
def board_lock(shared_dir: Path):
    lock_path = shared_dir / ".todo.lock"
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def record_event(shared_dir: Path, event: str, **details: str) -> None:
    payload = {"timestamp": time.time(), "event": event, **details}
    with (shared_dir / "events.jsonl").open("a") as event_file:
        event_file.write(json.dumps(payload) + "\n")


def claim(shared_dir: Path, agent_id: str) -> int:
    todo_path = shared_dir / "todo.md"
    with board_lock(shared_dir):
        lines = todo_path.read_text().splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.startswith("- [ ] "):
                task = line.removeprefix("- [ ] ").strip()
                todo_path.write_text("".join(lines[:index] + lines[index + 1 :]))
                record_event(shared_dir, "task_claimed", agent_id=agent_id, task=task)
                print(task)
                return 0
    return 2


def add(shared_dir: Path, task: str) -> None:
    task = task.strip()
    if not task or "\n" in task:
        raise ValueError("task must be a single non-empty line")
    with board_lock(shared_dir):
        with (shared_dir / "todo.md").open("a") as todo_file:
            todo_file.write(f"- [ ] {task}\n")


def pending_count(shared_dir: Path) -> int:
    with board_lock(shared_dir):
        return sum(
            line.startswith("- [ ] ")
            for line in (shared_dir / "todo.md").read_text().splitlines()
        )


def complete(shared_dir: Path, agent_id: str, task: str, summary: str, report: str) -> None:
    values = (task, summary, report)
    if any(not value.strip() or "\n" in value for value in values):
        raise ValueError("task, summary, and report must each be one non-empty line")
    with board_lock(shared_dir):
        with (shared_dir / "completed.md").open("a") as completed_file:
            completed_file.write(f"- {task} — {summary} (report: {report})\n")
        record_event(
            shared_dir,
            "task_completed",
            agent_id=agent_id,
            task=task,
            summary=summary,
            report=report,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage a shared Codex task board")
    parser.add_argument("--shared-dir", type=Path, default=DEFAULT_SHARED)
    subcommands = parser.add_subparsers(dest="command", required=True)
    claim_parser = subcommands.add_parser("claim")
    claim_parser.add_argument("--agent-id", required=True)
    subcommands.add_parser("pending")
    add_parser = subcommands.add_parser("add")
    add_parser.add_argument("task")
    complete_parser = subcommands.add_parser("complete")
    complete_parser.add_argument("--agent-id", required=True)
    complete_parser.add_argument("--task", required=True)
    complete_parser.add_argument("--summary", required=True)
    complete_parser.add_argument("--report", required=True)
    args = parser.parse_args()
    shared_dir = args.shared_dir.resolve()

    if args.command == "claim":
        return claim(shared_dir, args.agent_id)
    if args.command == "pending":
        print(pending_count(shared_dir))
        return 0
    if args.command == "add":
        add(shared_dir, args.task)
        return 0
    complete(shared_dir, args.agent_id, args.task, args.summary, args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
