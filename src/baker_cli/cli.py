import sys
from pathlib import Path
import shutil
import os
import yaml
import typer
from typing import List, Optional
from importlib.resources import files as pkg_files
from jinja2 import Environment, BaseLoader

from . import core


app = typer.Typer(add_completion=False, help="baker-cli", rich_markup_mode="markdown", context_settings={"help_option_names": ["-h", "--help"]})
image_app = typer.Typer(add_completion=False, help="Add a new image target")
app.add_typer(image_app, name="image")

# Supported CI providers and their templates/default output locations
CI_PIPELINES = {
	"gh": {
		"template": ("ci", "github-actions.yml.j2"),
		"default_output": ".github/workflows/baker.yml",
		"label": "GitHub Actions",
	},
}


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


def _sanitize_name(name: str) -> str:
	s = name.strip().lower()
	s = s.replace(" ", "-")
	s = "".join(ch for ch in s if (ch.isalnum() or ch in "-_."))
	s = s.strip("-._")
	if not s:
		typer.echo("Invalid name for image", err=True)
		raise typer.Exit(code=2)
	return s


def _dep_var_name(dep: str) -> str:
	return f"IMAGE_{dep.replace('-', '_').upper()}"


def _dockerfile_for_image(name: str, deps: List[str], base_image: str | None) -> str:
	lines = []
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


def _read_settings(path: Path) -> dict:
	with path.open("r", encoding="utf-8") as f:
		data = yaml.safe_load(f) or {}
	if not isinstance(data, dict) or "targets" not in data:
		raise typer.Exit(code=2)
	return data


def load_template(section: str, name: str) -> str:
	res = pkg_files("baker_cli").joinpath("templates", section, name)
	return res.read_text(encoding="utf-8")


def _compute_leaf_targets(settings: dict) -> List[str]:
	tdefs = settings.get("targets") or {}
	all_names = set(tdefs.keys())
	dep_names = set()
	for t in tdefs.values():
		for d in (t.get("deps") or []):
			dep_names.add(d)
	leafs = sorted(all_names - dep_names)
	return leafs or sorted(all_names)


def _render_ci(settings: dict, provider: str) -> str:
	if provider not in CI_PIPELINES:
		allowed = ", ".join(sorted(CI_PIPELINES.keys()))
		typer.echo(f"Unknown CI provider '{provider}'. Allowed: {allowed}", err=True)
		raise typer.Exit(code=2)

	leaf_targets = _compute_leaf_targets(settings)
	if not leaf_targets:
		raise typer.Exit(code=2)
	registry = (settings.get("registry") or "ghcr.io").strip() or "ghcr.io"

	section, name = CI_PIPELINES[provider]["template"]
	jinja_src = load_template(section, name)
	# No trimming: preserve line breaks exactly
	env = Environment(loader=BaseLoader(), autoescape=False, keep_trailing_newline=True)
	tmpl = env.from_string(jinja_src)
	return tmpl.render(registry=registry, targets=leaf_targets)


@app.command("init", help="Initialize a new baker project")
def init_cmd(
	target: Optional[str] = typer.Argument(None, help="Target folder (default: cwd)"),
):
	target_path = Path(target or ".").resolve()
	# Derive project name from target folder (or CWD if no target provided)
	project_name = (Path(target).name if target else Path.cwd().name)
	project_slug = _sanitize_name(project_name)
	templates_root = Path(pkg_files("baker_cli") / "templates")
	# Copy project templates (excluding CI templates)
	files_to_copy = [
		templates_root / "build-settings.yml",
		# Additional helper files, if not present
		templates_root / "README.md",
		templates_root / "pyproject.toml",
		templates_root / ".gitignore",
		templates_root / ".envrc",
	]
	dirs_to_copy = [
		templates_root / "docker",
	]

	# Jinja environment for text templates (no autoescape, preserve newlines)
	env = Environment(loader=BaseLoader(), autoescape=False, keep_trailing_newline=True)

	for fp in files_to_copy:
		if fp.exists():
			target_path.mkdir(parents=True, exist_ok=True)
			out = target_path / fp.name
			if not out.exists():
				# .envrc and .gitignore are copied unchanged, all others are rendered
				if fp.name in (".envrc", ".gitignore"):
					shutil.copy2(fp, out)
				else:
					src_txt = fp.read_text(encoding="utf-8")
					tmpl = env.from_string(src_txt)
					rendered = tmpl.render(project_name=project_name, project_slug=project_slug)
					out.write_text(rendered, encoding="utf-8")
	for dp in dirs_to_copy:
		if dp.exists():
			copy_tree(dp, target_path / dp.name)

	typer.echo(f"Initialized templates in {target_path}")


