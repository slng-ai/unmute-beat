"""Offline inspection of actual native generation settings, including defaults.

Run with the POC interpreter. GOOGLE_API_KEY and other required values are dummy
process-local placeholders; the client factory is replaced before construction.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / 'bench/raw/native-reference/examples/salon-concierge/build/pipecat'
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BUILD))
sys.path.append(
    '/Users/nicoferdi/Documents/GitHub/unmute_cli/examples/salon-concierge/build/pipecat/.venv/lib/python3.14/site-packages'
)
for name in json.loads((BUILD / 'compile-report.json').read_text())['required_env']:
    os.environ[name] = 'offline-config-probe'
spec = importlib.util.spec_from_file_location('native_config_probe', BUILD / 'bot.py')
assert spec and spec.loader
bot = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bot
spec.loader.exec_module(bot)
bot._google_vertex_client = lambda *args: None
for builder in (bot.build_concierge_llm, bot.build_complaint_specialist_llm):
    service = builder(bot.build_state())
    params = service._build_generation_params()
    assert service._settings.model == 'gemini-3.5-flash-lite'
    assert 'max_output_tokens' not in params, params
    assert params['thinking_config']['thinking_level'] == 'MINIMAL'
    assert params['http_options']['headers'] == {'X-Vertex-AI-LLM-Shared-Request-Type': 'priority'}
    assert not any(key in params for key in ('temperature', 'top_p', 'top_k', 'seed'))
    print(builder.__name__, json.dumps(params, default=str))
code = (BUILD / 'bot.py').read_text()
assert code.count('text_aggregation_mode="token"') == 2
assert 'SmartTurnParams(stop_secs=1.5)' in code
assert 'VADParams(stop_secs=0.2)' in code
print('native settings ok: model, thinking, priority, no output cap; configured audio settings match the POC')
