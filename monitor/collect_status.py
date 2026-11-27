#!/usr/bin/env python3
"""Write monitor/status.json with basic host health. Safe to import; runs only as a script."""
import json, shutil, time
from pathlib import Path

ROOT = Path('/opt/ai-orchestrator')
if not ROOT.exists(): ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'monitor'/'status.json'


def collect(root=ROOT):
    mem = {}
    for line in open('/proc/meminfo'):
        k, v, *_ = line.split(); mem[k.rstrip(':')] = int(v)*1024
    du = shutil.disk_usage('/')
    profile = {}
    try: profile = json.loads((root/'profiles'/'active.json').read_text())
    except Exception: pass
    return {'timestamp': time.time(), 'profile': profile.get('profile', 'unknown'), 'disk_used_pct': round(100*du.used/du.total, 1), 'disk_free_gb': round(du.free/1024**3, 1), 'ram_used_pct': round(100*(1-mem.get('MemAvailable', 0)/mem.get('MemTotal', 1)), 1), 'uptime_hours': round(float(open('/proc/uptime').read().split()[0])/3600, 1)}


def main():
    OUT.write_text(json.dumps(collect(), indent=2)+'\n')


if __name__ == '__main__':
    main()
