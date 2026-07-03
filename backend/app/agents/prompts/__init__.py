"""SiGMA Agent Prompts — one file per tool.

Each module exports a PROMPT string used by the corresponding tool's
ToolDefinition.prompt field.  Prompts are detailed instructions that
appear in the system message; descriptions are short summaries for the
OpenAI function schema.
"""

from .read import PROMPT as PROMPT_READ
from .write import PROMPT as PROMPT_WRITE
from .edit import PROMPT as PROMPT_EDIT
from .bash import PROMPT as PROMPT_BASH
from .glob import PROMPT as PROMPT_GLOB
from .grep import PROMPT as PROMPT_GREP
from .ls import PROMPT as PROMPT_LS
from .agent import PROMPT as PROMPT_AGENT
from .sleep import PROMPT as PROMPT_SLEEP
from .notebook import (
    PROMPT_NOTEBOOK_EDIT,
    PROMPT_NOTEBOOK_READ,
    PROMPT_NOTEBOOK_RUN_CELL,
)
from .ask_user_question import PROMPT as PROMPT_ASK_USER_QUESTION

from .task import (
    PROMPT_TASK_CREATE,
    PROMPT_TASK_UPDATE,
    PROMPT_TASK_LIST,
    PROMPT_TASK_GET,
    PROMPT_TASK_WRITE,
)

from .browser import (
    PROMPT_BROWSER_NAVIGATE,
    PROMPT_BROWSER_SNAPSHOT,
    PROMPT_BROWSER_CLICK,
    PROMPT_BROWSER_INPUT,
    PROMPT_BROWSER_SCROLL,
    PROMPT_BROWSER_CONSOLE,
    PROMPT_BROWSER_VISION,
    PROMPT_BROWSER_BACK,
    PROMPT_BROWSER_CDP,
    PROMPT_BROWSER_PAGES,
)

from .library import (
    PROMPT_LIBRARY_SEARCH,
    PROMPT_LIBRARY_LS,
    PROMPT_LIBRARY_NEW,
    PROMPT_LIBRARY_MKDIR,
    PROMPT_LIBRARY_MV,
    PROMPT_LIBRARY_UPDATE,
    PROMPT_LIBRARY_GET,
    PROMPT_LIBRARY_RM,
)

from .annotation import (
    PROMPT_ANNOTATION_NEW,
    PROMPT_ANNOTATION_RM,
    PROMPT_ANNOTATION_GET,
    PROMPT_ANNOTATION_REPLY,
    PROMPT_ANNOTATION_LIST,
)

from .skill import PROMPT as PROMPT_SKILL_LOAD

from .draw import PROMPT as PROMPT_DRAW_IMAGE
from .vision import PROMPT as PROMPT_VISION_ANALYZE
