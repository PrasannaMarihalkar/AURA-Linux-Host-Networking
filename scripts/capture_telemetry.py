"""Capture AURA telemetry during idle or CPU-loaded conditions."""

import argparse
import json
import multiprocessing
import os
import time
from pathlib import Path

from aura import telemetry


def cpu_worker(stop_event):
    """Generate CPU workload until stopped."""
    value = 1

    while not stop_event.is_set():
        value = (value * 1664525 + 1013904223) & 0xFFFFFFFF


def available_cpus():
    """Return the number of CPUs available to this process."""
    if hasattr(os, "sched_getaffinity"):
        return max(1, len(os.sched_getaffinity(0)))

    return max(1, os.cpu_count() or 1)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--mode",
        choices=["idle", "loaded"],
        required=True,
    )
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--interface", default="enp0s3")
    parser.add_argument("--output", required=True)

    args = parser.parse_args()

    if args.duration <= 0 or args.interval <= 0:
        parser.error("Duration and interval must be positive")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    stop_event = multiprocessing.Event()
    workers = []

    try:
        if args.mode == "loaded":
            cpu_count = available_cpus()

            print(f"Starting {cpu_count} CPU stress workers.")

            for _ in range(cpu_count):
                process = multiprocessing.Process(
                    target=cpu_worker,
                    args=(stop_event,),
                )
                process.start()
                workers.append(process)

        telemetry._BASELINE = None

        print(f"Mode: {args.mode}")
        print(f"Interface: {args.interface}")
        print(f"Duration: {args.duration} seconds")
        print(f"Output: {output}")

        start = time.monotonic()
        next_sample = None
        samples = 0

        with output.open("w", encoding="utf-8") as file:
            while time.monotonic() - start < args.duration:

                if next_sample is not None:
                    delay = next_sample - time.monotonic()

                    if delay > 0:
                        time.sleep(delay)

                if time.monotonic() - start >= args.duration:
                    break

                snapshot = telemetry.collect_snapshot(args.interface)

                file.write(json.dumps(snapshot, allow_nan=False) + "\n")
                file.flush()

                samples += 1

                if samples % 10 == 0:
                    print(f"Collected {samples} snapshots.")

                if next_sample is None:
                    next_sample = time.monotonic() + args.interval
                else:
                    next_sample += args.interval

        print(f"Experiment completed. Samples: {samples}")
        print(f"Saved: {output}")

    finally:
        stop_event.set()

        for process in workers:
            process.join(timeout=3)

            if process.is_alive():
                process.terminate()
                process.join()


if __name__ == "__main__":
    main()
