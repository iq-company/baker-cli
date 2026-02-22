#!/usr/bin/env python3
"""
Dockerfile generation from Jinja2 templates with variant-specific recipes.

This module provides:
- Recipe system: Platform-specific code blocks (e.g., apt vs apk)
- Template rendering: Jinja2 templates that use recipes
- Variant support: debian, alpine, etc.
"""

from pathlib import Path
from typing import Any
import yaml

from jinja2 import Environment, FileSystemLoader, BaseLoader, ChoiceLoader


# Default recipes are loaded from templates/recipes/*.yml
# This dict is populated at runtime by _load_builtin_recipes()
DEFAULT_RECIPES: dict[str, dict[str, str]] = {}

_BUILTIN_RECIPES_LOADED = False


def _load_builtin_recipes() -> dict[str, dict[str, str]]:
    """Load built-in recipes from baker-cli's templates/recipes/ directory."""
    global DEFAULT_RECIPES, _BUILTIN_RECIPES_LOADED

    if _BUILTIN_RECIPES_LOADED:
        return DEFAULT_RECIPES

    from importlib.resources import files as pkg_files

    try:
        recipes_dir = pkg_files("baker_cli").joinpath("templates", "recipes")

        # Load all .yml files from the package
        for item in sorted(recipes_dir.iterdir()):
            if item.name.endswith(".yml") or item.name.endswith(".yaml"):
                content = item.read_text(encoding="utf-8")
                data = yaml.safe_load(content) or {}
                file_recipes = data.get("recipes", {})

                for name, variants in file_recipes.items():
                    if name not in DEFAULT_RECIPES:
                        DEFAULT_RECIPES[name] = {}
                    DEFAULT_RECIPES[name].update(variants)
    except Exception:
        # Fallback: no built-in recipes available
        pass

    _BUILTIN_RECIPES_LOADED = True
    return DEFAULT_RECIPES


# Default configuration (can be overridden via build-settings.yml dockerfile_defaults or variant config)
GLOBAL_DEFAULTS = {
    "python_version": "3.12",
    "debian_base": "bookworm",
    "alpine_version": "3.20",
    "pg_version": "16.4",
    "nginx_version": "1.27.4",
    "node_version": "20",
}

# Variant-specific base images (use {python_version} placeholder)
DEFAULT_BASE_IMAGES = {
    "debian": "python:{python_version}-slim-{debian_base}",
    "alpine": "python:{python_version}-alpine{alpine_version}",
}


class RecipeRegistry:
    """Registry for Dockerfile recipes (platform-specific code blocks)."""

    def __init__(self, recipes: dict[str, dict[str, str]] | None = None):
        # Load built-in recipes first
        builtin = _load_builtin_recipes()
        self.recipes = {k: dict(v) for k, v in builtin.items()}

        if recipes:
            # Deep merge user/project recipes (override built-ins)
            for name, variants in recipes.items():
                if name not in self.recipes:
                    self.recipes[name] = {}
                self.recipes[name].update(variants)

    def get(self, name: str, variant: str, **kwargs) -> str:
        """Get a recipe for a specific variant, rendered with kwargs."""
        if name not in self.recipes:
            raise KeyError(f"Unknown recipe: {name}")

        recipe_variants = self.recipes[name]

        # Priority: variant-specific -> _default -> empty string
        template_str = recipe_variants.get(variant) or recipe_variants.get("_default", "")

        if not template_str:
            return ""

        # Render the recipe template with kwargs
        env = Environment(loader=BaseLoader(), autoescape=False, keep_trailing_newline=True)
        tmpl = env.from_string(template_str)
        return tmpl.render(**kwargs)

    def has(self, name: str, variant: str) -> bool:
        """Check if a recipe exists for a variant (or as default)."""
        if name not in self.recipes:
            return False
        recipe_variants = self.recipes[name]
        return bool(recipe_variants.get(variant) or recipe_variants.get("_default"))


