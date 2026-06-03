# Deployment Templates

This directory contains copy-ready VM deployment templates for the local SQLite
runtime.

Files:

- `wnba-backend.service`: `systemd` unit for the FastAPI backend
- `nginx-wnba-stats.conf`: nginx site config for the frontend and `/api` proxy

Before installing them on a VM:

- replace `YOUR_VM_USER` in `wnba-backend.service`
- replace `YOUR_DOMAIN_OR_VM_IP` in `nginx-wnba-stats.conf`
- confirm the app path is `/opt/wnba-stats`, or update both files to match
