#!/usr/bin/env python3
"""Update monitor/mission-status.json from the command line. Safe to import; runs only as a script."""
import argparse, json, time
from pathlib import Path

p = Path(__file__).with_name("mission-status.json")


def main(argv=None):
    a = argparse.ArgumentParser(); a.add_argument("--phase"); a.add_argument("--task"); a.add_argument("--status"); a.add_argument("--human", choices=["yes", "no"]); a.add_argument("--complete"); x = a.parse_args(argv)
    d = json.loads(p.read_text())
    if x.phase: d["current_phase"] = x.phase
    if x.task: d["current_task"] = x.task
    if x.status: d["status"] = x.status
    if x.human: d["human_action_required"] = x.human == "yes"
    if x.complete:
        for q in d["phases"]:
            if q["id"] == x.complete: q["status"] = "done"
    d["updated_at"] = time.time(); p.write_text(json.dumps(d, indent=2)+"\n")


if __name__ == "__main__":
    main()
