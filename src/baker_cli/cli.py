import argparse
import sys
from pathlib import Path
import shutil
import os
import yaml
from importlib.resources import files as pkg_files

from . import core


def copy_tree(src: Path, dst: Path) -> None:
	for root, dirs, files in os.walk(src):
		rel = Path(root).relative_to(src)
		target_root = dst / rel
		target_root.mkdir(parents=True, exist_ok=True)
		for f in files:
			src_f = Path(root) / f
			dst_f = target_root / f
			if not dst_f.exists():
				shutil.copy2(src_f, dst_f)


def cmd_init(args: argparse.Namespace) -> int:
	"""Initialisiert ein Projekt mit Templates."""
	target = Path(args.target or ".").resolve()
	templates_dir = Path(pkg_files("baker_cli") / "templates")
	copy_tree(templates_dir, target)
	print(f"Initialized templates in {target}")
	if args.ci:
		return cmd_ci(argparse.Namespace(settings=str(target / "build-settings.yml"), output=str(target / ".github/workflows/baker.yml")))
	return 0


def read_settings(path: Path) -> dict:
	with path.open("r", encoding="utf-8") as f:
		data = yaml.safe_load(f) or {}
	if not isinstance(data, dict) or "targets" not in data:
		raise SystemExit("Invalid build-settings.yml: 'targets' missing")
	return data


def generate_github_actions_yaml(settings: dict) -> str:
	targets = list((settings.get("targets") or {}).keys())
	if not targets:
		raise SystemExit("No targets defined in settings")
	registry = (settings.get("registry") or "ghcr.io").strip() or "ghcr.io"
	yaml_lines = [
		"name: Baker Build and Push",
		"",
		"on:",
		"  push:",
		"    branches: [ main ]",
		"  pull_request:",
		"    branches: [ main ]",
		"",
		"jobs:",
		"  build:",
		"    runs-on: ubuntu-latest",
		"    permissions:",
		"      contents: read",
		"      packages: write",
		"    env:",
		f"      REGISTRY: {registry}",
		"      OWNER: \${{ github.repository_owner }}",
		"    strategy:",
		"      fail-fast: false",
		"      matrix:",
		"        target:",
		*[f"          - {t}" for t in targets],
		"    steps:",
		"      - uses: actions/checkout@v4",
		"      - name: Set up QEMU",
		"        uses: docker/setup-qemu-action@v3",
		"      - name: Set up Docker Buildx",
		"        uses: docker/setup-buildx-action@v3",
		"      - name: Log in to Registry",
		"        uses: docker/login-action@v3",
		"        with:",
		"          registry: \${{ env.REGISTRY }}",
		"          username: \${{ github.actor }}",
		"          password: \${{ secrets.GITHUB_TOKEN }}",
		"      - name: Set up Python",
		"        uses: actions/setup-python@v5",
		"        with:",
		"          python-version: '3.x'",
		"      - name: Install baker-cli",
		"        run: pip install baker-cli",
		"      - name: Build",
		"        run: baker build --check remote --push --targets \${{ matrix.target }}",
	]
	return "\n".join(yaml_lines) + "\n"


def cmd_ci(args: argparse.Namespace) -> int:
	settings_path = Path(args.settings or "build-settings.yml")
	settings = read_settings(settings_path)
	out_yaml = generate_github_actions_yaml(settings)
	out_path = Path(args.output or ".github/workflows/baker.yml")
	out_path.parent.mkdir(parents=True, exist_ok=True)
	out_path.write_text(out_yaml, encoding="utf-8")
	print(f"Wrote GitHub Actions workflow to {out_path}")
	return 0

# ---------------- image add ----------------

def _sanitize_name(name: str) -> str:
	s = name.strip().lower()
	s = s.replace(" ", "-")
	s = "".join(ch for ch in s if (ch.isalnum() or ch in "-_."))
	s = s.strip("-._")
	if not s:
		raise SystemExit("Ungültiger Name für Image")
	return s


def _dep_var_name(dep: str) -> str:
	return f"IMAGE_{dep.replace('-', '_').upper()}"


def _dockerfile_for_image(name: str, deps: list[str], base_image: str | None) -> str:
	lines = []
	# ARGs mit Defaults; Buildx setzt diese per Auto-Args im HCL
	for d in deps:
		var = _dep_var_name(d)
		lines.append(f"ARG {var}=builder-{d}:latest")
	for d in deps:
		var = _dep_var_name(d)
		stage = d.replace('-', '_')
		lines.append(f"FROM ${{{var}}} AS {stage}")
	lines.append("")
	final_from = base_image or "alpine:3.20"
	lines.append(f"FROM {final_from}")
	lines.append("")
	return "\n".join(lines) + "\n"


