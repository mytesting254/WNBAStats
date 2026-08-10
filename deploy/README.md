# Deployment Templates

This directory contains copy-ready VM deployment templates for the local SQLite
runtime.

Files:

- `wnba-backend.service`: `systemd` unit for the FastAPI backend
- `nginx-wnba-stats.conf`: nginx site config for the frontend and `/api` proxy
- `wnba-daily-props.cron`: host-cron template for daily ESPN roster/results refresh, Specials prune, core settlement/training, standalone DFS prewarm, and odds refresh
- `wnba-stocks-prep.cron`: host-cron template for 11pm ET Specials stocks prep

Before installing them on a VM:

- replace `YOUR_VM_USER` in `wnba-backend.service`
- replace `YOUR_DOMAIN_OR_VM_IP` in `nginx-wnba-stats.conf`
- confirm the app path is `/opt/wnba-stats`, or update both files to match
