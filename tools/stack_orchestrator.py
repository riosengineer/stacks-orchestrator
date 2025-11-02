#!/usr/bin/env python3
"""Simple deployment-stack orchestrator.

Scans the repository for manifest files, resolves dependencies, and deploys
subscription-level deployment stacks using the Azure CLI.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import yaml


def _sequence_key(item: Any) -> Optional[str]:
  if not isinstance(item, dict):
    return None
  name = item.get("name")
  if isinstance(name, str) and name:
    return name
  stack_name = item.get("stackName")
  if isinstance(stack_name, str) and stack_name:
    return stack_name
  return None


def _merge_sequences(base: List[Any], override: List[Any]) -> List[Any]:
  if not base:
    return copy.deepcopy(override)
  if not override:
    return copy.deepcopy(base)

  if all(isinstance(item, dict) for item in base + override):
    keys: List[str] = []
    base_map: Dict[str, Any] = {}
    for item in base:
      key = _sequence_key(item)
      if key is None or key in base_map:
        # Fallback to overriding the full list when keys are not usable.
        return copy.deepcopy(override)
      keys.append(key)
      base_map[key] = copy.deepcopy(item)

    append_order: List[str] = []
    for item in override:
      key = _sequence_key(item)
      if key is None:
        return copy.deepcopy(override)
      if key in base_map:
        base_map[key] = _deep_merge(base_map[key], item)
      else:
        base_map[key] = copy.deepcopy(item)
        append_order.append(key)

    merged_keys = keys + append_order
    return [base_map[key] for key in merged_keys]

  return copy.deepcopy(override)


def _deep_merge(base: Any, override: Any) -> Any:
  if isinstance(base, dict) and isinstance(override, dict):
    result = copy.deepcopy(base)
    for key, value in override.items():
      if key in result:
        result[key] = _deep_merge(result[key], value)
      else:
        result[key] = copy.deepcopy(value)
    return result
  if isinstance(base, list) and isinstance(override, list):
    return _merge_sequences(base, override)
  return copy.deepcopy(override)


@dataclass
class Dependency:
  name: str
  stack_name: str
  outputs: Dict[str, str]


@dataclass
class StackManifest:
  name: str
  manifest_path: Path
  template_file: Path
  parameter_file: Optional[Path]
  subscription_deployment: bool = True
  resource_group: Optional[str] = None
  location: Optional[str] = None
  description: Optional[str] = None
  dependencies: List[Dependency] = field(default_factory=list)
  exports: Dict[str, str] = field(default_factory=dict)
  extra_az_args: List[str] = field(default_factory=list)
  parameter_bindings: Dict[str, str] = field(default_factory=dict)

  @property
  def working_directory(self) -> Path:
    return self.manifest_path.parent


@dataclass
class ExecutionGraph:
  order_index: Dict[str, int]
  dependents: Dict[str, Set[str]]
  indegree: Dict[str, int]

  def initial_ready(self) -> List[str]:
    return sorted(
      [name for name, value in self.indegree.items() if value == 0],
      key=lambda candidate: self.order_index[candidate],
    )


PALETTE_KEYS = ("heading", "root", "dependent", "arrow", "reset")


class ColorMode(str, Enum):
  AUTO = "auto"
  ALWAYS = "always"
  NEVER = "never"


def _supports_color_output() -> bool:
  stream = getattr(sys.stdout, "isatty", None)
  return bool(stream and stream()) and os.environ.get("NO_COLOR") is None


def build_console_palette(requested_mode: str) -> Dict[str, str]:
  try:
    mode = ColorMode(requested_mode or ColorMode.AUTO.value)
  except ValueError:
    mode = ColorMode.AUTO

  use_color = mode is ColorMode.ALWAYS or (mode is ColorMode.AUTO and _supports_color_output())
  palette = {key: "" for key in PALETTE_KEYS}
  if use_color:
    palette.update({
      "heading": "\033[1m",
      "root": "\033[32m",
      "dependent": "\033[36m",
      "arrow": "\033[90m",
      "reset": "\033[0m",
    })
  return palette


class ManifestRepository:
  def __init__(self, root: Path, glob_pattern: str) -> None:
    self._root = root
    self._glob = glob_pattern

  def load(self) -> Dict[str, StackManifest]:
    manifests: Dict[str, StackManifest] = {}
    for manifest_path in sorted(self._root.glob(self._glob)):
      if not manifest_path.is_file():
        continue
      manifest = self._parse_manifest(manifest_path)
      existing = manifests.get(manifest.name)
      if existing:
        replacement = self._resolve_duplicate(existing, manifest)
        if replacement is None:
          raise ValueError(
            f"Duplicate stack name '{manifest.name}' found in {manifest_path} "
            f"and {existing.manifest_path}"
          )
        manifests[manifest.name] = replacement
        continue
      manifests[manifest.name] = manifest
    if not manifests:
      raise ValueError(f"No manifest files found under '{self._root}' using pattern '{self._glob}'.")
    return manifests

  def _parse_manifest(self, manifest_path: Path) -> StackManifest:
    data = self._load_manifest_data(manifest_path)

    if not isinstance(data, dict) or "stack" not in data:
      raise ValueError(f"Manifest {manifest_path} must contain a 'stack' object.")

    stack_section = data["stack"]
    if not isinstance(stack_section, dict):
  
      raise ValueError(f"Manifest {manifest_path}: 'stack' must be a mapping.")

    name = stack_section.get("name")
    if not name:
      raise ValueError(f"Manifest {manifest_path}: stack.name is required.")

    template_section = stack_section.get("template") or {}
    template_file = template_section.get("file")
    parameter_file = template_section.get("parameters")

    if not template_file:
      raise ValueError(f"Manifest {manifest_path}: stack.template.file is required.")

    template_path = (manifest_path.parent / template_file).resolve()
    parameter_path = None
    if parameter_file:
      parameter_path = (manifest_path.parent / parameter_file).resolve()
      if not parameter_path.exists():
        raise FileNotFoundError(
          f"Parameter file '{parameter_file}' referenced by {manifest_path} does not exist."
        )

    deployment_section = stack_section.get("deployment", {})
    subscription_deployment = bool(deployment_section.get("subscription", True))
    resource_group = deployment_section.get("resourceGroup")
    location = deployment_section.get("location")

    dependencies_data = data.get("dependencies", [])
    dependencies = [self._parse_dependency(row, manifest_path) for row in dependencies_data]

    exports = data.get("exports", {})
    if not isinstance(exports, dict):
      raise ValueError(f"Manifest {manifest_path}: 'exports' must be a mapping if provided.")

    description = stack_section.get("description")

    extra_az_args_raw = stack_section.get("extraAzArgs", [])
    if not isinstance(extra_az_args_raw, list) or any(not isinstance(item, str) for item in extra_az_args_raw):
      raise ValueError(f"Manifest {manifest_path}: stack.extraAzArgs must be an array of strings when specified.")

    parameter_bindings = data.get("parameterBindings", {})
    if not isinstance(parameter_bindings, dict):
      raise ValueError(f"Manifest {manifest_path}: parameterBindings must be a mapping when specified.")

    return StackManifest(
      name=name,
      manifest_path=manifest_path,
      template_file=template_path,
      parameter_file=parameter_path,
      subscription_deployment=subscription_deployment,
      resource_group=resource_group,
      location=location,
      description=description,
      dependencies=dependencies,
      exports=exports,
      extra_az_args=list(extra_az_args_raw),
      parameter_bindings=parameter_bindings,
    )

  def _resolve_duplicate(self, existing: StackManifest, candidate: StackManifest) -> Optional[StackManifest]:
    existing_kind = self._classify_manifest(existing.manifest_path)
    candidate_kind = self._classify_manifest(candidate.manifest_path)
    if existing_kind == candidate_kind:
      return None
    if candidate_kind == "overlay":
      return candidate
    if existing_kind == "overlay":
      return existing
    return None

  def _classify_manifest(self, manifest_path: Path) -> str:
    try:
      relative_parts = manifest_path.resolve().relative_to(self._root).parts
    except ValueError:
      relative_parts = manifest_path.resolve().parts
    return "overlay" if "environments" in relative_parts else "base"

  def _parse_dependency(self, row: dict, manifest_path: Path) -> Dependency:
    if not isinstance(row, dict):
      raise ValueError(f"Manifest {manifest_path}: dependency entries must be mappings.")
    stack_name = row.get("stackName")
    if not stack_name:
      raise ValueError(f"Manifest {manifest_path}: dependency.stackName is required.")
    name = row.get("name", stack_name)
    outputs = row.get("outputs", {})
    if not isinstance(outputs, dict):
      raise ValueError(f"Manifest {manifest_path}: dependency.outputs must be a mapping.")
    return Dependency(name=name, stack_name=stack_name, outputs=outputs)

  def _load_manifest_data(self, manifest_path: Path, seen: Optional[Set[Path]] = None) -> Dict[str, Any]:
    if seen is None:
      seen = set()

    resolved_manifest_path = manifest_path.resolve()
    if resolved_manifest_path in seen:
      raise ValueError(f"Cyclic 'extends' reference detected at {manifest_path}.")
    seen.add(resolved_manifest_path)

    with manifest_path.open("r", encoding="utf-8") as handle:
      loaded = yaml.safe_load(handle) or {}

    if not isinstance(loaded, dict):
      raise ValueError(f"Manifest {manifest_path} must parse to a mapping.")

    extends_value = loaded.pop("extends", None)
    merged: Dict[str, Any] = {}

    if extends_value:
      if isinstance(extends_value, str):
        extends_list = [extends_value]
      elif isinstance(extends_value, list) and all(isinstance(item, str) for item in extends_value):
        extends_list = extends_value
      else:
        raise ValueError(
          f"Manifest {manifest_path}: 'extends' must be a string or list of strings when specified."
        )

      for entry in extends_list:
        base_path = (manifest_path.parent / entry).resolve()
        if not base_path.exists():
          raise FileNotFoundError(f"Manifest {manifest_path}: extended file '{entry}' was not found.")
        base_data = self._load_manifest_data(base_path, seen)
        merged = _deep_merge(merged, base_data)

    merged = _deep_merge(merged, loaded)
    self._ensure_absolute_template_paths(merged, manifest_path)
    seen.remove(resolved_manifest_path)
    return merged

  def _ensure_absolute_template_paths(self, data: Dict[str, Any], manifest_path: Path) -> None:
    stack_section = data.get("stack")
    if not isinstance(stack_section, dict):
      return
    template_section = stack_section.get("template")
    if not isinstance(template_section, dict):
      return
    parent_dir = manifest_path.parent
    for key in ("file", "parameters"):
      value = template_section.get(key)
      if isinstance(value, str) and value:
        candidate = Path(value)
        if not candidate.is_absolute():
          absolute = (parent_dir / candidate).resolve()
          template_section[key] = str(absolute)
        else:
          template_section[key] = str(candidate)


def resolve_execution_order(
  manifests: Dict[str, StackManifest], target_stacks: Optional[Set[str]] = None
) -> Tuple[List[StackManifest], Dict[str, Set[str]]]:
  """Return manifests sorted in deployment order (dependencies first) along with missing dependencies."""
  if target_stacks is None:
    target_stacks = set(manifests.keys())

  needed: Set[str] = set()
  missing_dependencies: Dict[str, Set[str]] = defaultdict(set)

  def collect(stack_name: str, requester: Optional[str]) -> None:
    if stack_name in needed:
      return
    manifest = manifests.get(stack_name)
    if manifest is None:
      if requester is not None:
        missing_dependencies[requester].add(stack_name)
      else:
        missing_dependencies.setdefault(stack_name, set())
      return
    needed.add(stack_name)
    for dep in manifest.dependencies:
      collect(dep.stack_name, manifest.name)

  for stack in target_stacks:
    collect(stack, stack)

  visited: Set[str] = set()
  visiting: Set[str] = set()
  order: List[StackManifest] = []

  def visit(stack_name: str) -> None:
    if stack_name in visited:
      return
    if stack_name in visiting:
      raise ValueError(f"Cyclic dependency detected involving stack '{stack_name}'.")
    visiting.add(stack_name)
    manifest = manifests[stack_name]
    for dep in manifest.dependencies:
      if dep.stack_name in needed:
        visit(dep.stack_name)
    visiting.remove(stack_name)
    visited.add(stack_name)
    order.append(manifest)

  for stack_name in needed:
    visit(stack_name)

  return order, missing_dependencies


def build_execution_graph(ordered_manifests: List[StackManifest]) -> ExecutionGraph:
  order_index = {manifest.name: idx for idx, manifest in enumerate(ordered_manifests)}
  dependents: Dict[str, Set[str]] = defaultdict(set)
  indegree: Dict[str, int] = {}

  for manifest in ordered_manifests:
    dependency_names = {
      dependency.stack_name
      for dependency in manifest.dependencies
      if dependency.stack_name in order_index
    }
    indegree[manifest.name] = len(dependency_names)
    for dependency_name in dependency_names:
      dependents[dependency_name].add(manifest.name)
    dependents.setdefault(manifest.name, set())

  return ExecutionGraph(
    order_index=order_index,
    dependents={name: set(children) for name, children in dependents.items()},
    indegree=dict(indegree),
  )


def build_az_command(
  manifest: StackManifest,
  *,
  location: str,
  action_on_unmanage: str,
  deny_settings_mode: str,
  output_format: str,
  az_cli: str,
  extra_args: Iterable[str],
  parameter_overrides: Dict[str, Any],
  auto_approve: bool = False,
) -> List[str]:
  extra_args = list(extra_args)
  if not manifest.subscription_deployment:
    raise NotImplementedError(
      f"Stack '{manifest.name}' declares a non-subscription deployment, which is not yet supported."
    )

  template_path = str(manifest.template_file)
  command = [
    az_cli,
    "stack",
    "sub",
    "create",
    "--name",
    manifest.name,
    "--location",
    manifest.location or location,
    "--template-file",
    template_path,
  ]

  if manifest.parameter_file:
    command.extend(["--parameters", str(manifest.parameter_file)])

  for param_name, value in parameter_overrides.items():
    serialized = json.dumps(value)
    command.extend(["--parameters", f"{param_name}={serialized}"])

  command.extend([
    "--action-on-unmanage",
    action_on_unmanage,
    "--deny-settings-mode",
    deny_settings_mode,
  ])

  if auto_approve and "--yes" not in extra_args and "--yes" not in command:
    command.append("--yes")

  has_output_flag = any(
    arg == "--output"
    or arg.startswith("--output=")
    or arg == "-o"
    or (arg.startswith("-o") and len(arg) > 2)
    for arg in extra_args
  )
  if output_format and not has_output_flag:
    command.extend(["--output", output_format])

  command.extend(extra_args)
  return command


def fetch_stack_outputs(az_cli: str, stack_name: str) -> Dict[str, Any]:
  show_command = [az_cli, "stack", "sub", "show", "--name", stack_name, "--output", "json"]
  completed = subprocess.run(show_command, check=False, capture_output=True, text=True)
  if completed.returncode != 0:
    return {}
  try:
    payload = json.loads(completed.stdout)
  except json.JSONDecodeError:
    return {}
  outputs = payload.get("outputs", {}) or {}
  resolved: Dict[str, Any] = {}
  for key, value in outputs.items():
    if isinstance(value, dict) and "value" in value:
      resolved[key] = value["value"]
    else:
      resolved[key] = value
  return resolved


def format_command(command: Iterable[str]) -> str:
  return " ".join(json.dumps(arg) for arg in command)


def run_command(
  command: List[str],
  *,
  dry_run: bool,
  cwd: Path,
  verbose: bool,
  echo_commands: bool,
) -> int:
  if dry_run:
    if verbose or echo_commands:
      print(format_command(command))
    return 0

  if verbose or echo_commands:
    print(format_command(command))

  try:
    completed = subprocess.run(command, cwd=cwd, check=False)
  except FileNotFoundError as exc:
    missing = command[0]
    print(
      f"Command '{missing}' could not be executed ({exc.strerror or 'file not found'}). "
      "Ensure it is installed and available on PATH.",
      file=sys.stderr,
    )
    return 127
  return completed.returncode


def print_dependency_summary(
  all_manifests: Dict[str, StackManifest],
  ordered: List[StackManifest],
  palette: Optional[Dict[str, str]] = None,
  missing_dependencies: Optional[Dict[str, Set[str]]] = None,
) -> None:
  if not ordered:
    print("No stacks selected for deployment.")
    return

  if palette is None:
    palette = {key: "" for key in PALETTE_KEYS}
  if missing_dependencies is None:
    missing_dependencies = {}

  execution_names = [manifest.name for manifest in ordered]
  execution_index = {name: idx for idx, name in enumerate(execution_names)}

  heading = palette.get("heading", "")
  reset = palette.get("reset", "")
  print(f"{heading}Dependency map (selected scope):{reset}")
  dependency_map: Dict[str, Dict[str, List[str]]] = {}
  for manifest in ordered:
    within_scope = [
      dependency.stack_name
      for dependency in manifest.dependencies
      if dependency.stack_name in execution_index
    ]
    external = sorted(missing_dependencies.get(manifest.name, set()))
    dependency_map[manifest.name] = {
      "internal": within_scope,
      "external": external,
    }

  roots = [
    name
    for name, mapping in dependency_map.items()
    if not mapping["internal"] and not mapping["external"]
  ]
  dependents = [
    name
    for name, mapping in dependency_map.items()
    if mapping["internal"] or mapping["external"]
  ]

  print(f"  {palette.get('heading', '')}Root stacks:{reset}")
  if roots:
    for name in sorted(roots, key=lambda candidate: execution_index[candidate]):
      print(f"    - {palette.get('root', '')}{name}{reset}")
  else:
    print("    (none)")

  print(f"  {palette.get('heading', '')}Dependent stacks:{reset}")
  if dependents:
    for name in sorted(dependents, key=lambda candidate: execution_index[candidate]):
      print(f"    {palette.get('dependent', '')}{name}{reset}")
      for dependency_name in dependency_map[name]["internal"]:
        print(
          f"      {palette.get('arrow', '')}-> {reset}{palette.get('root', '')}{dependency_name}{reset}"
        )
      for dependency_name in dependency_map[name]["external"]:
        print(
          f"      {palette.get('arrow', '')}-> {reset}{palette.get('root', '')}{dependency_name}{reset}"
          + f" {palette.get('arrow', '')}(external){reset}"
        )
  else:
    print("    (none)")

  print()

  print(f"{heading}Execution order:{reset}")
  for position, name in enumerate(execution_names, 1):
    manifest = all_manifests.get(name)
    origin = ""
    if manifest is not None:
      try:
        origin = str(manifest.manifest_path.relative_to(Path.cwd()))
      except ValueError:
        origin = str(manifest.manifest_path)
    print(f"  {position}. {palette.get('dependent', '')}{name}{reset} ({origin})")
  print()


def deploy_stack(
  manifest: StackManifest,
  *,
  args: argparse.Namespace,
  az_cli_path: str,
  exports_cache: Dict[str, Dict[str, Any]],
  exports_lock: threading.Lock,
) -> Tuple[bool, int]:
  dependency_map = {dep.name: dep for dep in manifest.dependencies}
  parameter_overrides: Dict[str, Any] = {}

  for param_name, binding in manifest.parameter_bindings.items():
    if not isinstance(binding, str) or "." not in binding:
      print(
        f"Parameter binding '{binding}' for '{param_name}' in stack '{manifest.name}' is invalid.",
        file=sys.stderr,
      )
      continue

    dep_alias, output_name = binding.split(".", 1)
    dependency = dependency_map.get(dep_alias)
    if dependency is None:
      print(
        f"Stack '{manifest.name}' references unknown dependency alias '{dep_alias}' in parameter binding '{param_name}'.",
        file=sys.stderr,
      )
      continue

    with exports_lock:
      dep_outputs = exports_cache.get(dependency.stack_name)

    if dep_outputs is None:
      dep_outputs = fetch_stack_outputs(az_cli_path, dependency.stack_name)
      if dep_outputs:
        with exports_lock:
          exports_cache[dependency.stack_name] = dep_outputs

    if not dep_outputs or output_name not in dep_outputs:
      print(
        f"Output '{output_name}' from dependency '{dependency.stack_name}' is unavailable; cannot bind parameter '{param_name}' for stack '{manifest.name}'.",
        file=sys.stderr,
      )
      continue

    parameter_overrides[param_name] = dep_outputs[output_name]

  combined_extra_args = list(manifest.extra_az_args) + list(args.extra_az_args)

  try:
    command = build_az_command(
      manifest,
      location=args.location,
      action_on_unmanage=args.action_on_unmanage,
      deny_settings_mode=args.deny_settings_mode,
      output_format=args.output,
      az_cli=az_cli_path,
      extra_args=combined_extra_args,
      parameter_overrides=parameter_overrides,
      auto_approve=args.yes,
    )
  except Exception as exc:  # pylint: disable=broad-except
    print(f"Failed to build command for stack '{manifest.name}': {exc}", file=sys.stderr)
    return False, 1

  if args.dry_run:
    if args.verbose:
      print(f"[dry-run] {manifest.name} from {manifest.manifest_path}")
      if not args.echo:
        print(f"  command: {format_command(command)}")
  else:
    print(f"Deploying stack '{manifest.name}' from {manifest.manifest_path}...")
  return_code = run_command(
    command,
    dry_run=args.dry_run,
    cwd=manifest.working_directory,
    verbose=args.verbose,
    echo_commands=args.echo,
  )
  if return_code != 0:
    print(
      f"Stack '{manifest.name}' deployment failed with exit code {return_code}.",
      file=sys.stderr,
    )
    return False, return_code

  if not args.dry_run:
    with exports_lock:
      cached_outputs = exports_cache.get(manifest.name)
    if cached_outputs is None:
      outputs = fetch_stack_outputs(az_cli_path, manifest.name)
      if outputs:
        with exports_lock:
          exports_cache[manifest.name] = outputs

  return True, return_code


def execute_stack_level(
  level: List[str],
  manifests: Dict[str, StackManifest],
  *,
  args: argparse.Namespace,
  az_cli_path: str,
  exports_cache: Dict[str, Dict[str, Any]],
  exports_lock: threading.Lock,
  executor: Optional[ThreadPoolExecutor],
) -> Tuple[Dict[str, bool], List[str], bool]:
  if not level:
    return {}, [], False

  results: Dict[str, bool] = {}
  executed: List[str] = []
  stop_due_to_error = False

  if executor is not None:
    future_map = {
      executor.submit(
        deploy_stack,
        manifests[name],
        args=args,
        az_cli_path=az_cli_path,
        exports_cache=exports_cache,
        exports_lock=exports_lock,
      ): name
      for name in level
    }

    for future in as_completed(future_map):
      name = future_map[future]
      try:
        success, _ = future.result()
      except Exception as exc:  # pylint: disable=broad-except
        print(f"Stack '{name}' raised an unexpected error: {exc}", file=sys.stderr)
        success = False
      results[name] = success
      if not success and args.stop_on_error:
        stop_due_to_error = True
    executed = list(level)
    return results, executed, stop_due_to_error

  for name in level:
    success, _ = deploy_stack(
      manifests[name],
      args=args,
      az_cli_path=az_cli_path,
      exports_cache=exports_cache,
      exports_lock=exports_lock,
    )
    executed.append(name)
    results[name] = success
    if not success and args.stop_on_error:
      stop_due_to_error = True
      break

  return results, executed, stop_due_to_error


def orchestrate(args: argparse.Namespace) -> int:
  repository = ManifestRepository(Path(args.root).resolve(), args.glob)
  manifests = repository.load()

  target_set = set(args.stacks) if args.stacks else None
  ordered_manifests, missing_dependencies = resolve_execution_order(manifests, target_set)
  palette = build_console_palette(getattr(args, "color", ColorMode.AUTO.value))

  dependency_mode = getattr(args, "dependency_mode", "include")
  if dependency_mode == "skip":
    if not args.stacks:
      print(
        "--skip-dependencies requires --stacks to target specific stacks; refusing to skip dependencies for a full run.",
        file=sys.stderr,
      )
      return 1
    target_names = set(args.stacks)
    execution_manifests = [manifest for manifest in ordered_manifests if manifest.name in target_names]
    if not execution_manifests:
      print(
        "No matching stacks were found for the requested --stacks values; nothing to deploy.",
        file=sys.stderr,
      )
      return 1
    missing = target_names - {manifest.name for manifest in execution_manifests}
    if missing:
      print(
        f"Requested stacks were not found in the manifest set: {', '.join(sorted(missing))}",
        file=sys.stderr,
      )
      return 1
    skipped_dependencies = sorted(
      {
        dependency.stack_name
        for manifest in execution_manifests
        for dependency in manifest.dependencies
        if dependency.stack_name not in target_names
      }
    )
    if skipped_dependencies:
      print(
        "Skipping dependency deployments for: "
        + ", ".join(skipped_dependencies)
        + ". Existing outputs will be reused.",
      )
    ordered_manifests = execution_manifests
    missing_dependencies = {
      stack: deps
      for stack, deps in missing_dependencies.items()
      if stack in {manifest.name for manifest in ordered_manifests}
    }

  print_dependency_summary(manifests, ordered_manifests, palette, missing_dependencies)

  az_cli_path = shutil.which(args.az_cli)
  if az_cli_path is None:
    print(
      f"Azure CLI executable '{args.az_cli}' was not found on PATH. "
      "Install Azure CLI or supply --az-cli with the full path to the executable.",
      file=sys.stderr,
    )
    return 1

  graph = build_execution_graph(ordered_manifests)
  ready = graph.initial_ready()
  remaining = set(graph.indegree.keys())

  exports_cache: Dict[str, Dict[str, Any]] = {}
  exports_lock = threading.Lock()
  failures: Set[str] = set()
  max_parallel = max(1, getattr(args, "parallelism", 1))
  if getattr(args, "stop_on_error", False):
    max_parallel = 1
  executor: Optional[ThreadPoolExecutor] = None
  stop_due_to_error = False

  try:
    if max_parallel > 1:
      executor = ThreadPoolExecutor(max_workers=max_parallel)

    while ready:
      level = sorted(ready, key=lambda candidate: graph.order_index[candidate])
      ready = []

      results, executed_level, level_stop = execute_stack_level(
        level,
        manifests,
        args=args,
        az_cli_path=az_cli_path,
        exports_cache=exports_cache,
        exports_lock=exports_lock,
        executor=executor,
      )

      for name in executed_level:
        remaining.discard(name)
        success = results.get(name, False)
        if success:
          for child in graph.dependents.get(name, set()):
            graph.indegree[child] -= 1
            if graph.indegree[child] == 0 and child in remaining:
              ready.append(child)
        else:
          failures.add(name)

      if level_stop:
        stop_due_to_error = True
      if stop_due_to_error:
        break

      ready = sorted(set(ready), key=lambda candidate: graph.order_index[candidate])

    if remaining:
      blocked = sorted(remaining, key=lambda n: graph.order_index.get(n, float("inf")))
      print(
        f"Skipped stacks due to unmet dependencies or earlier failures: {', '.join(blocked)}",
        file=sys.stderr,
      )
      failures.update(blocked)

    if failures:
      ordered_failures = sorted(failures, key=lambda n: graph.order_index.get(n, float("inf")))
      print(f"Completed with failures in: {', '.join(ordered_failures)}", file=sys.stderr)
      return 1

    print("All stacks processed successfully.")
    return 0
  finally:
    if executor is not None:
      executor.shutdown(wait=True)


def parse_arguments(argv: Optional[List[str]] = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="Deployment stack orchestrator")
  parser.add_argument(
    "--root",
    default=".",
    help="Root directory to search for manifest files (default: current directory).",
  )
  parser.add_argument(
    "--glob",
    default="**/*.manifest.yaml",
    help="Glob pattern for manifest discovery relative to the root directory.",
  )
  parser.add_argument(
    "--stacks",
    nargs="*",
    help="Optional list of stack names to deploy (dependencies are included automatically).",
  )
  parser.add_argument(
    "--location",
    default="uksouth",
    help="Azure location used when creating the deployment stack record (default: uksouth).",
  )
  parser.add_argument(
    "--action-on-unmanage",
    default="deleteAll",
    help="Value for --action-on-unmanage (default: deleteAll).",
  )
  parser.add_argument(
    "--deny-settings-mode",
    default="none",
    help="Value for --deny-settings-mode (default: none).",
  )
  parser.add_argument(
    "--az-cli",
    default="az",
    help="Azure CLI executable name (default: az).",
  )
  parser.add_argument(
    "--extra-az-args",
    nargs="*",
    default=[],
    help=(
      "Additional arguments appended to the az command. "
      "Arguments that are not recognized by the orchestrator are also forwarded."
    ),
  )
  parser.add_argument(
    "--parallelism",
    type=int,
    default=1,
    help="Maximum number of stacks to deploy in parallel (default: 1).",
  )
  parser.add_argument(
    "--yes",
    action="store_true",
    help="Automatically confirm Azure CLI prompts for each deployment stack.",
  )
  parser.add_argument(
    "--output",
    "-o",
    choices=["json", "jsonc", "none", "table", "tsv", "yaml", "yamlc"],
    default=None,
    help="Azure CLI output format applied to each stack invocation (default: CLI standard json).",
  )
  parser.add_argument(
    "--color",
    choices=[mode.value for mode in ColorMode],
    default=ColorMode.AUTO.value,
    help="Color output mode: auto (default), always, or never.",
  )
  parser.add_argument(
    "--dry-run",
    action="store_true",
    help="Print commands without executing them.",
  )
  parser.add_argument(
    "--verbose",
    action="store_true",
    help="Print every constructed command, even when not in dry-run mode.",
  )
  parser.add_argument(
    "--echo",
    action="store_true",
    help="Echo each Azure CLI command before execution (quiet by default).",
  )
  parser.add_argument(
    "--stop-on-error",
    action="store_true",
    help="Stop executing further stacks after the first failure.",
  )
  dependency_group = parser.add_mutually_exclusive_group()
  dependency_group.add_argument(
    "--include-dependencies",
    action="store_true",
    help=(
      "Deploy dependency stacks alongside the selected stacks. "
      "This is the default behaviour unless overridden via environment variable."
    ),
  )
  dependency_group.add_argument(
    "--skip-dependencies",
    action="store_true",
    help=(
      "Skip deploying dependency stacks and reuse their existing outputs. "
      "Supports fast, targeted updates but should be used with caution."
    ),
  )

  env_dependency_mode = os.environ.get("STACK_ORCHESTRATOR_DEPENDENCIES", "include").lower()
  base_dependency_mode = env_dependency_mode if env_dependency_mode in {"include", "skip"} else "include"

  args, remaining = parser.parse_known_args(argv)
  if remaining:
    args.extra_az_args.extend(remaining)
  dependency_mode = base_dependency_mode
  if getattr(args, "include_dependencies", False):
    dependency_mode = "include"
  elif getattr(args, "skip_dependencies", False):
    dependency_mode = "skip"
  args.dependency_mode = dependency_mode
  return args


def main(argv: Optional[List[str]] = None) -> int:
  args = parse_arguments(argv)
  try:
    return orchestrate(args)
  except Exception as exc:  # pylint: disable=broad-except
    print(f"Unhandled error: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
  sys.exit(main())
