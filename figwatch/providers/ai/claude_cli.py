"""Claude CLI (subprocess) provider."""

import subprocess
import tempfile
from pathlib import Path

_HOME = Path.home()


class ClaudeCLIProvider:
    name = 'Claude'
    inline_files = False

    def __init__(self, model: str, claude_path: str, skill_dir: str = ''):
        self._model = model
        self._claude_path = claude_path
        self._skill_dir = skill_dir

    def call(self, prompt: str, image_path: 'str | None') -> str:
        # image_path is unused — the path is embedded in the prompt text and
        # Claude reads it directly via the Read tool (--add-dir /tmp).
        from figwatch.handlers import subprocess_env, parse_claude_output

        cmd = [
            self._claude_path, '-p', prompt,
            '--print', '--allowedTools', 'Read',
            '--model', self._model,
            '--add-dir', tempfile.gettempdir(),
        ]
        if self._skill_dir:
            cmd.extend(['--add-dir', self._skill_dir])

        result = subprocess.run(
            cmd, capture_output=True, timeout=120,
            env=subprocess_env(), cwd=str(_HOME),
        )
        return parse_claude_output(result)
