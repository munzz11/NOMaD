#!/usr/bin/env python3
"""Validate a COCO JSON file against the NOMaD canonical spec (coco_spec.json).

The canonical spec defines the authoritative set of categories: their names,
their integer ids, and their supercategories. A conforming COCO file must:

  * contain the top-level ``categories``, ``images`` and ``annotations`` keys
  * carry the full canonical category list (same ids + names as the spec)
  * only reference category names that exist in the spec
  * keep its annotation ``category_id`` values aligned to the spec ids
  * have annotations that point at categories and images that actually exist

When the file does not conform, this script prints a report and then
interactively offers to rewrite the file so that it matches the spec:

  * the ``categories`` array is replaced by the canonical list
  * every annotation ``category_id`` is remapped by *class name* to the spec id
  * annotations whose class name is not in the spec (e.g. ``misc_*``) are
    dropped (you are asked first)

Examples
--------
Validate and be prompted to fix in place::

    python scripts/validate_coco_against_spec.py data/training_archive.json

Just check (CI-friendly, no prompts, exit code 1 if non-conforming)::

    python scripts/validate_coco_against_spec.py data/training_archive.json --check-only

Fix non-interactively, writing to a new file, dropping unknown classes::

    python scripts/validate_coco_against_spec.py data/training_archive.json \
        --yes --unknown drop --output data/training_archive.fixed.json
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SPEC = _REPO_ROOT / "coco_spec.json"

_REQUIRED_TOP_KEYS = ("categories", "images", "annotations")

# Legacy class names that are no longer in the spec but should be remapped to a
# current spec class (by name) instead of being dropped. For example, the old
# generic ``buoy`` super-class was renamed to ``buoy_misc``.
_LEGACY_NAME_ALIASES = {
    "buoy": "buoy_misc",
    "structure": "structure_misc",
}


def resolve_to_spec_id(name: str, name_to_id: dict[str, int]) -> int | None:
    """Spec id for a class name, following legacy aliases. None if unknown."""
    if name in name_to_id:
        return name_to_id[name]
    alias = _LEGACY_NAME_ALIASES.get(name)
    if alias is not None and alias in name_to_id:
        return name_to_id[alias]
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a COCO JSON against the NOMaD canonical spec and, if it "
            "does not match, prompt to rewrite it so it does."
        )
    )
    parser.add_argument(
        "coco_json",
        type=Path,
        help="Path to the COCO JSON file to validate (e.g. data/training_archive.json).",
    )
    parser.add_argument(
        "--spec",
        type=Path,
        default=_DEFAULT_SPEC,
        help=f"Canonical spec JSON with the authoritative categories (default: {_DEFAULT_SPEC}).",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only validate and report. Never prompt or write. Exit 1 if non-conforming.",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Assume 'yes' to all prompts (non-interactive fix).",
    )
    parser.add_argument(
        "--unknown",
        choices=("ask", "drop", "abort"),
        default="ask",
        help=(
            "How to handle annotations whose class name is not in the spec "
            "(default: ask). 'drop' removes them; 'abort' refuses to fix."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Where to write the fixed file (default: overwrite the input in place).",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not write a .bak copy when overwriting the input in place.",
    )
    return parser


def confirm(question: str, *, assume_yes: bool, default: bool = False) -> bool:
    if assume_yes:
        print(f"{question} [auto-yes]")
        return True
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        try:
            resp = input(question + suffix).strip().lower()
        except EOFError:
            return default
        if not resp:
            return default
        if resp in ("y", "yes"):
            return True
        if resp in ("n", "no"):
            return False
        print("Please answer 'y' or 'n'.")


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"File not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}")


def load_spec(path: Path) -> tuple[list[dict], dict[str, int], dict[int, str]]:
    data = load_json(path)
    cats = data.get("categories")
    if not isinstance(cats, list) or not cats:
        raise SystemExit(f"Spec {path} has no non-empty 'categories' array.")
    name_to_id: dict[str, int] = {}
    id_to_name: dict[int, str] = {}
    for c in cats:
        cid = int(c["id"])
        name = str(c["name"])
        if name in name_to_id and name_to_id[name] != cid:
            raise SystemExit(f"Spec {path}: class name {name!r} maps to two ids.")
        name_to_id[name] = cid
        id_to_name[cid] = name
    spec_categories = [copy.deepcopy(c) for c in cats]
    return spec_categories, name_to_id, id_to_name


class Report:
    """Collected validation findings for one COCO file."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        # Drivers of the fix step:
        self.categories_match_spec: bool = True
        self.needs_category_remap: bool = False
        self.unknown_names: Counter[str] = Counter()
        self.aliased_names: Counter[str] = Counter()
        self.missing_spec_names: list[str] = []

    @property
    def conforms(self) -> bool:
        return not self.errors

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)


