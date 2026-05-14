## PowerBI Thingamajigs

Some scripts for stuffs that I believe PowerBI Desktop should have done better, but they didn't so I resorted to Python.

Only using base libs because I can't install packages on my work machine.

### prune_pbi.py

A surgical data model pruner that reduces .pbip file size by removing unused components while strictly maintaining report integrity.

#### How it Works (Dependency Logic)
The script uses a multi-pass **Structural Dependency Tracing** engine:
1.  **Report Surface Scan**: Scans all visual definitions (JSON/PBIR) for field references. In Legacy mode, it uses recursive unescaping to find fields hidden in nested JSON strings.
2.  **Structural Locking**: Automatically protects all columns involved in Relationships (`relationships.tmdl`) and system-managed tables (Auto Date/Time).
3.  **Transitive DAX Tracing**: Parses DAX formulas for measures and calculated columns. It uses a **Global Measure Map** to resolve cross-table references like `[MeasureName]` even without table prefixes.
4.  **Deep Pruning**: Only after every visual, relationship, and DAX dependency is satisfied does it safely remove unused columns and measures from the TMDL files.

#### Verification
- **Safety**: Protects the "Skeleton" of the model (Relationships).
- **Correctness**: Ensures visual "Green Ticks" and DAX calculations remain functional.

```bash
uv sync

uv run prune_pbi.py "path_to_pbip_directory/" --run
```