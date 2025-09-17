# baker-cli

A small, pragmatic Python CLI that controls your Docker build cascades **uniformly locally and in CI**:

* **Targets & Bundles** are defined in **YAML**
* **Tags** are created by **checksum** (self / self+deps) *or* by **expressions** (ENV, files, Git-SHA, ...)
* **Build only when necessary**: Existence check locally or in registry
* Optionally generates a **`docker-bake.hcl`** and builds via **`docker buildx bake`**
* **Build-Args** are definable, get interpolated and **flow into the hash**
* Configuration values can be **overridden via CLI** (`--set key=value`)

---

## Contents

* [Quickstart](#quickstart)
* [Prerequisites](#prerequisites)
* [Repository Layout](#repository-layout)
* [Configuration (`build-settings.yml`)](#configuration-build-settingsyml)

  * [Targets](#targets)
  * [Bundles](#bundles)
  * [Interpolation & Expressions](#interpolation--expressions)
  * [Tag Expressions (Functions)](#tag-expressions-functions)
  * [Build-Args & Hashing](#build-args--hashing)
* [CLI](#cli)

  * [`plan`](#plan)
  * [`gen-hcl`](#gen-hcl)
  * [`build`](#build)
  * [Global Overrides (`--set`)](#global-overrides---set)
* [Existence Check & Push Strategy](#existence-check--push-strategy)
* [GitHub Actions Example](#github-actions-example)
* [Tips & Best Practices](#tips--best-practices)
* [Troubleshooting](#troubleshooting)
* [Security Notes](#security-notes)

---

## Quickstart

### 1) Installation with venv (Recommended)

```bash
mkdir my-project

cd my-project

python3 -m venv .venv
source .venv/bin/activate

pip install baker-cli

baker init
```

### 2) Global installation (pip/pipx)

```bash
# With pip
pip install baker-cli

# Or with pipx (recommended for global CLIs)
pipx install baker-cli

# Initialize project (current directory or target folder)
baker init
# or
baker init ./my-project
```

### 2) Development (local, .venv)

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install project locally (editable)
pip install -U pip
pip install -e .

# Initialize project (if not yet present)
baker init

# Optional: Generate CI workflow
baker ci --settings build-settings.yml

# Example: Plan & Build
baker plan
baker build --push --targets base
```

---

## Prerequisites

* **Python 3.11+**
* **Docker** (with `buildx` plugin)

---

## Repository Layout

```
demo/                           # Project name
├── build-settings.yml          # Build configuration
├── sqlite/                     # Sample Stage "sqlite"
│   └── Dockerfile              # Related Dockerfile
└── ui/                         # Sample Stage "ui"
    └── Dockerfile              # Related Dockerfile
```

---

## Configuration (`build-settings.yml`)

### Targets

```yaml
targets:
  cascade-base:
    dockerfile: Dockerfile.sqlite
    context: .
    tags:
      - "cascade-base:{{ checksum_self }}"
    build-args:
      CONDUCTOR_VERSION: "3.16.0"
      JAVA_VERSION: "17"

  cascade-ui:
    dockerfile: ui/Dockerfile
    context: .
    tags:
      - "cascade-ui:{{ checksum_self }}"
    depends_on:
      - cascade-base
    build-args:
      BASE_IMAGE: "cascade-base:{{ checksum_self }}"
```

### Bundles

```yaml
bundles:
  all:
    targets:
      - cascade-base
      - cascade-ui

  sqlite:
    targets:
      - cascade-base
```

### Interpolation & Expressions

```yaml
targets:
  my-target:
    tags:
      - "my-app:{{ env.BUILD_VERSION }}"
      - "my-app:{{ git.short_sha }}"
      - "my-app:{{ file_hash('package.json') }}"
    build-args:
      VERSION: "{{ env.BUILD_VERSION }}"
      COMMIT_SHA: "{{ git.full_sha }}"
```

### Tag Expressions (Functions)

* `{{ checksum_self }}` - Hash of Dockerfile + context
* `{{ checksum_deps }}` - Hash of dependencies
* `{{ env.VAR_NAME }}` - Environment variable
* `{{ git.short_sha }}` - Short Git commit hash
* `{{ git.full_sha }}` - Full Git commit hash
* `{{ file_hash('path/to/file') }}` - Hash of specific file
* `{{ timestamp }}` - Current timestamp

### Build-Args & Hashing

Build-args are interpolated and included in the hash calculation:

```yaml
targets:
  my-target:
    build-args:
      VERSION: "{{ env.BUILD_VERSION }}"
      FEATURE_FLAG: "{{ env.ENABLE_FEATURE }}"
    # These args flow into the checksum calculation
```

---

## CLI

### `plan`

Show what would be built:

```bash
# Show plan for specific targets
python baker.py plan --targets cascade-base

# Show plan with existence check
python baker.py plan --check local --targets cascade-base

# Show plan for bundles
python baker.py plan --targets all
```

### `gen-hcl`

Generate `docker-bake.hcl` file:

```bash
# Generate HCL file
python baker.py gen-hcl --targets cascade-base

# Generate for all targets
python baker.py gen-hcl --targets all
```

### `build`

Build Docker images:

```bash
# Build locally
python baker.py build --check local --push=off --targets cascade-base

# Build and push
python baker.py build --check registry --push=on --targets cascade-base

# Build with specific registry
python baker.py build --registry my-registry.com --push=on --targets cascade-base
```

### Global Overrides (`--set`)

Override configuration values:

```bash
# Override build args
python baker.py build --set CONDUCTOR_VERSION=3.17.0 --targets cascade-base

# Override multiple values
python baker.py build --set CONDUCTOR_VERSION=3.17.0 --set JAVA_VERSION=21 --targets cascade-base
```

---

## Existence Check & Push Strategy

### Local Check
```bash
python baker.py build --check local --push=off --targets cascade-base
```
* Checks if image exists locally
* Skips build if found

### Registry Check
```bash
python baker.py build --check registry --push=on --targets cascade-base
```
* Checks if image exists in registry
* Skips build if found
* Pushes after successful build

### No Check
```bash
python baker.py build --check=off --push=on --targets cascade-base
```
* Always builds
* Pushes after successful build

---

## GitHub Actions Example

```yaml
name: Build and Push

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.9'

      - name: Install dependencies
        run: pip install pyyaml

      - name: Build images
        run: |
          python baker.py build \
            --check registry \
            --push=on \
            --targets all \
            --set BUILD_VERSION=${{ github.sha }}
```

---

## Tips & Best Practices

### 1. Use Checksums for Reproducible Builds
```yaml
targets:
  my-target:
    tags:
      - "my-app:{{ checksum_self }}"
```

### 2. Leverage Dependencies
```yaml
targets:
  base:
    dockerfile: Dockerfile.base

  app:
    dockerfile: Dockerfile.app
    depends_on:
      - base
    build-args:
      BASE_IMAGE: "base:{{ checksum_self }}"
```

### 3. Use Environment Variables for Dynamic Values
```yaml
targets:
  my-target:
    build-args:
      VERSION: "{{ env.BUILD_VERSION }}"
      COMMIT_SHA: "{{ git.short_sha }}"
```

### 4. Group Related Targets in Bundles
```yaml
bundles:
  production:
    targets:
      - base
      - app
      - worker

  development:
    targets:
      - base
      - dev-tools
```

---

## Troubleshooting

### Common Issues

#### 1. Docker Buildx Not Available
```bash
# Enable buildx
docker buildx create --use
```

#### 2. Registry Authentication
```bash
# Login to registry
docker login my-registry.com
```

#### 3. Build Context Issues
```yaml
# Ensure context includes all necessary files
targets:
  my-target:
    context: .  # Use project root
    dockerfile: path/to/Dockerfile
```

#### 4. Tag Collisions
```yaml
# Use unique tags
targets:
  my-target:
    tags:
      - "my-app:{{ checksum_self }}"
      - "my-app:latest"  # Only if appropriate
```

---

## Security Notes

### 1. Build-Args Security
* Build-args are visible in image history
* Don't pass secrets via build-args
* Use multi-stage builds for sensitive data

### 2. Registry Security
* Use authenticated registries
* Scan images for vulnerabilities
* Use specific tags, avoid `latest`

### 3. Context Security
* Use `.dockerignore` to exclude sensitive files
* Minimize build context size
* Review Dockerfile for security best practices

---

## Advanced Usage

### Custom Tag Functions
```yaml
targets:
  my-target:
    tags:
      - "my-app:{{ env.BUILD_VERSION }}-{{ git.short_sha }}"
      - "my-app:{{ file_hash('package.json') }}"
```

### Conditional Builds
```yaml
targets:
  my-target:
    dockerfile: Dockerfile
    tags:
      - "my-app:{{ checksum_self }}"
    # Only build if specific conditions are met
    build-args:
      BUILD_TYPE: "{{ env.BUILD_TYPE }}"
```

### Multi-Architecture Builds
```yaml
targets:
  my-target:
    platforms:
      - linux/amd64
      - linux/arm64
    tags:
      - "my-app:{{ checksum_self }}"
```

---

This baker-cli provides a powerful yet simple way to manage Docker builds with consistency between local development and CI/CD pipelines.
