# Baker Setup

This repository contains a minimal Baker setup: build-settings.yml and Dockerfiles under docker/.

## Install (recommended local .venv)

```bash
python -m venv .venv
source .venv/bin/activate

# Upgrade pip and install this repo (pulls baker-cli)
pip install -U pip
pip install .
```

## Build locally

```bash
# Show plan
baker plan --settings build-settings.yml --check local --targets base

# Build and push
baker build --settings build-settings.yml --check remote --push --targets base
```

## CI Workflow (GitHub Actions)

```bash
# Generate or update the workflow based on build-settings.yml
baker ci gh
```
