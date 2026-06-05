"""
CASI — LLM Diagnostic Layer
Uses Anthropic Claude API for root-cause analysis when CASI < 700.
Falls back to Ollama if ANTHROPIC_API_KEY is not set.
"""

import os
import json
import logging
import requests

log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
OLLAMA_URL = 'http://localhost:11434/api/generate'
OLLAMA_MODEL = os.environ.get('CASI_LLM_MODEL', 'llama3')

PROMPT_TEMPLATE = """\
You are a senior engineering maturity advisor. A CASI engineering maturity score has dropped below 700.

Dataset context:
- {n_fail} test cases currently failing across {n_modules} modules
- Component C (Downtime): {C_raw} total failing-days accumulated
- Component B (Avg Fix Time): {B_raw} days mean
- Top failing tests:
{top_failures_list}
- Sprint: {sprint_start} to {sprint_end}

Respond in this exact JSON format only (no markdown, no extra text):
{{
  "root_cause": "2-3 sentence plain English explanation of the most likely root cause",
  "components_implicated": ["C", "B"],
  "actions": [
    {{"title": "action description", "priority": "Critical|High|Medium", "effort": "High|Medium|Low"}},
    {{"title": "action description", "priority": "Critical|High|Medium", "effort": "High|Medium|Low"}},
    {{"title": "action description", "priority": "Critical|High|Medium", "effort": "High|Medium|Low"}}
  ]
}}"""

FALLBACK = {
    'root_cause': (
        'No LLM service configured — manual analysis required. '
        'Review the longest-running failures first, as accumulated downtime '
        'is the primary driver of the score degradation.'
    ),
    'components_implicated': ['C', 'B'],
    'actions': [
        {'title': 'Triage all currently failing test cases by days open', 'priority': 'Critical', 'effort': 'Medium'},
        {'title': 'Assign owners to failures open longer than 14 days', 'priority': 'High', 'effort': 'Low'},
        {'title': 'Set ANTHROPIC_API_KEY env var to enable AI diagnostics', 'priority': 'Medium', 'effort': 'Low'},
    ],
}


def _build_prompt(result):
    dataset = result.get('dataset', {})
    components = result.get('components', {})
    sprint_history = result.get('sprint_history', [])
    open_failures = result.get('open_failures', [])

    last_sprint = sprint_history[-1] if sprint_history else {}
    sprint_start = last_sprint.get('sprint_start', 'Unknown')
    sprint_end = last_sprint.get('sprint_end', 'Unknown')
    n_fail = last_sprint.get('n_fail', len(open_failures))

    top_failures = open_failures[:5]
    top_list = '\n'.join(
        f"  - {f['tc_id']}: {f['name']} ({f['priority']}, {f['days_open']} days open)"
        for f in top_failures
    ) or '  None'

    return PROMPT_TEMPLATE.format(
        n_fail=n_fail,
        n_modules=len(dataset.get('modules', [])),
        C_raw=components.get('C', {}).get('raw', 0),
        B_raw=components.get('B', {}).get('raw', 0),
        top_failures_list=top_list,
        sprint_start=sprint_start,
        sprint_end=sprint_end,
    )


def _parse_response(text):
    """Extract JSON from LLM response."""
    text = text.strip()
    # Strip markdown code fences if present
    if '```' in text:
        text = text.split('```')[1]
        if text.startswith('json'):
            text = text[4:]
    parsed = json.loads(text.strip())
    if 'root_cause' in parsed and 'actions' in parsed:
        return parsed
    return None


MODEL = 'claude-haiku-4-5-20251001'


def _call_claude(prompt):
    """Call Claude and return (parsed_dict_or_None, input_tokens, output_tokens)."""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{'role': 'user', 'content': prompt}],
    )
    raw = msg.content[0].text
    log.debug('Claude raw response: %s', raw[:200])
    usage = msg.usage
    return _parse_response(raw), getattr(usage, 'input_tokens', 0), getattr(usage, 'output_tokens', 0)


def _call_ollama(prompt):
    resp = requests.post(
        OLLAMA_URL,
        json={'model': OLLAMA_MODEL, 'prompt': prompt, 'stream': False, 'format': 'json'},
        timeout=90,
    )
    resp.raise_for_status()
    return _parse_response(resp.json().get('response', '{}'))


def get_diagnostic(result, sprint=None, module=None, user_email=None, project_id=None, conn=None):
    prompt = _build_prompt(result)

    # Try Claude API first (if key set) — do NOT fall to Ollama if key is configured
    if ANTHROPIC_API_KEY:
        try:
            parsed, in_tok, out_tok = _call_claude(prompt)
            # Log token usage if we have a db connection and user context
            if conn and user_email:
                try:
                    import db as _db
                    _db.log_ai_usage(
                        conn,
                        user_email   = user_email,
                        project_id   = project_id,
                        source       = 'diagnostic',
                        input_tokens = in_tok,
                        output_tokens= out_tok,
                        model        = MODEL,
                    )
                except Exception:
                    pass
            if parsed:
                return parsed
            log.warning('Claude returned unparseable response, using fallback')
        except Exception as e:
            log.error('Claude API error: %s', e)
        return FALLBACK

    # No Claude key — try Ollama
    try:
        parsed = _call_ollama(prompt)
        if parsed:
            return parsed
    except Exception as e:
        log.error('Ollama error: %s', e)

    return FALLBACK
