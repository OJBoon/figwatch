"""Claude CLI (subprocess) provider."""

import subprocess
import tempfile
from pathlib import Path

_HOME = Path.home()


class ClaudeCLIProvider:
    inline_files = False

    def __init__(
        self, model: str, claude_path: str, skill_dir: str = '', gateway: 'dict | None' = None,
    ):
        # `gateway` is the dict from figwatch.gateway.gateway_info() when a
        # cc-switch / custom-gateway profile is active for the Claude CLI, else
        # None. It is injected (not read here) so provider construction stays
        # pure and testable. In gateway mode the model is dictated by the
        # gateway's ANTHROPIC_MODEL, so surface that in the sign-off rather than
        # the unused public alias.
        self._gateway = gateway
        self.model_id = (gateway.get('model') if gateway else None) or model
        self._model = model
        self._claude_path = claude_path
        self._skill_dir = skill_dir

    def call(self, prompt: str, image_path: 'str | None') -> str:
        # image_path is unused — the path is embedded in the prompt text and
        # Claude reads it directly via the Read tool (--add-dir /tmp).
        from figwatch.handlers import parse_claude_output, subprocess_env

        cmd = [
            self._claude_path, '-p', prompt,
            '--print', '--allowedTools', 'Read',
        ]
        # Only pin --model in personal mode. A gateway rejects public model ids
        # with 400 "model not found"; let its configured ANTHROPIC_MODEL apply.
        if not self._gateway:
            cmd.extend(['--model', self._model])
        cmd.extend(['--add-dir', tempfile.gettempdir()])
        if self._skill_dir:
            cmd.extend(['--add-dir', self._skill_dir])

        result = subprocess.run(
            cmd, capture_output=True, timeout=300,
            env=subprocess_env(), cwd=tempfile.gettempdir(),
        )
        return parse_claude_output(result)