@app.command("ci", help="Generate a CI workflow Pipeline for a specific CI provider")
def ci_cmd(
	provider: str = typer.Argument(..., help="CI provider (e.g., 'gh' for GitHub Actions)"),
	settings: str = typer.Option("build-settings.yml", "--settings"),
	output: Optional[str] = typer.Option(None, "--output"),
):
	settings_path = Path(settings)
	data = _read_settings(settings_path)
	out_yaml = _render_ci(data, provider)
	# Default output depends on provider
	default_out = CI_PIPELINES.get(provider, {}).get("default_output", "baker-ci.yml")
	out_path = Path(output or default_out)
	out_path.parent.mkdir(parents=True, exist_ok=True)
	out_path.write_text(out_yaml, encoding="utf-8")
	label = CI_PIPELINES.get(provider, {}).get("label", provider)
	typer.echo(f"Wrote {label} workflow to {out_path}")


@app.command("plan", help="Show the plan and what would build")
def plan_cmd(
    settings: str = typer.Option("build-settings.yml", "--settings", help="Path to settings.yml (default: build-settings.yml)"),
    set_override: List[str] = typer.Option([], "--set", help="Override config property, e.g. --set push=false or --set targets.srv.latest=false"),
    targets: Optional[List[str]] = typer.Option(None, "--targets", help="targets (default: all)"),
    force: List[str] = typer.Option([], "--force", help="force specific targets"),
    skip: List[str] = typer.Option([], "--skip", help="skip specific targets"),
    end: List[str] = typer.Option([], "--end", help="stop planning at these targets"),
    check: Optional[str] = typer.Option(None, "--check", help="where to check image existence (auto|local|remote)", case_sensitive=False),
    push: Optional[bool] = typer.Option(None, "--push/--no-push", help="override push behavior"),
    json_out: bool = typer.Option(False, "--json", help="machine-readable output"),
    print_env: bool = typer.Option(False, "--print-env", help="print TAG_<TARGET> vars"),
):
    class Args:
        pass
    args = Args()
    args.targets = targets
    args.force = force
    args.skip = skip
    args.end = end
    args.check = check
    args.push = push
    args.json = json_out
    args.print_env = print_env
    args.overrides = set_override
    s = core.load_settings(settings)
    # apply overrides and coerce
    for ov in set_override:
        if "=" not in ov:
            raise typer.Exit(code=2)
        k, v = ov.split("=", 1)
        core.set_deep(s, k.strip(), v)
    s = core.coerce_bools(s)
    selected, primary_tags, all_tags_map, to_build, decisions = core.plan(s, args)
    if json_out:
        import json as _json
        typer.echo(_json.dumps({"selected": selected, "decisions": decisions}, indent=2))
        return
    if print_env:
        for n in selected:
            envname = f"TAG_{n.replace('-', '_').upper()}"
            typer.echo(f"{envname}={decisions[n]['primary_tag']}")
    for n in selected:
        d = decisions[n]
        mark = "BUILD" if d["build"] else "skip"
        all_tags_str = ",".join(d["all_tags"])
        typer.echo(f"{n:<22} {d['primary_tag']:<12} {mark:5} ({d['reason']})  {d['ref']}  [{all_tags_str}]")
    typer.echo("\nWill build:" if to_build else "\nNothing to build.", nl=False)
    if to_build:
        typer.echo(" " + ", ".join(to_build))


@app.command("gen-hcl", help="Generate a docker-bake.hcl file from the build-settings.yml for debugging purposes")
def gen_hcl_cmd(
    settings: str = typer.Option("build-settings.yml", "--settings"),
    set_override: List[str] = typer.Option([], "--set"),
    targets: Optional[List[str]] = typer.Option(None, "--targets"),
    force: List[str] = typer.Option([], "--force"),
    skip: List[str] = typer.Option([], "--skip"),
    end: List[str] = typer.Option([], "--end"),
    check: Optional[str] = typer.Option(None, "--check"),
    push: Optional[bool] = typer.Option(None, "--push/--no-push"),
    output: str = typer.Option("docker-bake.hcl", "-o", "--output"),
):
    class Args:
        pass
    args = Args()
    args.targets = targets
    args.force = force
    args.skip = skip
    args.end = end
    args.check = check
    args.push = push
    args.overrides = set_override
    s = core.load_settings(settings)
    for ov in set_override:
        if "=" not in ov:
            raise typer.Exit(code=2)
        k, v = ov.split("=", 1)
        core.set_deep(s, k.strip(), v)
    s = core.coerce_bools(s)
    selected = core.select_targets(s, targets)
    primary_tags, all_tags_map = core.compute_tags(s, selected)
    hcl = core.gen_hcl(s, primary_tags, all_tags_map, targets_subset=selected)
    if output == "-":
        typer.echo(hcl, nl=False)
    else:
        Path(output).write_text(hcl, encoding="utf-8")
        typer.echo(f"Wrote {output}")


