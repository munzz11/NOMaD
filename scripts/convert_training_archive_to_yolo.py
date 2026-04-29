#!/usr/bin/env python3
"""Convert NOMaD training_archive.json to YOLOv5 label text files and split layout."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
from collections import Counter, defaultdict
from pathlib import Path


SPLITS = ("train", "val", "test")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert a COCO-like training archive to YOLO label files, "
            "optionally organizing images, labels, and COCO JSON annotations "
            "under train/val/test subfolders."
        )
    )
    parser.add_argument(
        "--archive",
        default="data/training_archive.json",
        help="Path to training archive JSON (default: data/training_archive.json).",
    )
    parser.add_argument(
        "--images-dir",
        default="data",
        help="Directory containing flat image files referenced by the archive.",
    )
    parser.add_argument(
        "--output-root",
        default="data/dataset",
        help="Dataset root: images/{train,val,test}, labels/..., annotations/...",
    )
    parser.add_argument(
        "--image-action",
        choices=("copy", "move", "symlink"),
        default="copy",
        help="How to place each image from --images-dir into the split image folders.",
    )
    parser.add_argument(
        "--default-split",
        choices=SPLITS,
        default="train",
        help="Split when an annotation has no split, or for images with no annotations.",
    )
    parser.add_argument(
        "--skip-empty-labels",
        action="store_true",
        help="Do not create empty .txt label files for images without annotations.",
    )
    parser.add_argument(
        "--no-coco",
        action="store_true",
        help="Do not write per-split COCO JSON under annotations/{split}/.",
    )
    parser.add_argument(
        "--write-dataset-yaml",
        action="store_true",
        help="Write dataset.yaml under --output-root for YOLOv5 (names from archive categories).",
    )
    return parser


def normalize_split(raw_value: str | None, default_split: str) -> str:
    if not raw_value:
        return default_split

    value = raw_value.strip().lower()
    if value in {"validate", "validation", "val"}:
        return "val"
    if value in {"train", "training"}:
        return "train"
    if value in {"test", "testing"}:
        return "test"
    return default_split


def clamp_box(
    x: float, y: float, w: float, h: float, img_w: float, img_h: float
) -> tuple[float, float, float, float]:
    x = max(0.0, min(x, img_w))
    y = max(0.0, min(y, img_h))
    w = max(0.0, w)
    h = max(0.0, h)

    if x + w > img_w:
        w = img_w - x
    if y + h > img_h:
        h = img_h - y

    return x, y, w, h


def yolo_line(class_id: int, bbox: list[float], width: int, height: int) -> str | None:
    if len(bbox) != 4:
        return None

    x, y, w, h = (float(v) for v in bbox)
    x, y, w, h = clamp_box(x, y, w, h, float(width), float(height))
    if w <= 0.0 or h <= 0.0:
        return None

    x_center = (x + w / 2.0) / float(width)
    y_center = (y + h / 2.0) / float(height)
    w_norm = w / float(width)
    h_norm = h / float(height)

    return f"{class_id} {x_center:.6f} {y_center:.6f} {w_norm:.6f} {h_norm:.6f}"


def assign_image_split(
    split_votes: Counter[str],
    default_split: str,
) -> str:
    if not split_votes:
        return default_split
    return split_votes.most_common(1)[0][0]


def place_image(
    source: Path,
    destination: Path,
    action: str,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()

    if action == "copy":
        shutil.copy2(source, destination, follow_symlinks=True)
    elif action == "move":
        shutil.move(str(source), str(destination))
    else:
        try:
            os.symlink(source.resolve(), destination, target_is_directory=False)
        except OSError:
            if destination.exists():
                destination.unlink()
            shutil.copy2(source, destination, follow_symlinks=True)


def make_coco_subset(
    image_by_id: dict[int, dict],
    old_annotations: list[dict],
    categories: list[dict],
    image_ids: set[int],
) -> dict:
    """COCO JSON for one split with remapped image and annotation ids."""
    sorted_ids = sorted(image_ids)
    old_id_to_new: dict[int, int] = {oid: new_id for new_id, oid in enumerate(sorted_ids)}

    new_images: list[dict] = []
    for old_id in sorted_ids:
        img = image_by_id[old_id]
        row = copy.deepcopy(img)
        row["id"] = old_id_to_new[old_id]
        new_images.append(row)

    new_annotations: list[dict] = []
    next_ann = 0
    for ann in old_annotations:
        oid = int(ann.get("image_id", -1))
        if oid not in old_id_to_new:
            continue
        a = copy.deepcopy(ann)
        a["id"] = next_ann
        next_ann += 1
        a["image_id"] = old_id_to_new[oid]
        new_annotations.append(a)

    return {
        "info": {
            "description": "NOMaD (subset derived from training_archive.json)",
        },
        "licenses": [],
        "categories": copy.deepcopy(categories),
        "images": new_images,
        "annotations": new_annotations,
    }


def yolo_names_yaml_block(categories: list[dict]) -> str:
    lines = [f"  {c['id']}: {c['name']}" for c in sorted(categories, key=lambda x: int(x["id"]))]
    return "names:\n" + "\n".join(lines) + "\n"


def main() -> int:
    args = build_parser().parse_args()
    archive_path = Path(args.archive).expanduser().resolve()
    images_dir = Path(args.images_dir).expanduser().resolve()
    out_root = Path(args.output_root).expanduser().resolve()

    for sub in ("images", "labels", "annotations"):
        for s in SPLITS:
            (out_root / sub / s).mkdir(parents=True, exist_ok=True)

    payload = json.loads(archive_path.read_text(encoding="utf-8"))
    images = payload.get("images", [])
    all_annotations = payload.get("annotations", [])
    categories = payload.get("categories", [])

    image_by_id: dict[int, dict] = {}
    for image in images:
        image_by_id[int(image["id"])] = image

    labels_by_image_id: dict[int, list[str]] = defaultdict(list)
    split_votes_by_image_id: dict[int, Counter[str]] = defaultdict(Counter)
    skipped = 0

    for ann in all_annotations:
        image_id = int(ann.get("image_id", -1))
        image_meta = image_by_id.get(image_id)
        if image_meta is None:
            skipped += 1
            continue

        width = int(image_meta.get("width", 0))
        height = int(image_meta.get("height", 0))
        if width <= 0 or height <= 0:
            skipped += 1
            continue

        line = yolo_line(int(ann["category_id"]), ann.get("bbox", []), width, height)
        if line is None:
            skipped += 1
            continue

        labels_by_image_id[image_id].append(line)
        split_name = normalize_split(ann.get("split"), args.default_split)
        split_votes_by_image_id[image_id][split_name] += 1

    image_id_to_split: dict[int, str] = {}
    for iid, image in image_by_id.items():
        explicit = (image.get("split") or "").strip()
        if explicit:
            image_id_to_split[iid] = normalize_split(explicit, args.default_split)
        else:
            image_id_to_split[iid] = assign_image_split(
                split_votes_by_image_id.get(iid) or Counter(), args.default_split
            )

    split_to_image_ids: dict[str, list[int]] = {s: [] for s in SPLITS}
    for iid, sp in image_id_to_split.items():
        split_to_image_ids[sp].append(iid)

    moved_or_linked = 0
    miss_files: list[str] = []
    label_files_written = 0
    annotation_json_written = 0

    for split in SPLITS:
        for image_id in split_to_image_ids[split]:
            image = image_by_id[image_id]
            file_name = str(image["file_name"])
            stem = Path(file_name).stem
            label_lines = labels_by_image_id.get(image_id, [])

            label_rel = out_root / "labels" / split / f"{stem}.txt"
            if label_lines or not args.skip_empty_labels:
                label_rel.write_text(
                    "\n".join(label_lines) + ("\n" if label_lines else ""),
                    encoding="utf-8",
                )
                label_files_written += 1

            source_path = images_dir / file_name
            dest_path = out_root / "images" / split / file_name
            if not source_path.is_file():
                miss_files.append(str(source_path))
                continue
            if dest_path.resolve() != source_path.resolve():
                place_image(source_path, dest_path, args.image_action)
                moved_or_linked += 1

    split_to_list_lines: dict[str, list[str]] = {s: [] for s in SPLITS}
    for split in SPLITS:
        for iid in sorted(split_to_image_ids[split]):
            fn = str(image_by_id[iid]["file_name"])
            split_to_list_lines[split].append(
                str((out_root / "images" / split / fn).as_posix())
            )

    for split, list_name in (("train", "train.txt"), ("val", "val.txt"), ("test", "test.txt")):
        lines = split_to_list_lines[split]
        (out_root / list_name).write_text(
            "\n".join(sorted(lines)) + ("\n" if lines else ""), encoding="utf-8"
        )

    categories_path = out_root / "categories.names"
    categories_path.write_text(
        "\n".join(cat["name"] for cat in sorted(categories, key=lambda c: int(c["id"]))) + "\n",
        encoding="utf-8",
    )

    if not args.no_coco:
        for split in SPLITS:
            iids = set(split_to_image_ids[split])
            if not iids:
                continue
            coco = make_coco_subset(image_by_id, all_annotations, categories, iids)
            ann_path = out_root / "annotations" / split / "instances.json"
            ann_path.write_text(json.dumps(coco, indent=2) + "\n", encoding="utf-8")
            annotation_json_written += 1

    if args.write_dataset_yaml:
        names_block = yolo_names_yaml_block(categories)
        ds = f"""# Generated by scripts/convert_training_archive_to_yolo.py
# Relative to this file (or pass absolute path: path: /abs/...)
path: {out_root.as_posix()}

train: images/train
val: images/val
test: images/test

{names_block}"""
        (out_root / "dataset.yaml").write_text(ds, encoding="utf-8")

    print(f"Archive: {archive_path}")
    print(f"Source images: {images_dir}")
    print(f"Output root: {out_root}")
    print(f"Image action: {args.image_action}")
    print(f"Images in archive: {len(images)}")
    print(f"Annotations: {len(all_annotations)}")
    print(f"Label files written: {label_files_written}")
    print(f"Images placed: {moved_or_linked} ({args.image_action})")
    if miss_files:
        print(f"Missing source image files: {len(miss_files)} (first 10 shown)")
        for p in miss_files[:10]:
            print(f"  {p}")
    print(f"Skipped annotations: {skipped}")
    print(f"Per-split COCO files written: {annotation_json_written}")
    print(f"Wrote: {out_root / 'train.txt'}, val.txt, test.txt, {categories_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