class DockerfileGenerator:
    """Generates Dockerfiles from Jinja2 templates with recipe support."""

    def __init__(
        self,
        template_dir: Path,
        recipes: dict[str, dict[str, str]] | None = None,
        variant: str = "debian",
        defaults: dict[str, Any] | None = None,
    ):
        self.template_dir = Path(template_dir)
        self.variant = variant
        # Extract base variant (e.g., "debian" from "debian-trixie")
        self.base_variant = variant.split("-")[0] if "-" in variant else variant
        self.registry = RecipeRegistry(recipes)

        # Merge defaults: GLOBAL_DEFAULTS <- user defaults <- variant config
        self.defaults = dict(GLOBAL_DEFAULTS)
        if defaults:
            self.defaults.update(defaults)

        # Set up Jinja2 environment
        loaders = []
        if self.template_dir.exists():
            loaders.append(FileSystemLoader(str(self.template_dir)))

        self.env = Environment(
            loader=ChoiceLoader(loaders) if loaders else None,
            autoescape=False,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )

        # Compute base_image from template
        base_image_template = DEFAULT_BASE_IMAGES.get(self.base_variant, DEFAULT_BASE_IMAGES["debian"])
        base_image = base_image_template.format(**self.defaults)

        # Register custom functions/filters
        self.env.globals["recipe"] = self._recipe_func
        self.env.globals["recipe_raw"] = self._recipe_raw_func  # Returns without RUN prefix
        self.env.globals["has_recipe"] = self._has_recipe_func
        self.env.globals["variant"] = self.variant
        self.env.globals["base_variant"] = self.base_variant
        self.env.globals["base_image"] = base_image
        self.env.globals["defaults"] = self.defaults

        # Expose each default as top-level variable for convenience
        # (allows {{ project_slug }} in addition to {{ defaults.project_slug }})
        for k, v in self.defaults.items():
            if k not in self.env.globals:  # Don't override existing globals
                self.env.globals[k] = v

        # Layer helper for combining multiple commands
        self.env.globals["layer_start"] = lambda: "RUN set -ex; \\"
        self.env.globals["layer_join"] = lambda: " && \\"

    def _recipe_func(self, name: str, **kwargs) -> str:
        """Jinja2 function to include a recipe (with RUN prefix).

        Leading comment lines are moved before the RUN instruction to prevent
        broken Dockerfile syntax (e.g., "RUN # comment\\nchmod ..." would make
        "chmod" an orphaned instruction).
        """
        # Merge defaults into kwargs (kwargs take precedence)
        merged = {**self.defaults, **kwargs}
        content = self.registry.get(name, self.base_variant, **merged)
        # Add RUN prefix if not empty and doesn't have one
        content = content.rstrip()
        if content and not content.startswith("RUN ") and not content.startswith("COPY "):
            # Separate leading comment lines from commands
            lines = content.split("\n")
            leading_comments: list[str] = []
            while lines and lines[0].strip().startswith("#"):
                leading_comments.append(lines.pop(0))

            cmd_content = "\n".join(lines).strip()
            if cmd_content:
                prefix = "\n".join(leading_comments) + "\n" if leading_comments else ""
                content = prefix + "RUN " + cmd_content
            elif leading_comments:
                # Only comments, no commands — output as Dockerfile comments
                content = "\n".join(leading_comments)
        return content

    def _recipe_raw_func(self, name: str, **kwargs) -> str:
        """Jinja2 function to get recipe content without RUN prefix (for combining in layers)."""
        merged = {**self.defaults, **kwargs}
        content = self.registry.get(name, self.base_variant, **merged)
        # Strip leading "RUN " if present
        if content.startswith("RUN "):
            content = content[4:]
        # Strip trailing whitespace/newlines (important for combining in one RUN block)
        return content.rstrip()

    def _has_recipe_func(self, name: str) -> bool:
        """Jinja2 function to check if a recipe exists."""
        return self.registry.has(name, self.base_variant)

    def render(self, template_name: str, context: dict[str, Any] | None = None) -> str:
        """Render a Dockerfile template."""
        ctx = context or {}
        ctx.setdefault("variant", self.variant)

        template = self.env.get_template(template_name)
        return template.render(**ctx)

    def render_string(self, template_str: str, context: dict[str, Any] | None = None) -> str:
        """Render a Dockerfile from a template string."""
        ctx = context or {}
        ctx.setdefault("variant", self.variant)

        template = self.env.from_string(template_str)
        return template.render(**ctx)