@app.command("build", help="Build the Docker images")
def build_cmd(
    settings: str = typer.Option("build-settings.yml", "--settings"),
    set_override: List[str] = typer.Option([], "--set"),
    targets: Optional[List[str]] = typer.Option(None, "--targets"),
    force: List[str] = typer.Option([], "--force"),
    skip: List[str] = typer.Option([], "--skip"),
    end: List[str] = typer.Option([], "--end"),
    check: Optional[str] = typer.Option(None, "--check"),
    push: Optional[bool] = typer.Option(None, "--push/--no-push"),
    keep_hcl: bool = typer.Option(False, "--keep-hcl", help="keep temporary .bake file"),
):
    class Args:
        pass
    args = Args()
    args.targets = targets
    args.force = force
    args.skip = skip
    args.end = end
    args.check = check
    args.push = push
    args.overrides = set_override
    args.keep_hcl = keep_hcl
    s = core.load_settings(settings)
    for ov in set_override:
        if "=" not in ov:
            raise typer.Exit(code=2)
        k, v = ov.split("=", 1)
        core.set_deep(s, k.strip(), v)
    s = core.coerce_bools(s)
    selected, primary_tags, all_tags_map, to_build, decisions = core.plan(s, args)
    core.do_build(s, args, to_build, primary_tags, all_tags_map)


@image_app.command("add", help="Add a new image target")
def image_add_cmd(
	name: str = typer.Argument(..., help="Name of the image (e.g. release)"),
	dep: List[str] = typer.Option([], "--dep", help="Dependencies, multiple or comma-separated"),
	image: Optional[str] = typer.Option(None, "--image", help="Base image (e.g. alpine:3)"),
	settings: str = typer.Option("build-settings.yml", "--settings", help="Path to the settings YAML"),
	force: bool = typer.Option(False, "--force", help="Overwrite existing files/targets"),
):
	settings_path = Path(settings)
	if not settings_path.exists():
		typer.echo(f"Settings file not found: {settings_path}", err=True)
		raise typer.Exit(code=2)

	pname = _sanitize_name(name)
	deps: List[str] = []
	for item in dep:
		for part in str(item).split(","):
			p = _sanitize_name(part)
			if p and p not in deps:
				deps.append(p)

	with settings_path.open("r", encoding="utf-8") as f:
		settings_data = yaml.safe_load(f) or {}
	if "targets" not in settings_data or not isinstance(settings_data["targets"], dict):
		settings_data["targets"] = {}

	if pname in settings_data["targets"] and not force:
		typer.echo(f"Target '{pname}' already exists. Use --force to overwrite.", err=True)
		raise typer.Exit(code=2)

	docker_dir = Path("docker") / pname
	docker_dir.mkdir(parents=True, exist_ok=True)
	df_path = docker_dir / "Dockerfile"
	if df_path.exists() and not force:
		typer.echo(f"Dockerfile already exists: {df_path}. Use --force.", err=True)
		raise typer.Exit(code=2)
	df_content = _dockerfile_for_image(pname, deps, image)
	df_path.write_text(df_content, encoding="utf-8")

	target_def = {
		"dockerfile": str(df_path).replace("\\", "/"),
		"context": ".",
		"deps": deps,
		"hash_mode": "self+deps" if deps else "self",
		"image": f"baker-{pname}",
		"latest": True,
	}
	settings_data["targets"][pname] = target_def

	with settings_path.open("w", encoding="utf-8") as f:
		yaml.safe_dump(settings_data, f, sort_keys=False, allow_unicode=True)

	typer.echo(f"Image '{pname}' added. Dockerfile: {df_path}")


@app.command("version", help="Show the version of the baker-cli")
def version_cmd():
	from . import __version__

	typer.echo(f"baker-cli version {__version__}")

def main(argv: List[str] | None = None) -> None:
    app()
