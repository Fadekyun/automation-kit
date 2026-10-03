"""Read-only admission check. Never deletes, expands or repairs storage."""
import argparse
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import time

GIB = 1024 ** 3
MAX_AGE_SECONDS = 60
MIN_AVAILABLE_BYTES = 6 * GIB
RECOVERY_RESERVE_BYTES = GIB
CONCURRENT_RESERVE_BYTES = 2 * GIB
POOL_DATA_LIMIT = 90
POOL_METADATA_LIMIT = 70


def number(value, *, integer=False, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid_number")
    if not math.isfinite(value) or value < minimum:
        raise ValueError("invalid_number")
    if integer and not isinstance(value, int):
        raise ValueError("invalid_integer")
    if maximum is not None and value > maximum:
        raise ValueError("invalid_number")
    return value


def utc(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


def evaluate(observation, peak_bytes, *, now=None):
    """An unknown or stale observation always blocks dependent work.

    Peak includes the largest simultaneous temporary/export/build footprint.
    Doubling it budgets overlap, plus reserves for recovery and other writers.
    This is admission evidence, not a reservation against unrelated processes.
    """
    now = time.time() if now is None else now
    peak = number(peak_bytes, integer=True, minimum=1, maximum=2**63-1)
    required = max(MIN_AVAILABLE_BYTES,
                   2 * peak + RECOVERY_RESERVE_BYTES + CONCURRENT_RESERVE_BYTES)
    result = {
        "schema_version": 1, "report_id": "ops.capacity.v1",
        "generated_at": utc(now), "source_as_of": None,
        "status": "blocked", "completeness": "unknown",
        "required_available_bytes": required, "reasons": [],
    }
    try:
        if observation["schema_version"] != 1 or type(observation["schema_version"]) is not int:
            raise ValueError("schema")
        sampled = number(observation["sampled_at"], minimum=1)
        if not 0 <= now - sampled <= MAX_AGE_SECONDS:
            raise ValueError("stale")
        fs = observation["filesystem"]
        avail = number(fs["available_bytes"], integer=True)
        total = number(fs["total_bytes"], integer=True, minimum=1)
        if avail > total or fs["read_only"] is not False:
            raise ValueError("filesystem")
        pool = observation["thin_pool"]
        size = number(pool["size_bytes"], integer=True, minimum=1)
        data = number(pool["data_percent"], maximum=100)
        metadata = number(pool["metadata_percent"], maximum=100)
        if pool["healthy"] is not True:
            raise ValueError("pool_health")
    except (KeyError, TypeError, ValueError, OverflowError):
        result["reasons"] = ["CAPACITY_EVIDENCE_UNAVAILABLE"]
        return result
    projected = data + 100 * (2 * peak + CONCURRENT_RESERVE_BYTES) / size
    result.update({
        "source_as_of": utc(sampled), "completeness": "complete",
        "available_bytes": avail, "pool_data_percent": data,
        "pool_metadata_percent": metadata,
        "projected_pool_data_percent": round(projected, 6),
    })
    reasons = result["reasons"]
    if avail < required:
        reasons.append("FILESYSTEM_HEADROOM_LOW")
    if data >= POOL_DATA_LIMIT or projected >= POOL_DATA_LIMIT:
        reasons.append("THIN_POOL_HEADROOM_LOW")
    if metadata >= POOL_METADATA_LIMIT:
        reasons.append("THIN_POOL_METADATA_HIGH")
    if not reasons:
        result["status"] = "ready"
    return result


def command(args):
    # No shell, output/timeout bounded, no raw diagnostics in a report.
    proc = subprocess.run(args, capture_output=True, timeout=15, check=True)
    if len(proc.stdout) > 65536:
        raise ValueError("output_limit")
    return proc.stdout.decode("utf-8")


def filesystem(path):
    info = os.statvfs(path)
    return {"available_bytes": info.f_bavail * info.f_frsize,
            "total_bytes": info.f_blocks * info.f_frsize,
            "read_only": bool(info.f_flag & os.ST_RDONLY)}


def collect(pool_name, *, container=None, user=None, path="/"):
    sampled_at = time.time()
    if not re.fullmatch(r"[A-Za-z0-9_+.-]+/[A-Za-z0-9_+.-]+", pool_name):
        raise ValueError("pool_name")
    if container is None:
        if user is not None:
            raise ValueError("service_user_requires_container")
        fs = filesystem(path)
    else:
        if type(container) is not int or not 100 <= container <= 999999999:
            raise ValueError("container")
        if not user or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
            raise ValueError("user")
        # statvfs.f_bavail reflects space usable by the actual service user.
        code = ("import os,json;v=os.statvfs('/');print(json.dumps({"
                "'available_bytes':v.f_bavail*v.f_frsize,"
                "'total_bytes':v.f_blocks*v.f_frsize,"
                "'read_only':bool(v.f_flag & os.ST_RDONLY)}))")
        fs = json.loads(command(["pct", "exec", str(container), "--", "runuser",
                                 "-u", user, "--", "python3", "-c", code]))
    raw = json.loads(command(["lvs", "--reportformat", "json", "--units", "b",
                              "--nosuffix", "-o",
                              "lv_size,data_percent,metadata_percent,lv_health_status,segtype",
                              pool_name]))
    rows = raw["report"][0]["lv"]
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("pool_identity")
    row = rows[0]
    for key in ('lv_size', 'data_percent', 'metadata_percent', 'lv_health_status', 'segtype'):
        if not isinstance(row.get(key), str):
            raise ValueError('invalid_lvm_scalar')
    if row['segtype'].strip() != 'thin-pool':
        raise ValueError('pool_identity')
    return {"schema_version": 1, "sampled_at": sampled_at, "filesystem": fs,
            "thin_pool": {"size_bytes": int(float(row["lv_size"])),
                          "data_percent": float(row["data_percent"]),
                          "metadata_percent": float(row["metadata_percent"]),
                          "healthy": not row["lv_health_status"].strip()}}


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's normal errors quote invalid arguments, which could be private.
        # --help still uses the standard help text and successful exit.
        raise ValueError('invalid_arguments')


def main(argv=None):
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--peak-bytes", required=True, type=int)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--observation", help="JSON observation file; '-' for stdin")
    src.add_argument("--thin-pool", help="Read live LVM thin-pool evidence")
    parser.add_argument("--container", type=int)
    parser.add_argument("--service-user")
    try:
        args = parser.parse_args(argv)
        if args.observation:
            if args.container or args.service_user:
                raise ValueError("incompatible_flags")
            if args.observation == "-":
                data = sys.stdin.read(65537)
            else:
                with open(args.observation, encoding="utf-8") as source:
                    data = source.read(65537)
            if len(data) > 65536:
                raise ValueError("input_limit")
            observation = json.loads(data)
        else:
            observation = collect(args.thin_pool, container=args.container, user=args.service_user)
        result = evaluate(observation, args.peak_bytes)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError,
            OverflowError, RecursionError, subprocess.SubprocessError):
        result = {"schema_version": 1, "report_id": "ops.capacity.v1",
                  "generated_at": utc(time.time()), "source_as_of": None,
                  "status": "blocked", "completeness": "unknown",
                  "reasons": ["CAPACITY_EVIDENCE_UNAVAILABLE"]}
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result["status"] == "ready" else 2


if __name__ == "__main__":
    sys.exit(main())