def load_recipes_from_file(path: Path) -> dict[str, dict[str, str]]:
    """Load recipes from a YAML file."""
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    return data.get("recipes", {})


def load_recipes_from_dir(dir_path: Path) -> dict[str, dict[str, str]]:
    """
    Load and merge recipes from all YAML files in a directory.

    Files are loaded alphabetically, later files override earlier ones.
    This allows users to add custom recipes or override existing ones.
    """
    if not dir_path.exists() or not dir_path.is_dir():
        return {}

    merged = {}
    for yaml_file in sorted(dir_path.glob("*.yml")):
        file_recipes = load_recipes_from_file(yaml_file)
        for name, variants in file_recipes.items():
            if name not in merged:
                merged[name] = {}
            merged[name].update(variants)

    # Also check for .yaml extension
    for yaml_file in sorted(dir_path.glob("*.yaml")):
        file_recipes = load_recipes_from_file(yaml_file)
        for name, variants in file_recipes.items():
            if name not in merged:
                merged[name] = {}
            merged[name].update(variants)

    return merged


def load_all_recipes(settings: dict, base_path: Path | None = None) -> dict[str, dict[str, str]]:
    """
    Load recipes from all sources and merge them.

    Merge order (later overrides earlier):
    1. baker-cli built-in defaults (in DEFAULT_RECIPES)
    2. Project-level recipes (from recipes_dir or recipes_file)

    Settings can specify:
    - recipes_dir: Directory with multiple .yml files (loaded alphabetically)
    - recipes_file: Single file (legacy, also checks for recipes/ subdir)
    """
    merged = {}

    # 1. Built-in defaults are handled by RecipeRegistry

    # Determine recipes source
    recipes_dir_setting = settings.get("recipes_dir")
    recipes_file_setting = settings.get("recipes_file")

    if recipes_dir_setting:
        # 2a. Load from recipes directory
        recipes_dir = Path(recipes_dir_setting)
        if base_path and not recipes_dir.is_absolute():
            recipes_dir = base_path / recipes_dir

        dir_recipes = load_recipes_from_dir(recipes_dir)
        for name, variants in dir_recipes.items():
            if name not in merged:
                merged[name] = {}
            merged[name].update(variants)

    elif recipes_file_setting:
        # 2b. Load from single file (legacy)
        recipes_path = Path(recipes_file_setting)
        if base_path and not recipes_path.is_absolute():
            recipes_path = base_path / recipes_path

        file_recipes = load_recipes_from_file(recipes_path)
        for name, variants in file_recipes.items():
            if name not in merged:
                merged[name] = {}
            merged[name].update(variants)

        # Also check for recipes/ subdirectory next to the file
        recipes_subdir = recipes_path.parent / "recipes"
        if recipes_subdir.exists():
            dir_recipes = load_recipes_from_dir(recipes_subdir)
            for name, variants in dir_recipes.items():
                if name not in merged:
                    merged[name] = {}
                merged[name].update(variants)

    else:
        # 2c. Default: look for recipes/ in docker-templates/
        default_dir = Path("ops/build/docker-templates/recipes")
        if base_path:
            default_dir = base_path / default_dir

        if default_dir.exists():
            dir_recipes = load_recipes_from_dir(default_dir)
            for name, variants in dir_recipes.items():
                if name not in merged:
                    merged[name] = {}
                merged[name].update(variants)

    return merged


