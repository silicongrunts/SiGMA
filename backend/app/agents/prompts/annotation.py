"""
Annotation tool prompts — create, read, delete, reply to, and list file annotations.
"""

PROMPT_ANNOTATION_NEW = """Create an annotation anchored to a text range in a project file.

file_content must appear exactly once in the file — zero or multiple matches is not allowed.

annotation_content may contain <diff> blocks to suggest code changes:
<diff><before>original text</before><after>new text</after></diff>
Each <before> must appear exactly once in the file — zero or multiple matches is not allowed..

Tool result: "Annotation created successfully, ID: {id}" on success.  
These IDs are for subsequent tool calls only and not visible to the user."""

PROMPT_ANNOTATION_RM = """Delete one or more annotations.

Accepts a single ID or an array of IDs. Provide at least the first 8 characters of each ID for prefix matching.

Tool result, one line per ID: "Annotation {id} deleted" or "Error for {id}: {reason}". These IDs are for subsequent tool calls only."""

PROMPT_ANNOTATION_GET = """Retrieve the full thread of one or more annotations.

Accepts a single ID or an array of IDs. Provide at least the first 8 characters of each ID for prefix matching.

Tool result (XML):
<annotation><id>full-id</id><file_content>original text snapshot</file_content><reply><role>SiGMA|user</role><text>message content</text></reply>...</annotation>
One block per matched annotation. Errors shown inline as "Error for {id}: {reason}"."""

PROMPT_ANNOTATION_REPLY = """Reply to an existing annotation.

reply_content may contain <diff> blocks:
<diff><before>original text</before><after>new text</after></diff>
Each <before> must appear exactly once in the annotation's file.

Provide at least the first 8 characters of the ID for prefix matching.

Tool result: "Reply added to annotation {id}" or "Error: {reason}"."""

PROMPT_ANNOTATION_LIST = """List annotations on one or more project files.

Accepts a single file path or an array of file paths.

Tool result (XML):
--- {file} ({N} annotations) ---
<annotation><id>full-id</id><summary>first 80 chars of first reply...</summary></annotation>
Grouped by file. Files with no annotations show "No annotations on {file}"."""
