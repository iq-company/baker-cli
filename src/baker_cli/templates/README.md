# Bake: {{ project_name }}

This repository contains a minimal Baker setup to bake Images for: {{ project_name }}

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
baker plan --check local --targets base

# Build and push
baker build --check remote --push --targets base
```

## CI Workflow (GitHub Actions)

```bash
# Generate or update the workflow based on build-settings.yml
baker ci gh
```
