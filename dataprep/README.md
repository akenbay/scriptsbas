# Drill-core dataset preparation scripts

Scripts for preparing a drill-core photo and journal dataset. This repository
contains **code only**. No photos, journals, drill hole IDs or results are
stored here (see `.gitignore`).

Requirements: Python 3.6+ and nothing else (standard library only).

## Step 1 - Inventory, checksums and file checks (`inventory.py`)

Read-only on the source folder: it never changes, moves or copies the
original files.

### First-time setup

1. Get the code on the processing computer:
   ```
   git clone <repo-url>
   ```
   Or later, to update: `git pull`. Then `cd scriptsbas\dataprep`.
2. Copy `config.example.json` to `config.json` in this `dataprep` folder and enter
   the real drill hole IDs. `config.json` stays on that computer and is never
   committed.

### Run

```
python inventory.py "D:\path\to\original_files" "D:\gold_core_dataset"
```

The first path is the folder with the original photos, journals and documents.
The second is where the results are written. It must not be inside the first.

### Outputs

| File | Content |
|---|---|
| `metadata/source_inventory.csv` | One row per file: path, type, size, SHA-256, drill hole, box, capture time, camera, image size, duplicate group, QC flags |
| `metadata/sha256sums.txt` | Checksums in standard `sha256sum` format |
| `metadata/inventory_run_info.json` | Script version, Python version, settings, counts |
| `metadata/history/` | Previous inventories, archived automatically on each rerun |
| `quality_control/validation_report.csv` | All issues, errors first, `review_status = open` |
| `quality_control/checksum_verification.csv` | Changed, missing and new files compared with the previous run |

CSV files are UTF-8 with BOM so Excel shows Cyrillic correctly.

### Issues reported

| Issue | Severity |
|---|---|
| `empty_file`, `file_unreadable`, `invalid_raw_header`, `truncated_raw` | error |
| `duplicate_across_boxes`: identical file under different drill holes/boxes | error |
| `drillhole_conflict` / `box_conflict`: folder and file name disagree | error |
| `drillhole_ambiguous` / `box_ambiguous`: several IDs in one name | error |
| `file_changed_since_last_run`, `file_missing_since_last_run` | error |
| `drillhole_without_images` | error |
| `exact_duplicate`: identical file in the same box | warning |
| `drillhole_unassigned`, `box_unassigned` | warning |
| `missing_box_numbers`: gaps in the box sequence | warning |
| `journal_missing` | warning |
| `missing_capture_time`, `same_name_different_content`, `unknown_file_type`, `new_file_since_last_run`, `multiple_journals` | info |

On any disagreement the script leaves the field blank and reports the
issue. It never guesses.

### How drill hole and box are detected

From folder names and the file name. Drill hole IDs come from `config.json`.
Box numbers are matched by `box_patterns`, which by default accepts names like
`box_012`, `Box12`, `BX-3`, `ящик 7`, `Ящ_07`. If your folders are named only
with a number (e.g. `012`), add `--bare-number-folders-are-boxes`.

### Notes

- Image size is the size reported by the camera in the CR2 header. The exact
  sensor size will be recorded by the RAW conversion step.
- CR3 files are inventoried and hashed, but their headers are not parsed.