def validate(coco: dict, name_to_id: dict[str, int], id_to_name: dict[int, str]) -> Report:
    report = Report()

    for key in _REQUIRED_TOP_KEYS:
        if key not in coco:
            report.add_error(f"Missing required top-level key: {key!r}.")

    categories = coco.get("categories") or []
    images = coco.get("images") or []
    annotations = coco.get("annotations") or []

    if not isinstance(categories, list):
        report.add_error("'categories' must be a list.")
        categories = []

    # --- Category structure -------------------------------------------------
    file_id_to_name: dict[int, str] = {}
    seen_ids: Counter[int] = Counter()
    seen_names: Counter[str] = Counter()
    for c in categories:
        if "id" not in c or "name" not in c:
            report.add_error(f"Category missing 'id' or 'name': {c!r}.")
            continue
        cid = int(c["id"])
        name = str(c["name"])
        seen_ids[cid] += 1
        seen_names[name] += 1
        file_id_to_name[cid] = name

    for cid, n in seen_ids.items():
        if n > 1:
            report.add_error(f"Duplicate category id {cid} (appears {n} times).")
    for name, n in seen_names.items():
        if n > 1:
            report.add_error(f"Duplicate category name {name!r} (appears {n} times).")

    # --- Category names vs spec --------------------------------------------
    file_names = {str(c["name"]) for c in categories if "name" in c}
    aliased_cat_names = sorted(
        n for n in file_names if n not in name_to_id and n in _LEGACY_NAME_ALIASES
    )
    for name in aliased_cat_names:
        report.add_error(
            f"Category name {name!r} is a legacy alias for "
            f"{_LEGACY_NAME_ALIASES[name]!r}; it will be remapped (not dropped)."
        )
    unknown = sorted(
        n for n in file_names if resolve_to_spec_id(n, name_to_id) is None
    )
    for name in unknown:
        report.add_error(f"Category name not in spec: {name!r}.")

    # --- Category ids vs spec (must be aligned to canonical ids) -----------
    for c in categories:
        if "id" not in c or "name" not in c:
            continue
        name = str(c["name"])
        if name in name_to_id and int(c["id"]) != name_to_id[name]:
            report.categories_match_spec = False
            report.add_error(
                f"Category {name!r} has id {int(c['id'])} but spec id is "
                f"{name_to_id[name]}."
            )

    # --- Full canonical set present ----------------------------------------
    missing = [name for name in name_to_id if name not in file_names]
    if missing:
        report.categories_match_spec = False
        report.missing_spec_names = sorted(missing, key=lambda n: name_to_id[n])
        report.add_error(
            f"File defines {len(file_names)} categories but the spec has "
            f"{len(name_to_id)}; missing {len(missing)} (e.g. "
            f"{report.missing_spec_names[:5]})."
        )

    extra = sorted(file_names - set(name_to_id))
    if extra or unknown:
        report.categories_match_spec = False

    # --- Annotations: referential integrity + remap need -------------------
    image_ids = {int(im["id"]) for im in images if isinstance(im, dict) and "id" in im}
    bad_cat_refs = 0
    bad_img_refs = 0
    for ann in annotations:
        if not isinstance(ann, dict):
            report.add_error("Found a non-object entry in 'annotations'.")
            continue
        acid = ann.get("category_id")
        if acid is None or int(acid) not in file_id_to_name:
            bad_cat_refs += 1
            continue
        cur_name = file_id_to_name[int(acid)]
        spec_id = resolve_to_spec_id(cur_name, name_to_id)
        if spec_id is None:
            report.unknown_names[cur_name] += 1
        else:
            if cur_name not in name_to_id:
                report.aliased_names[cur_name] += 1
            if int(acid) != spec_id:
                report.needs_category_remap = True
        iid = ann.get("image_id")
        if iid is None or (image_ids and int(iid) not in image_ids):
            bad_img_refs += 1

    if bad_cat_refs:
        report.add_error(
            f"{bad_cat_refs} annotation(s) reference a category_id not defined "
            f"in 'categories'."
        )
    if bad_img_refs:
        report.add_warning(
            f"{bad_img_refs} annotation(s) reference an image_id not present in "
            f"'images'."
        )

    if not report.categories_match_spec:
        report.needs_category_remap = True

    return report