def load_variant_config(template_dir: Path, variant: str) -> dict[str, Any]:
    """
    Load variant-specific configuration.

    Supports subvariants like "debian-trixie":
    1. First loads base variant config (variants/debian.yml)
    2. Then loads subvariant if defined in the file or as separate file

    Example variants/debian.yml:
        debian_base: bookworm  # default
        subvariants:
            trixie:
                debian_base: trixie
            bookworm:
                debian_base: bookworm
    """
    result = {}

    # Parse variant name (e.g., "debian-trixie" -> base="debian", sub="trixie")
    parts = variant.split("-", 1)
    base_variant = parts[0]
    subvariant = parts[1] if len(parts) > 1 else None

    # Load base variant config
    base_file = template_dir / "variants" / f"{base_variant}.yml"
    if base_file.exists():
        with base_file.open("r", encoding="utf-8") as f:
            base_config = yaml.safe_load(f) or {}

        # Extract subvariants before merging
        subvariants = base_config.pop("subvariants", {})
        result.update(base_config)

        # Apply subvariant overrides if specified
        if subvariant and subvariant in subvariants:
            result.update(subvariants[subvariant])

    # Also check for exact variant file (e.g., debian-trixie.yml)
    exact_file = template_dir / "variants" / f"{variant}.yml"
    if exact_file.exists() and exact_file != base_file:
        with exact_file.open("r", encoding="utf-8") as f:
            exact_config = yaml.safe_load(f) or {}
        exact_config.pop("subvariants", None)
        result.update(exact_config)

    return result


