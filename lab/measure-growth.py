#!/usr/bin/env python3
"""
Measure how large the mirrored repo actually gets over time.

"A year of moves/adds/changes will make the repo huge" is a reasonable
worry and worth checking rather than guessing. This simulates a year of
churn against a realistically-sized estate, driving the *real*
write_snapshot() so the serialisation, per-subnet file layout and
manifest are exactly what production produces, then measures the git
repo.

    ./lab/measure-growth.py                       # defaults below
    ./lab/measure-growth.py --subnets 400 --per-subnet 120 \
                            --changes-per-day 500 --days 365

Defaults model 204 subnets x 200 addresses (~40,000 records) with 30
address changes a day. Adjust to your own estate and churn rate.

Results on that default profile (git 2.4x, one host):

    30 changes/day    day 365: 27.5 MB loose  ->   2.6 MB packed
    300 changes/day   day 365: 38.4 MB loose  ->  12.2 MB packed

The uncompressed content in the low-churn case totalled 371 MB across
10,780 blobs; git packed it to 2.3 MB. Canonically-serialised JSON with
sorted keys deltas extremely well, which is the main reason this stays
small -- and a good reason not to abandon the canonical serialisation.
"""

import argparse
import pathlib
import random
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from ipamsync.snapshot import build_subnet_document, write_snapshot  # noqa: E402


def git(args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check,
                          capture_output=True, text=True)


def size_mb(path):
    out = subprocess.run(["du", "-sk", str(path)], capture_output=True,
                         text=True).stdout
    return int(out.split()[0]) / 1024


def build_documents(state, subnets, per_subnet):
    documents = []
    for i in range(subnets):
        third = i % 256
        net = f"10.{100 + i // 256}.{third}.0"
        documents.append(build_subnet_document(
            section_name="Shared", cidr=f"{net}/24",
            fields={"description": f"subnet {i}", "tag": "2", "isPool": "0"},
            addresses=[(f"10.{100 + i // 256}.{third}.{h}", state[(i, h)])
                       for h in range(1, per_subnet + 1)],
        ))
    return documents


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--subnets", type=int, default=204)
    parser.add_argument("--per-subnet", type=int, default=200)
    parser.add_argument("--changes-per-day", type=int, default=30)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--repo", default="/tmp/ipam-growth-sim")
    args = parser.parse_args()

    rnd = random.Random(1234)          # deterministic, so runs compare
    repo = pathlib.Path(args.repo)
    subprocess.run(["rm", "-rf", str(repo)], check=False)
    repo.mkdir(parents=True)
    git(["init", "-q"], repo)
    git(["config", "user.email", "sim@example.com"], repo)
    git(["config", "user.name", "sim"], repo)

    state = {(i, h): {"hostname": f"host-{i}-{h}", "description": "seed",
                      "owner": "netops", "tag": "2"}
             for i in range(args.subnets)
             for h in range(1, args.per_subnet + 1)}
    records = args.subnets + len(state)
    print(f"{args.subnets} subnets x {args.per_subnet} addresses "
          f"= {records:,} records; {args.changes_per_day} changes/day "
          f"over {args.days} days\n")

    write_snapshot(repo, build_documents(state, args.subnets, args.per_subnet),
                   source={"base_url": "https://sim"}, sections=["Shared"])
    git(["add", "-A"], repo)
    git(["commit", "-qm", "day 0"], repo)

    commits = 1
    for day in range(1, args.days + 1):
        for _ in range(args.changes_per_day):
            i = rnd.randrange(args.subnets)
            h = rnd.randrange(1, args.per_subnet + 1)
            state[(i, h)] = {
                "hostname": f"host-{i}-{h}-d{day}",
                "description": f"changed day {day}",
                "owner": rnd.choice(["netops", "servers", "voice"]),
                "tag": "2",
            }
        write_snapshot(repo,
                       build_documents(state, args.subnets, args.per_subnet),
                       source={"base_url": "https://sim"}, sections=["Shared"])
        # Exactly what the exporter does: commit only on real change.
        if git(["status", "--porcelain"], repo).stdout.strip():
            git(["add", "-A"], repo)
            git(["commit", "-qm", f"day {day}"], repo)
            commits += 1
        if day in (30, 90, 180, args.days):
            print(f"  day {day:>4}  {size_mb(repo / '.git'):7.1f} MB "
                  f"({commits} commits)")

    print("\n  running git gc --aggressive ...")
    git(["gc", "--aggressive", "--prune=now"], repo, check=False)
    print(f"  packed  {size_mb(repo / '.git'):7.1f} MB")

    counts = git(["count-objects", "-vH"], repo).stdout
    print("\n" + "\n".join(f"  {line}" for line in counts.strip().splitlines()))
    print(f"\n  working tree (current snapshot): "
          f"{size_mb(repo) - size_mb(repo / '.git'):.1f} MB")
    print(f"  repo left at {repo}")


if __name__ == "__main__":
    main()