def cmd_image_add(args: argparse.Namespace) -> int:
	settings_path = Path(args.settings or "build-settings.yml")
	if not settings_path.exists():
		raise SystemExit(f"Settings-Datei nicht gefunden: {settings_path}")

	name = _sanitize_name(args.name)
	deps: list[str] = []
	for item in (args.dep or []):
		for part in str(item).split(","):
			p = _sanitize_name(part)
			if p and p not in deps:
				deps.append(p)
	base_image = args.image

	# YAML laden
	with settings_path.open("r", encoding="utf-8") as f:
		settings = yaml.safe_load(f) or {}
	if "targets" not in settings or not isinstance(settings["targets"], dict):
		settings["targets"] = {}

	if name in settings["targets"] and not args.force:
		raise SystemExit(f"Target '{name}' existiert bereits. Verwende --force zum Überschreiben.")

	# Dockerfile schreiben
	docker_dir = Path("docker") / name
	docker_dir.mkdir(parents=True, exist_ok=True)
	df_path = docker_dir / "Dockerfile"
	if df_path.exists() and not args.force:
		raise SystemExit(f"Dockerfile existiert bereits: {df_path}. Verwende --force.")
	df_content = _dockerfile_for_image(name, deps, base_image)
	df_path.write_text(df_content, encoding="utf-8")

	# Target-Eintrag aktualisieren
	target_def = {
		"dockerfile": str(df_path).replace("\\", "/"),
		"context": ".",
		"deps": deps,
		"hash_mode": "self+deps" if deps else "self",
		"image": f"builder-{name}",
		"latest": True,
	}
	settings["targets"][name] = target_def

	# YAML speichern
	with settings_path.open("w", encoding="utf-8") as f:
		yaml.safe_dump(settings, f, sort_keys=False, allow_unicode=True)

	print(f"Image '{name}' hinzugefügt. Dockerfile: {df_path}")
	return 0

# --------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
	ap = argparse.ArgumentParser(prog="baker", description="baker-cli")
	sub = ap.add_subparsers(dest="cmd", required=True)

	p_init = sub.add_parser("init", help="Projektvorlagen initialisieren")
	p_init.add_argument("target", nargs="?", default=".", help="Zielordner (default: cwd)")
	p_init.add_argument("ci_literal", nargs="?", help=argparse.SUPPRESS)  # erlaubt 'baker init ci'
	p_init.add_argument("--ci", action="store_true", help="CI-Workflow zusätzlich generieren")
	p_init.set_defaults(func=lambda a: cmd_init(a))

	p_ci = sub.add_parser("ci", help="GitHub Actions Workflow generieren/aktualisieren")
	p_ci.add_argument("--settings", default="build-settings.yml")
	p_ci.add_argument("--output", default=".github/workflows/baker.yml")
	p_ci.set_defaults(func=cmd_ci)

	# Kernkommandos an core delegieren
	p_plan = sub.add_parser("plan", help="Plan anzeigen (delegiert an Core)")
	p_plan.add_argument("args", nargs=argparse.REMAINDER)
	p_plan.set_defaults(func=lambda a: core.core_main(["plan"] + a.args))

	p_hcl = sub.add_parser("gen-hcl", help="HCL generieren (delegiert an Core)")
	p_hcl.add_argument("args", nargs=argparse.REMAINDER)
	p_hcl.set_defaults(func=lambda a: core.core_main(["gen-hcl"] + a.args))

	p_build = sub.add_parser("build", help="Build ausführen (delegiert an Core)")
	p_build.add_argument("args", nargs=argparse.REMAINDER)
	p_build.set_defaults(func=lambda a: core.core_main(["build"] + a.args))

	# image add
	p_image = sub.add_parser("image", help="Image-Operationen")
	image_sub = p_image.add_subparsers(dest="image_cmd", required=True)
	p_image_add = image_sub.add_parser("add", help="Neues Image anlegen und in Config eintragen")
	p_image_add.add_argument("name", help="Name des Images (z.B. release)")
	p_image_add.add_argument("--dep", action="append", default=[], help="Abhängigkeit(en), mehrfach oder komma-getrennt")
	p_image_add.add_argument("--image", help="Basis-Image (z.B. alpine:3)")
	p_image_add.add_argument("--settings", default="build-settings.yml", help="Pfad zur settings YAML")
	p_image_add.add_argument("--force", action="store_true", help="Existierende Dateien/Targets überschreiben")
	p_image_add.set_defaults(func=cmd_image_add)

	# alias: add-image
	p_add_image = sub.add_parser("add-image", help="Alias für 'image add'")
	p_add_image.add_argument("name")
	p_add_image.add_argument("--dep", action="append", default=[])
	p_add_image.add_argument("--image")
	p_add_image.add_argument("--settings", default="build-settings.yml")
	p_add_image.add_argument("--force", action="store_true")
	p_add_image.set_defaults(func=cmd_image_add)

	return ap


def main(argv: list[str] | None = None) -> None:
	argv = list(sys.argv[1:] if argv is None else argv)
	# Support 'baker init ci' als Kurzform für '--ci'
	if len(argv) >= 2 and argv[0] == "init" and argv[1] == "ci":
		argv = ["init", "--ci"] + argv[2:]
	ap = build_parser()
	args = ap.parse_args(argv)
	res = args.func(args)
	if isinstance(res, int):
		sys.exit(res)
