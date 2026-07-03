"""
Notebook tool prompts — read, edit, and execute Jupyter notebook cells.
"""

PROMPT_NOTEBOOK_READ = """Reads Jupyter notebook cells and their execution outputs (.ipynb files). \
Returns cells in XML format.

notebook_path accepts absolute paths only inside the project, or project-relative paths.

By default, when cell_id is omitted, notebook_read returns cells starting at \
offset=0 with limit=5. Each returned cell includes index="{N}", the 0-indexed \
position of that cell in the notebook. Use offset/limit to page through cells.

When cell_id is provided, notebook_read returns that cell's full source and \
pages only its outputs: offset is the 0-indexed output line, and limit is the \
number of output lines to show (default 200). When cell_id is omitted, outputs \
inside each returned cell show at most the first 100 output lines. Cell source \
is not truncated. If outputs are truncated, use notebook_read with cell_id plus \
the suggested offset/limit to continue reading the remaining output lines.

Example output:
<notebook cells="3" offset="0" limit="5" kernel="idle">
<cell id="a1b2c3d4" type="code" index="0">
  <source>import pandas as pd
df = pd.read_csv('data.csv')</source>
</cell>
<cell id="e5f6a7b8" type="markdown" index="1">
  <source># Analysis</source>
</cell>
<cell id="c9d0e1f2" type="code" index="2">
  <source>result = 1 / 0</source>
  <outputs>
    <output type="error" name="ZeroDivisionError">division by zero</output>
  </outputs>
</cell>
</notebook>

The root <notebook> element includes a kernel attribute when a kernel session exists. \
Possible values include kernel="idle", kernel="busy", kernel="starting", kernel="dead", \
or kernel="unknown". If no kernel session exists, the attribute is omitted.

Cell IDs are shown in each <cell id="..."> attribute. Use exactly the IDs shown by \
notebook_read. Prefixes are accepted for stable cell IDs, but the full shown ID is preferred.

The tool reads from the Jupyter server's live state when available, so \
outputs reflect the latest kernel execution results."""


PROMPT_NOTEBOOK_EDIT = """Edit Jupyter notebook cells (.ipynb files). \
Supports replace, insert, and delete operations on cells.

notebook_path accepts absolute paths only inside the project, or project-relative paths.

Always read the notebook first with notebook_read to understand the current structure \
before making edits. This is enforced by the tool: notebook_edit fails if this \
notebook has not been read in the current session, or if it changed on disk \
since the last notebook_read/notebook_edit/notebook_run_cell.

Cell IDs come from the <cell id="..."> attribute shown by notebook_read. \
Prefixes are accepted only for stable cell IDs, not numeric fallback IDs.

Edit modes:
- replace (default): Replace text in the specified cell.
  - With old_string: finds and replaces old_string with new_string within the cell. \
old_string must match exactly once. Useful for small targeted edits.
  - Without old_string: replaces the ENTIRE cell source with new_string.
  Code cells have execution_count and outputs cleared.
- insert: Insert a new cell AFTER the specified cell (or at the beginning if cell_id is empty). \
cell_type is required. The tool returns the new cell ID.
- delete: Delete the specified cell.

After editing, the notebook is saved and the user's browser view updates automatically."""


PROMPT_NOTEBOOK_RUN_CELL = """Executes a code cell in a Jupyter notebook on its kernel, \
writes the results back into the cell, and returns the output. \
Jupyter must be running. Opening a notebook in the editor starts Jupyter when needed.

notebook_path accepts absolute paths only inside the project, or project-relative paths.

Only code cells can be executed. Markdown cells will return an error.

The cell's code runs on the notebook's kernel — variables persist between executions. \
For example, if one cell creates `df = pd.read_csv('data.csv')`, executing another \
cell can reference `df` directly.

Always use notebook_read first to check cell state, outputs, and kernel status \
before deciding what to do. This is enforced by the tool: notebook_run_cell \
fails if this notebook has not been read in the current session, or if it \
changed on disk since the last notebook_read/notebook_edit/notebook_run_cell.

Output includes streams, display values, function return values, and errors with tracebacks. \
If execution times out, the kernel is automatically interrupted.

If the kernel is busy, the tool will return an error suggesting interrupt=true."""
