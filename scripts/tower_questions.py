"""Only expose user-facing fields of known question tools, never raw arguments."""
import json


def question_text(name, arguments):
    name = str(name).rsplit('.', 1)[-1].lower()
    if name not in ('request_user_input', 'request_user_input_async', 'ask_user_question', 'askuserquestion'):
        return ''
    try:
        value = json.loads(arguments) if isinstance(arguments, str) else arguments
    except (ValueError, TypeError):
        return ''
    if not isinstance(value, dict) or not isinstance(value.get('questions'), list):
        return ''
    lines = ['❓ Agent 正在向你提问']
    for question in value['questions'][:3]:
        if not isinstance(question, dict):
            continue
        title = question.get('question') or question.get('title')
        if not isinstance(title, str) or not title.strip():
            continue
        lines.append(title[:2000])
        options = question.get('options') or []
        if not isinstance(options, list):
            continue
        for index, option in enumerate(options[:10], 1):
            label = option if isinstance(option, str) else option.get('label') if isinstance(option, dict) else None
            if isinstance(label, str):
                lines.append(f'{index}. {label[:300]}')
    return '\n\n'.join(lines) if len(lines) > 1 else ''