def load_defaults_file(search_paths: list[Path]) -> dict[str, Any]:
    """Load defaults.yml from first found location (optional, for backwards compatibility).

    Note: Defaults should primarily be defined in build-settings.yml under 'dockerfile_defaults'.
    This function is kept for backwards compatibility with projects that still use defaults.yml.
    """
    for path in search_paths:
        defaults_file = path / "defaults.yml"
        if defaults_file.exists():
            with defaults_file.open("r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    return {}


def load_copier_answers(base_path: Path | None = None) -> dict[str, Any]:
    """
    Load project values from .copier-answers.yml.

    These values are used as defaults for Dockerfile generation,
    avoiding duplication between copier config and dockerfile_defaults.

    Searches in multiple locations:
    - ops/build/.copier-answers.yml (copier destination)
    - .copier-answers.yml (project root)
    - Parent directories
    """
    bp = base_path or Path.cwd()

    search_paths = [
        bp / "ops" / "build",     # Copier destination (ops/build/)
        bp,                        # Project root
        bp.parent,                 # Parent (for nested app directories)
        bp.parent.parent,          # Two levels up
    ]

    for path in search_paths:
        answers_file = path / ".copier-answers.yml"
        if answers_file.exists():
            with answers_file.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

            # Return relevant keys (skip internal copier keys starting with _)
            return {k: v for k, v in data.items() if not k.startswith("_") and v is not None}

    return {}


def get_available_variants(template_dir: Path) -> list[str]:
    """Get list of available variants from template directory."""
    variants = []
    variants_dir = template_dir / "variants"

    if variants_dir.exists():
        for yml_file in variants_dir.glob("*.yml"):
            base_name = yml_file.stem

            # Load file to check for subvariants
            with yml_file.open("r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}

            variants.append(base_name)

            # Add subvariants
            subvariants = config.get("subvariants", {})
            for sub in subvariants:
                variants.append(f"{base_name}-{sub}")

    return sorted(variants) if variants else ["debian", "alpine"]


def get_required_defaults_for_variant(variant: str) -> list[str]:
    """Get list of required defaults for a specific variant."""
    base_variant = variant.split("-")[0] if "-" in variant else variant

    common = ["python_version", "node_version"]

    if base_variant == "debian":
        return common + ["debian_base"]
    elif base_variant == "alpine":
        return common + ["alpine_version"]
    else:
        return common


def check_variant_defaults(defaults: dict[str, Any], variant: str) -> tuple[bool, list[str]]:
    """
    Check if all required defaults for a variant are present.

    Returns:
        (is_complete, list of missing keys)
    """
    required = get_required_defaults_for_variant(variant)
    missing = [k for k in required if k not in defaults or defaults.get(k) is None]
    return len(missing) == 0, missing


def generate_dockerfile(
    template_path: Path,
    output_path: Path,
    variant: str = "debian",
    recipes: dict[str, dict[str, str]] | None = None,
    context: dict[str, Any] | None = None,
    defaults: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> str:
    """
    Generate a Dockerfile from a template.

    Args:
        template_path: Path to the Jinja2 template (e.g., docker-templates/base/Dockerfile.j2)
        output_path: Where to write the generated Dockerfile
        variant: Platform variant (debian, alpine, debian-trixie, etc.)
        recipes: Additional recipes to merge with defaults
        context: Additional template context variables
        defaults: Override default values (python_version, debian_base, etc.)
        dry_run: If True, return content without writing

    Returns:
        The generated Dockerfile content
    """
    template_dir = template_path.parent
    template_name = template_path.name

    # Load defaults from file (if exists)
    file_defaults = load_defaults_file([template_dir, template_dir.parent])

    # Load variant-specific config
    variant_config = load_variant_config(template_dir, variant)

    # Merge defaults: file defaults <- variant config <- explicit defaults
    merged_defaults = {**file_defaults, **variant_config, **(defaults or {})}

    # Merge context (context is for template variables, not defaults)
    full_context = {**variant_config, **(context or {})}

    # Generate
    generator = DockerfileGenerator(template_dir, recipes, variant, merged_defaults)
    content = generator.render(template_name, full_context)

    if not dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")

    return content


def generate_all_dockerfiles(
    settings: dict,
    variant: str = "debian",
    targets: list[str] | None = None,
    defaults: dict[str, Any] | None = None,
    dry_run: bool = False,
    base_path: Path | None = None,
) -> dict[str, str]:
    """
    Generate Dockerfiles for all (or selected) targets.

    Looks for `dockerfile_template` in each target's config.
    Falls back to existing dockerfile if no template is defined.

    Defaults are merged in this order (later overrides earlier):
    1. GLOBAL_DEFAULTS (python_version, etc.)
    2. .copier-answers.yml (project-specific: app_name, image_user, etc.)
    3. dockerfile_defaults from build-settings.yml
    4. Explicit defaults passed via --set

    Returns:
        Dict mapping target name to generated content
    """
    results = {}
    all_targets = settings.get("targets", {})
    selected = targets or list(all_targets.keys())

    # Load all recipes (project + user recipes directory)
    recipes = load_all_recipes(settings, base_path)

    # Load copier answers for project-specific defaults
    copier_defaults = load_copier_answers(base_path)

    # Load project-level defaults from build-settings.yml
    project_defaults = settings.get("dockerfile_defaults", {})

    # Merge: GLOBAL_DEFAULTS <- copier_answers <- project_defaults <- explicit defaults
    merged_defaults = {**copier_defaults, **project_defaults, **(defaults or {})}

    for tname in selected:
        if tname not in all_targets:
            continue

        tconfig = all_targets[tname]
        template_path = tconfig.get("dockerfile_template")

        # Convention over Configuration: look for docker-templates/{target}/Dockerfile.j2
        if not template_path:
            convention_path = Path("ops/build/docker-templates") / tname / "Dockerfile.j2"
            if convention_path.exists():
                template_path = str(convention_path)

        if not template_path:
            # No template found - skip this target
            continue

        template = Path(template_path)
        if not template.exists():
            raise FileNotFoundError(f"Template not found: {template_path} (target: {tname})")

        # Convention: docker/Dockerfile.{target}
        output_path = Path(tconfig.get("dockerfile", f"ops/build/docker/Dockerfile.{tname}"))

        # Target-specific context
        context = {
            "target_name": tname,
            "build_args": tconfig.get("build_args", {}),
            **tconfig.get("template_context", {}),
        }

        # Target-specific defaults override project defaults
        target_defaults = {**merged_defaults, **tconfig.get("dockerfile_defaults", {})}

        content = generate_dockerfile(
            template_path=template,
            output_path=output_path,
            variant=variant,
            recipes=recipes,
            context=context,
            defaults=target_defaults,
            dry_run=dry_run,
        )

        results[tname] = content

    return results
