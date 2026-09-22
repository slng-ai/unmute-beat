"""Compile the selected native reference with this POC's facts and business service.

Run as a module with the unmute repository path. Only the output copy is edited.
The generated Workers/Flows routing remains native Pipecat; no Pydantic AI agent
or execution core is imported by its business-tool adapter.
"""

import argparse
import hashlib
import json
import subprocess
import tarfile
from io import BytesIO
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REVISION = '2ddc7b5c9f15e307a470ff9013200bea1a3000e9'


def prepare(repo: Path, output: Path) -> Path:
    """Copy a pinned reference, adapt its package inputs, then invoke its compiler."""
    archive = subprocess.check_output(
        ['git', '-C', str(repo), 'archive', REVISION, 'examples/salon-concierge']
    )
    output.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=BytesIO(archive)) as source:
        source.extractall(output, filter='data')
    package = output / 'examples/salon-concierge'
    spec = yaml.safe_load((package / 'agent.yaml').read_text())
    spec.pop('knowledge')
    spec.pop('escalations')
    spec.pop('destinations')
    spec['secrets'].remove('MANAGER_PHONE_NUMBER')
    spec['secrets'].append('NATIVE_CALL_ID')
    lookups = {'look_up_refund_policy', 'look_up_salon_info'}
    spec['tools'] = [name for name in spec['tools'] if name not in lookups]
    for agent in spec['agents'].values():
        agent.pop('escalations', None)
        agent['tools'] = [name for name in agent['tools'] if name not in lookups]
    verify = spec['agents']['concierge']['tasks'][0]
    verify['when'] = 'Before a complaint if not verified, or after a phone correction.'
    verify['announce'] = "Let me confirm who I'm speaking to."
    spec['agents']['concierge']['tasks'][1]['announce'] = "I'll check the diary."
    spec['models']['think']['reasoning'].update(
        model='gemini-3.5-flash-lite',
        params={
            'vertexai': True,
            'location': 'global',
            'max_tokens': None,
            'thinking_config': {'thinking_level': 'MINIMAL'},
            'extra': {'http_options': {'headers': {'X-Vertex-AI-LLM-Shared-Request-Type': 'priority'}}},
        },
    )
    (package / 'agent.yaml').write_text(yaml.safe_dump(spec, sort_keys=False))

    voice = (ROOT / 'prompts/salon/_voice.md').read_text().split('## Actions')[0]
    voice += '\nOnly describe a change as completed after a successful tool result.\n'
    services = (ROOT / 'knowledge/services.md').read_text()
    guidance = (ROOT / 'knowledge/complaint-guidance.json').read_text()
    diary = (ROOT / 'prompts/salon/_diary.md').read_text().replace('read_diary', 'find_slots')
    diary = diary.replace('use `ask_booking`', 'read back the action and ask for approval')
    prompts = {
        'instructions.md': """## Your role: concierge
Answer public salon questions from the information below without identification.
For booking, moving or cancelling, run book; it verifies first when necessary.
For complaints, refunds or a manager request, run verify_customer first unless
customer_verified is nonempty, then to_complaints. There is no live manager transfer.
Corrected phone numbers also need verify_customer. Never verify twice unchanged.
A future booking requested free because of a past problem is a complaint.
After a completed flow, continue from its result. End only when the caller is done.
Current verification: {{customer_verified}}. Latest appointment: {{appointment}}.
"""
        + services,
        'tasks/verify-customer.md': """## Verify the customer
Combine phone fragments. Ask only for missing digits or country information.
Accept valid Spanish numbers in full international form, +34 and nine digits.
If the country is missing, ask; never infer it. Read every digit back, then wait for yes.
When a saved number is present, read it back in the first question instead of asking
for a number again. Include the country code. Never drop or change digits.
A correction needs another readback. Giving digits or asking a question isn't yes.
Only after approval, call find_or_create_customer with the confirmed number.
That tool finishes verification; the pending request continues without a new greeting.
If the caller declines or changes subject, finish with the unserved request.
Saved number: {{customer_phone}}. Status: {{customer_verified}}.
""",
        'tasks/booking.md': """## Handle one booking change
The caller is identified. Use returned diary identifiers and preserve unchanged
details. Save a clear selection of an offered slot immediately. An explicit move
at the same time is approval if that slot is available. For cancellation or an
unresolved choice, read back the action and wait for agreement before saving.
Ask move versus additional when ambiguous; don't infer a second appointment.
After success say the returned service and spoken date/time once. A refusal
needs an honest next step. A complaint goes to_complaints; abandon via finish.
Latest appointment: {{appointment}}.
"""
        + diary,
        'agents/complaint-specialist.md': """## Your role: complaints
Help the verified caller record their complaint and understand the next steps.
If not identified, run verify_customer before collecting complaint details.
Use the conversation. Ask what happened or what outcome they want only if missing.
Never suggest or arrange a free redo. A caller's free-service request stays here
for review; it neither changes nor makes an existing booking free.
Read back short exact excerpts of their complaint and requested resolution, then
ask whether to record them. Wait for approval; corrections need a new readback.
Call record_complaint once with the approved wording. Say its reference and that
the salon replies within two working days; refund requests get a manager callback.
Explain that the resolution remains pending approval. Don't duplicate the record.
Use the knowledge below for guidance. An apology must include the next question.
Stay here for remedies; to_concierge only for abandonment or a separate request.
An upcoming appointment isn't evidence of the date of a past haircut.
Current verification: {{customer_verified}}. Appointment: {{appointment}}.
## Complaint knowledge (same source as the POC)
"""
        + guidance,
    }
    for name, prompt in prompts.items():
        (package / name).write_text(voice + '\n' + prompt)
    handler = ROOT / 'bench/native_tools.py'
    (package / 'tools/salon.py').write_text(handler.read_text())
    for name in ('find_slots', 'save_booking', 'record_complaint', 'find_or_create_customer'):
        path = package / f'tools/{name}.yaml'
        tool = yaml.safe_load(path.read_text())
        if name == 'find_slots':
            tool['description'] = "Read current bookings and availability using the caller's own day words."
            tool['input']['properties']['date']['description'] = (
                'Caller day words, or empty to read existing bookings.'
            )
            tool['input']['properties']['prefer'] = {
                'type': 'string',
                'description': "Time or part of day in the caller's words, such as three or afternoon. For same time, use the saved appointment's time.",
            }
            tool['input']['required'].append('prefer')
            slot_shape = tool['output']['properties']['slots']['items']
            slot_shape['properties'].pop('date')
            slot_shape['required'].remove('date')
        elif name == 'save_booking':
            tool['description'] = (
                'Save an approved action using diary IDs. A clear offered-slot selection or explicit same-time move is approval. Cancellation needs a readback and yes.'
            )
            tool['input']['properties']['confirmed']['description'] = (
                'True after a clear offered-slot selection, explicit same-time move, or approval of a readback.'
            )
        elif name == 'record_complaint':
            tool['description'] = (
                'Record once, after the caller approves the complaint and resolution readback. Preserve that wording exactly; this does not approve the requested remedy.'
            )
            tool['announce'] = "I'll record that complaint."
        else:
            tool['input']['properties']['phone']['description'] = (
                'The approved Spanish number in full international form, +34 followed by nine digits. Never infer country or approval.'
            )
        # The shared service owns return validation/meaning. Keep the native
        # output description, without its old fixture's incompatible status enum.
        if name == 'find_slots':
            tool['output']['properties']['status']['enum'] = ['ok', 'full', 'need_day', 'past', 'closed']
        path.write_text(yaml.safe_dump(tool, sort_keys=False))
    subprocess.run(['unmute', 'compile', str(package), '--target', 'pipecat'], check=True)
    generated = package / 'build/pipecat/bot.py'
    code = generated.read_text()
    # The compiler has no knobs for these POC settings. Apply explicit checked
    # configuration changes to the isolated build; never edit the compiler.
    replacements = {
        'params=SmartTurnParams(stop_secs=1.6)': 'params=SmartTurnParams(stop_secs=1.5)',
        'warm_standby_enabled=True,': 'warm_standby_enabled=True,\n        text_aggregation_mode="token",',
        'user_mute_strategies=[MuteUntilFirstBotCompleteUserMuteStrategy()],': 'user_mute_strategies=[],',
    }
    for old, new in replacements.items():
        assert old in code, f'Native generator changed: {old}'
        code = code.replace(old, new)
    start, end = code.index('def _google_vertex_client('), code.index('class _GoogleVertexLLM(')
    code = (
        code[:start]
        + """def _google_vertex_client(api_key, location):
    from google import genai

    # Match the POC's API-key Express Mode routing; region is not pinned.
    assert location == "global"
    return genai.Client(vertexai=True, api_key=api_key)


"""
        + code[end:]
    )
    generated.write_text(code)
    hashes = {
        str(path.relative_to(package)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(package.rglob('*'))
        if path.is_file()
    }
    manifest = {
        'reference': 'https://github.com/slng-ai/unmute/tree/' + REVISION + '/examples/salon-concierge',
        'revision': REVISION,
        'compiler_target': 'Pipecat 1.10.0 (compiler supports only this version)',
        'execution_runtime': 'shared installed Pipecat 1.11.0; compatibility must pass qualification',
        'build_overrides': {
            'vad_silence_seconds': 0.2,
            'smart_turn_stop_seconds': 1.5,
            'tts_aggregation': 'token',
            'greeting_mute': False,
            'model_transport': 'Vertex Express Mode, region unpinned',
            'tier_header': 'X-Vertex-AI-LLM-Shared-Request-Type: priority',
        },
        'compiler': subprocess.check_output(['unmute', '--version'], text=True).strip(),
        'hashes': hashes,
        'poc_inputs': {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                ROOT / 'salon/diary.py',
                ROOT / 'salon/state.py',
                ROOT / 'knowledge/services.md',
                ROOT / 'knowledge/complaint-guidance.json',
            ]
        },
        'scope': 'Text qualification. Audio settings must be matched by the runner before any audio comparison.',
    }
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return package / 'build/pipecat'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repo', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'bench/raw/native-reference')
    args = parser.parse_args()
    print(prepare(args.repo, args.output))
