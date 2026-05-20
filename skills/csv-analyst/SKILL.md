---
name: csv-analyst
description: Profile a CSV file (row count, columns, dtypes, top-k uniques per column) using pandas-free stdlib code.
allowed_tools:
  - bash
  - read_file
  - run_skill_script
auto_load: false
---

# csv-analyst skill

You have access to this skill when the user asks to **analyse, profile,
summarise, or describe** a CSV file. Workflow:

1. Confirm the file path. If unknown, ask once or use `bash` to `ls`.
2. Run the bundled profiler:

   ```
   run_skill_script(name="csv-analyst", script="profile.py",
                    args=["<path-to-csv>"])
   ```

   It returns JSON with:
   - `rows`, `cols`
   - per-column: `dtype_guess`, `nulls`, `unique`, `top` (top-5 values + counts)

3. Surface findings in a short markdown table. Highlight columns where
   `nulls / rows > 0.2` or `unique == 1`.

4. If the user asks for deeper stats (mean / median / stddev), fall back
   to inline `bash` with `awk` or a quick Python one-liner — do NOT add
   pandas as a dependency.

## Constraints

- Never write to the source CSV.
- If the file is larger than 100 MB, stream it; do not load it all in memory.
- Quote every column name in your reply (some columns may have spaces).
