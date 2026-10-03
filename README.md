# Automation Kit

MIT-licensed, dependency-free components for self-hosted operations. Python 3.11+
is sufficient. This first slice provides a read-only storage admission check.

```sh
python3 -m unittest discover -s tests -v
python3 -m automation_kit.capacity --thin-pool vg/pool \
  --container 123 --service-user worker --peak-bytes 1610612736
```

The live collector runs on the Proxmox host with permission to read LVM and use
`pct exec`. It reads filesystem capacity as the service user. It never deletes
files, changes LVM, starts jobs, or sends notifications. Site addresses, secrets,
container IDs and deployment configuration belong outside this repository.

Exit 0 admits the stated peak workload; exit 2 blocks it. The caller must check
this result immediately before work and stop its dependents on failure. A check
does not reserve storage against other writers. Each scheduler must serialize
its own bounded work; uncoordinated parallel builds are outside this guarantee.

Required available space is the larger of 6 GiB and twice the supplied peak plus
1 GiB recovery and 2 GiB concurrent-writer reserves. The projected thin-pool data
use must remain below 90%; metadata below 70%. Missing, malformed, older-than-60s,
future-dated, unhealthy or read-only evidence blocks admission. For larger
workloads, include downloads, caches, intermediate exports and overlapping
outputs in the peak estimate. Do not reduce the estimate to force admission.

JSON output uses a fixed schema and safe reason codes. Raw command errors,
customer data and arbitrary input fields are omitted. No telemetry is stored.
`--observation path.json` permits offline evaluation of synthetic observations.

This component has no dependency on n8n and does not change any upstream license.
It is an initial component, not the complete automation platform. Deployment,
scheduled health reporting, durable workflow receipts and business adapters are
separate slices subject to their own tests and review.
