"""Script editor — checklist gate. Sends the draft back when a rule fails."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agents import Agent

from models import editor_chat
from schemas import ScriptVerdict

INSTRUCTIONS = """\
You are the script editor. You do not rewrite. You pass or you send the draft back.

Set passed to false if any of these are true:
- The logline's want or obstacle is missing from the pages.
- The hook is not inside the first 10% of the runtime.
- A scene does not turn.
- A line of dialogue states the theme or the subtext.
- Two characters share the same rhythm and vocabulary.
- An action line describes a thought, a memory, or a feeling instead of a visible act.
- An action paragraph runs past four lines.
- A planted detail is never paid off, or a payoff was never planted.
- Shot durations do not add up to the locked runtime.
- The number of shots exceeds the explicit shot_budget in the user message.
- A shot contains more than one continuous action, or more than one speaker.
  The fix is to keep the one action or the one speaker that carries the beat, and move the rest to audio or notes.
  Do not ask for a new shot, a separate shot, or a split. A longer shot list is not a fix.
- A character cue is not preceded by a blank line, or a heading does not start with INT. or EXT.

Each issue names the beat or the line and the smallest change that fixes it.
When the draft passes, issues is empty and passed is true.
Do not invent taste notes. Fail only on the list above.
"""

editor_agent = Agent(
    name="ScriptEditor",
    instructions=INSTRUCTIONS,
    model=editor_chat,
    output_type=ScriptVerdict,
)
