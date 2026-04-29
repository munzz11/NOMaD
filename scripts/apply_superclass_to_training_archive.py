#!/usr/bin/env python3
"""Remap a NOMaD training archive to superclass category ids from a super-classes/*.json file.

Input archives may use a local 0..N-1 renumbering for category_id. The script maps each
annotation to canonical NOMaD spec class ids (coco_spec.json) by looking up the class name
from the input ``categories`` list, then applies superclass remapping to subclass_ids (0-68).
"""

from __future__ import annotations

import argparse
import copy
import json
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_COCO_SPEC = _REPO_ROOT / "coco_spec.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Load a superclass grouping JSON and write a new training archive "
            "with annotations remapped to superclass category ids. "
            "Images are passed through unchanged. Categories are replaced by "
            "one entry per group (new ids 0..G-1)."
        )
    )
    parser.add_argument(
        "superclass_json",
        type=Path,
        help="Path to a superclass file (e.g. super-classes/harbor_nav_optimised.json).",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/training_archive.json"),
        help="Input training archive JSON (default: data/training_archive.json).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path. Default: <input's parent> / training_archive__<superclass_stem>.json",
    )
    parser.add_argument(
        "--unmapped",
        choices=("error", "drop"),
        default="error",
        help="What to do with annotations whose category_id is in no group (default: error).",
    )
    parser.add_argument(
        "--add-metadata",
        action="store_true",
        help="Add a top-level nomad_superclass_remap key describing the remapping.",
    )
    parser.add_argument(
        "--coco-spec",
        type=Path,
        default=None,
        help=f"NOMaD canonical class list (name -> id 0-68). Default: {_DEFAULT_COCO_SPEC}",
    )
    parser.add_argument(
        "--unknown-name",
        choices=("error", "drop"),
        default="error",
        help=(
            "If a class name from the input categories is missing from --coco-spec "
            "(e.g. extra 'misc_*' classes), 'error' exits or 'drop' removes those "
            "annotations (default: error)."
        ),
    )
    return parser


def load_superclass_spec(path: Path) -> dict:
    spec = json.loads(path.read_text(encoding="utf-8"))
    if "groups" not in spec or not isinstance(spec["groups"], list):
        raise SystemExit("Superclass file must contain a non-empty 'groups' array.")
    return spec


def name_map_from_categories(categories: list[dict]) -> dict[int, str]:
    return {int(c["id"]): str(c.get("name", "")) for c in categories if "id" in c}


def print_string_counts(title: str, counts: Counter) -> None:
    if not counts:
        print(f"{title}\n  (none)")
        return
    print(title)
    for name in sorted(counts):
        print(f"  {name!s}  {counts[name]}")
    print(f"  -- total  {sum(counts.values())}")


def print_class_counts(
    title: str,
    counts: Counter,
    id_to_name: dict[int, str],
) -> None:
    if not counts:
        print(f"{title}\n  (none)")
        return
    print(title)
    for cid in sorted(counts):
        name = id_to_name.get(cid, "")
        label = f"{name}" if name else ""
        if label:
            print(f"  {cid:3d}  {label!s}  {counts[cid]}")
        else:
            print(f"  {cid:3d}  (unnamed)  {counts[cid]}")
    print(f"  -- total  {sum(counts.values())}")