def print_report(path: Path, spec_path: Path, report: Report) -> None:
    print(f"COCO file : {path}")
    print(f"Spec      : {spec_path}")
    print("-" * 60)
    if report.conforms:
        print("PASS: file conforms to the spec.")
        if report.warnings:
            print(f"\nWarnings ({len(report.warnings)}):")
            for w in report.warnings:
                print(f"  ! {w}")
        return

    print(f"FAIL: {len(report.errors)} error(s) found.\n")
    print("Errors:")
    for e in report.errors:
        print(f"  x {e}")
    if report.warnings:
        print(f"\nWarnings ({len(report.warnings)}):")
        for w in report.warnings:
            print(f"  ! {w}")


def apply_fix(
    coco: dict,
    spec_categories: list[dict],
    name_to_id: dict[str, int],
    *,
    drop_unknown: bool,
) -> tuple[dict, dict[str, int]]:
    """Return a new COCO payload aligned to the spec, plus a stats dict."""
    fixed = copy.deepcopy(coco)

    file_id_to_name = {
        int(c["id"]): str(c["name"])
        for c in coco.get("categories", [])
        if "id" in c and "name" in c
    }

    new_annotations: list[dict] = []
    stats = {
        "remapped": 0,
        "remapped_alias": 0,
        "dropped_unknown": 0,
        "dropped_bad_ref": 0,
        "unchanged": 0,
    }
    next_id = 0
    for ann in coco.get("annotations", []):
        if not isinstance(ann, dict):
            continue
        acid = ann.get("category_id")
        cur_name = file_id_to_name.get(int(acid)) if acid is not None else None
        if cur_name is None:
            stats["dropped_bad_ref"] += 1
            continue
        new_cid = resolve_to_spec_id(cur_name, name_to_id)
        if new_cid is None:
            if drop_unknown:
                stats["dropped_unknown"] += 1
                continue
            # keep the annotation but it will still be non-conforming; caller
            # should only choose this path when not dropping.
            new = copy.deepcopy(ann)
            new["id"] = next_id
            next_id += 1
            new_annotations.append(new)
            continue
        new = copy.deepcopy(ann)
        if cur_name not in name_to_id:
            stats["remapped_alias"] += 1
        elif int(acid) != new_cid:
            stats["remapped"] += 1
        else:
            stats["unchanged"] += 1
        new["category_id"] = new_cid
        new["id"] = next_id
        next_id += 1
        new_annotations.append(new)

    fixed["categories"] = [copy.deepcopy(c) for c in spec_categories]
    fixed["annotations"] = new_annotations
    return fixed, stats


