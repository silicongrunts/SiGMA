"""
Notebook tool prompts — read, edit, and execute Jupyter notebook cells.
"""

PROMPT_NOTEBOOK_READ = """Read Jupyter notebook cells and their execution outputs (.ipynb files). Returns cells in XML format.

- When cell_id is omitted: return cells starting at offset (0-indexed cell index, default 0), limit cells at a time (default 5). Each cell includes index="{N}", the 0-indexed position of the cell in the notebook. Outputs inside each cell show at most the first 100 output lines; cell source is never truncated.
- When cell_id is provided: return that cell's full source and page only its outputs — offset is the 0-indexed output line, limit the number of output lines to show (default 200).
- If outputs are truncated, re-read with cell_id and the suggested offset/limit to continue.

Output:
<notebook cells="3" offset="0" limit="5" kernel="idle">
<cell id="a1b2c3d4e5f6a7b8" type="code" index="0">
  <source>import pandas as pd
df = pd.read_csv('data.csv')</source>
</cell>
<cell id="c9d0e1f2a3b4c5d6" type="markdown" index="1">
  <source># Analysis</source>
</cell>
<cell id="e7f8a9b0c1d2e3f4" type="code" index="2">
  <source>result = 1 / 0</source>
  <outputs>
    <output type="error" name="ZeroDivisionError">division by zero</output>
  </outputs>
</cell>
</notebook>

The root element includes kernel="idle|busy|starting|dead|unknown" when a kernel session exists; the attribute is omitted otherwise. Cell IDs come from the <cell id="..."> attribute — use exactly the IDs shown. Reads come from the Jupyter server's live state when available, so outputs reflect the latest kernel executions."""


PROMPT_NOTEBOOK_EDIT = """Edit Jupyter notebook cells (.ipynb files).

Read the notebook with notebook_read first — this is enforced by the tool: it fails if the notebook was not read in this session (one written with the write tool counts), or if it changed on disk since the last notebook_read/notebook_edit/notebook_run_cell; a compaction resets this state — re-read after compact. Cell IDs come from the <cell id="..."> attribute shown by notebook_read.

Edit modes:
- replace (default): with old_string, replace old_string with new_string inside the cell (old_string must match exactly once); without old_string, replace the ENTIRE cell source with new_string. Replacing clears execution_count and outputs on code cells.
- insert: insert a new cell AFTER the cell with the given cell_id (or at the beginning if cell_id is empty). cell_type ('code' or 'markdown') is required. Returns the new cell ID.
- delete: delete the specified cell.

The notebook is saved after editing; the user's editor view updates automatically."""


PROMPT_NOTEBOOK_RUN_CELL = """Execute a code cell on the notebook's Jupyter kernel, write the outputs back into the cell, and return them. Jupyter must be running — opening a notebook in the editor starts it when needed.

- Only code cells can be executed; markdown cells return an error.
- Variables persist on the kernel between executions (e.g. a df created in one cell is usable in the next).
- Read the notebook first — this is enforced by the tool: it fails if the notebook was not read in this session (one written with the write tool counts) or changed on disk since the last notebook_read/notebook_edit/notebook_run_cell; a compaction resets this state — re-read after compact.
- Output includes streams, display values, return values, and errors with tracebacks (first 8 lines); total output is capped at 100,000 characters. On timeout the kernel is automatically interrupted.
- If the kernel is busy, the tool returns an error suggesting interrupt=true. If it is dead or in an unknown state, no tool can restart it — ask the user to restart the kernel.

Output: "[{status}] Execution count: {N}" followed by <outputs> blocks ("[{status}]" alone when the count is unknown, e.g. on timeout), or "(no output)" when the execution produced none."""