def load_coco_name_maps(path: Path) -> tuple[dict[str, int], dict[int, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    name_to_id: dict[str, int] = {}
    id_to_name: dict[int, str] = {}
    for c in data.get("categories", []):
        cid = int(c["id"])
        n = str(c["name"])
        if n in name_to_id and name_to_id[n] != cid:
            raise SystemExit(
                f"Ambiguous: two ids for class name {n!r} in {path}"
            )
        name_to_id[n] = cid
        id_to_name[cid] = n
    if not name_to_id:
        raise SystemExit(f"No categories in {path}")
    return name_to_id, id_to_name


def build_old_to_new(
    spec: dict,
) -> tuple[dict[int, int], list[dict]]:
    """Map original category id -> new superclass id (0..G-1) and new category rows."""
    old_to_new: dict[int, int] = {}
    new_categories: list[dict] = []
    for new_id, group in enumerate(spec["groups"]):
        name = str(group.get("name", f"group_{new_id}")).strip()
        sids = group.get("subclass_ids", [])
        if not isinstance(sids, list) or not sids:
            raise SystemExit(
                f"Group {new_id!r} ('{name}') must have a non-empty 'subclass_ids' list."
            )
        for sid in sids:
            o = int(sid)
            if o in old_to_new:
                raise SystemExit(
                    f"Duplicate subclass_id {o}: listed in more than one group."
                )
            old_to_new[o] = new_id
        new_categories.append(
            {
                "id": new_id,
                "name": name,
                "description": "",
                "source_data_model": "NOMaD superclass",
            }
        )
    return old_to_new, new_categories


def main() -> int:
    args = build_parser().parse_args()
    spec_path = args.superclass_json.expanduser().resolve()
    input_path = args.input.expanduser().resolve()
    if args.output is None:
        out_name = f"training_archive__{spec_path.stem}.json"
        output_path = input_path.parent / out_name
    else:
        output_path = args.output.expanduser().resolve()

    spec = load_superclass_spec(spec_path)
    old_to_new, new_categories = build_old_to_new(spec)

    coco_path = (args.coco_spec or _DEFAULT_COCO_SPEC).expanduser().resolve()
    if not coco_path.is_file():
        raise SystemExit(f"--coco-spec not found: {coco_path}")
    name_to_spec_id, spec_id_to_name = load_coco_name_maps(coco_path)

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if "images" not in payload or "annotations" not in payload:
        raise SystemExit("Input must contain 'images' and 'annotations'.")

    in_anns: list[dict] = copy.deepcopy(payload.get("annotations", []))
    in_cat_name = name_map_from_categories(payload.get("categories", []))
    if not in_cat_name:
        raise SystemExit("Input has no 'categories' or empty list; need id -> class names to resolve to coco_spec.")

    start_by_spec: Counter = Counter()
    dropped_name_unknown: Counter = Counter()
    dropped = 0
    out_anns: list[dict] = []
    id_counter = 0
    for ann in in_anns:
        arch_cid = int(ann.get("category_id", -1))
        class_name = in_cat_name.get(arch_cid)
        if class_name is None or class_name == "":
            raise SystemExit(
                f"Annotation id {ann.get('id')}: category_id {arch_cid!r} has no class name in "
                f"input categories. Fix the archive or add a categories entry."
            )
        if class_name not in name_to_spec_id:
            if args.unknown_name == "error":
                raise SystemExit(
                    f"Class name {class_name!r} (archive category_id={arch_cid}) is not in "
                    f"{coco_path}. Add it to coco_spec.json or use --unknown-name drop."
                )
            dropped += 1
            dropped_name_unknown[class_name] += 1
            continue
        spec_cid = name_to_spec_id[class_name]
        start_by_spec[spec_cid] += 1
        if spec_cid not in old_to_new:
            if args.unmapped == "error":
                sample = f" (example class {class_name!r} -> spec id {spec_cid})"
                raise SystemExit(
                    f"Spec category id {spec_cid} is not in any superclass group in "
                    f"{spec_path}{sample}. Add it to a group or use --unmapped drop."
                )
            dropped += 1
            continue
        a = copy.deepcopy(ann)
        a["id"] = id_counter
        id_counter += 1
        a["category_id"] = old_to_new[spec_cid]
        out_anns.append(a)

    end_by_class: Counter = Counter(int(a.get("category_id", -1)) for a in out_anns)
    out_id_to_name = {int(c["id"]): str(c.get("name", "")) for c in new_categories}
    dropped_unmapped_by_spec: Counter = Counter()
    for ann in in_anns:
        arch_cid = int(ann.get("category_id", -1))
        class_name = in_cat_name.get(arch_cid) or ""
        if not class_name or class_name not in name_to_spec_id:
            continue
        spec_cid = name_to_spec_id[class_name]
        if spec_cid not in old_to_new:
            dropped_unmapped_by_spec[spec_cid] += 1

    if args.add_metadata:
        new_cat_names = {c["id"]: c["name"] for c in new_categories}
        counts: Counter[str] = Counter()
        for a in out_anns:
            counts[new_cat_names[a["category_id"]]] += 1
        payload["nomad_superclass_remap"] = {
            "scheme_name": spec.get("name", ""),
            "superclass_file": str(spec_path),
            "coco_spec": str(coco_path),
            "name_to_spec_id_resolution": True,
            "num_groups": len(new_categories),
            "annotations_dropped": dropped,
            "annotations_dropped_not_in_coco": dict(dropped_name_unknown),
            "annotations_dropped_unmapped_by_spec_id": {
                k: v for k, v in dropped_unmapped_by_spec.items() if v
            },
            "annotations_by_superclass": dict(counts),
        }
    else:
        payload.pop("nomad_superclass_remap", None)

    payload["categories"] = new_categories
    payload["annotations"] = out_anns
    if "info" in payload and isinstance(payload["info"], dict):
        payload["info"] = {
            **payload["info"],
            "description": f"{payload['info'].get('description', '')} "
            f"Superclass remapped: {spec.get('name', spec_path.name)}",
        }
    else:
        payload["info"] = {
            "description": f"Superclass remapped: {spec.get('name', spec_path.name)}",
        }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    n_unknown = int(sum(dropped_name_unknown.values()))
    n_unmapped = int(sum(dropped_unmapped_by_spec.values()))

    print(f"Canonical spec: {coco_path} ({len(name_to_spec_id)} classes by name->id)")
    print(f"Superclass: {spec_path} ({spec.get('name', '')})")
    print(f"Input:  {input_path}")
    print(f"Output: {output_path}")
    print(
        f"Mapping: archive category_id -> class name in input categories -> "
        f"spec id in {coco_path.name} -> superclass group (subclass_ids)"
    )
    print(f"Groups: {len(new_categories)}  ->  { [c['name'] for c in new_categories] }")
    print(
        f"Annotations: {len(in_anns)} -> {len(out_anns)}  "
        f"(dropped: {dropped}  [not in coco: {n_unknown}, not in any superclass: {n_unmapped}])"
    )
    print()
    before_title = (
        f"Before: annotations per NOMaD spec class (id + name from {coco_path.name}):"
    )
    if n_unmapped:
        before_title += (
            f"  (totals include {n_unmapped} annotation(s) with a spec class that was "
            f"not in the superclass file and {args.unmapped} was used, so not in 'After' below)"
        )
    print_class_counts(before_title, start_by_spec, spec_id_to_name)
    print()
    print_class_counts(
        "After: annotations per superclass (new category_id):",
        end_by_class,
        out_id_to_name,
    )
    if n_unknown:
        print()
        print_string_counts(
            f"Dropped: class name not in {coco_path.name} (--unknown-name drop):",
            dropped_name_unknown,
        )
    if n_unmapped:
        print()
        print_class_counts(
            f"Dropped: spec class not in any superclass group (see {spec_path.name} subclass_ids, --unmapped drop):",
            dropped_unmapped_by_spec,
            spec_id_to_name,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