def run_fix_flow(
    args: argparse.Namespace,
    coco: dict,
    report: Report,
    spec_categories: list[dict],
    name_to_id: dict[str, int],
) -> int:
    print("\nThe file does not match the spec.")
    print("Proposed fix:")
    print(f"  - Replace 'categories' with the canonical {len(spec_categories)} spec categories.")
    if report.needs_category_remap:
        print("  - Remap every annotation 'category_id' to the spec id by class name.")
    if report.aliased_names:
        total = sum(report.aliased_names.values())
        pairs = ", ".join(
            f"{n} -> {_LEGACY_NAME_ALIASES[n]}" for n in sorted(report.aliased_names)
        )
        print(
            f"  - {total} annotation(s) use legacy class names and will be "
            f"remapped, not dropped ({pairs})."
        )
    if report.unknown_names:
        total = sum(report.unknown_names.values())
        names = ", ".join(sorted(report.unknown_names))
        print(
            f"  - {total} annotation(s) use class names not in the spec "
            f"({names})."
        )

    drop_unknown = False
    if report.unknown_names:
        if args.unknown == "abort":
            print(
                "\nRefusing to fix: unknown class names present and --unknown abort "
                "was set. Add these classes to the spec or rerun with "
                "--unknown drop."
            )
            return 1
        if args.unknown == "drop":
            drop_unknown = True
        else:  # ask
            drop_unknown = confirm(
                "Drop annotations whose class name is not in the spec?",
                assume_yes=args.yes,
                default=False,
            )
            if not drop_unknown:
                print(
                    "Cannot make the file conform while keeping unknown class "
                    "names. Add them to the spec, or rerun and choose to drop "
                    "them. Aborting without writing."
                )
                return 1

    if not confirm("\nApply these changes?", assume_yes=args.yes, default=False):
        print("No changes written.")
        return 1

    fixed, stats = apply_fix(
        coco, spec_categories, name_to_id, drop_unknown=drop_unknown
    )

    out_path = (args.output or args.coco_json).expanduser().resolve()
    if out_path == args.coco_json.expanduser().resolve() and not args.no_backup:
        backup = out_path.with_suffix(out_path.suffix + ".bak")
        backup.write_text(json.dumps(coco, indent=2) + "\n", encoding="utf-8")
        print(f"Backup written: {backup}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(fixed, indent=2) + "\n", encoding="utf-8")

    print("\nFix applied:")
    print(f"  categories       : {len(fixed['categories'])} (canonical)")
    print(f"  annotations kept : {len(fixed['annotations'])}")
    print(f"  remapped ids     : {stats['remapped']}")
    if stats["remapped_alias"]:
        print(f"  remapped (legacy alias): {stats['remapped_alias']}")
    print(f"  already aligned  : {stats['unchanged']}")
    if stats["dropped_unknown"]:
        print(f"  dropped (unknown class): {stats['dropped_unknown']}")
    if stats["dropped_bad_ref"]:
        print(f"  dropped (bad category ref): {stats['dropped_bad_ref']}")
    print(f"  written to       : {out_path}")

    # Re-validate the result so the user knows it now conforms.
    spec_id_to_name = {v: k for k, v in name_to_id.items()}
    recheck = validate(fixed, name_to_id, spec_id_to_name)
    if recheck.conforms:
        print("\nRe-validation: PASS, the file now conforms to the spec.")
        return 0
    print("\nRe-validation: still non-conforming:")
    for e in recheck.errors:
        print(f"  x {e}")
    return 1


def main() -> int:
    args = build_parser().parse_args()
    coco_path = args.coco_json.expanduser().resolve()
    spec_path = args.spec.expanduser().resolve()

    spec_categories, name_to_id, spec_id_to_name = load_spec(spec_path)
    coco = load_json(coco_path)

    report = validate(coco, name_to_id, spec_id_to_name)
    print_report(coco_path, spec_path, report)

    if report.conforms:
        return 0

    if args.check_only:
        return 1

    return run_fix_flow(args, coco, report, spec_categories, name_to_id)


if __name__ == "__main__":
    sys.exit(main())
