"""
analyzer.py — вызов LLM (Ollama или API) для анализа тендера.
"""

import json
import re
from pathlib import Path


class APICallError(Exception):
    """Любая ошибка на стороне API или Ollama."""


def analyze(text: str, config: dict, mode: str) -> dict:
    """
    Анализировать текст тендера через LLM.
    mode: 'api' | 'ollama'
    Возвращает dict с полями из config['fields'].
    Значения — строки для обычных полей, list[dict] для полей с type:list.
    """
    max_tokens = config.get('max_chunk_tokens', 20000)
    chunks = _split_text(text, max_tokens)

    results = [_analyze_chunk(chunk, config, mode) for chunk in chunks]

    if len(results) == 1:
        return results[0]
    return _merge_results(results, config)


# ---------------------------------------------------------------------------
# Разбивка на части
# ---------------------------------------------------------------------------

def _split_text(text: str, max_tokens: int) -> list:
    """Русский текст: ~3 символа на токен. Режем по границам документов (=== ... ===)."""
    max_chars = max_tokens * 3
    if len(text) <= max_chars:
        return [text]

    parts = re.split(r'(?=^=== .+ ===$)', text, flags=re.MULTILINE)

    chunks = []
    current = ''
    for part in parts:
        if current and len(current) + len(part) > max_chars:
            chunks.append(current)
            current = part
        else:
            current += part
    if current:
        chunks.append(current)

    return chunks if chunks else [text]


# ---------------------------------------------------------------------------
# Анализ одного чанка
# ---------------------------------------------------------------------------

def _analyze_chunk(text: str, config: dict, mode: str) -> dict:
    prompt_path = Path(__file__).parent / 'prompts' / 'analyze_tender.txt'
    with open(prompt_path, 'r', encoding='utf-8') as f:
        template = f.read()

    # /no_think для Qwen3 — отключает thinking-режим, стабилизирует JSON-вывод
    if mode == 'ollama' and 'qwen3' in config.get('ollama', {}).get('model', '').lower():
        if not template.startswith('/no_think'):
            template = '/no_think\n' + template

    prompt = template.replace('{fields_schema}', _build_fields_schema(config)) + '\n' + text

    if mode == 'ollama':
        raw = _call_ollama(prompt, config)
    else:
        raw = _call_api(prompt, config)

    return _parse_response(raw, config)


def _build_fields_schema(config: dict) -> str:
    """Сгенерировать описание полей + JSON-шаблон для промпта из списка полей в конфиге."""
    fields = config.get('fields', [])

    desc_lines = []
    for f in fields:
        desc = (f.get('description') or '').strip()
        if desc:
            desc_lines.append(f'- {f["name"]}: {desc}')

    json_lines = ['{']
    for i, f in enumerate(fields):
        comma = ',' if i < len(fields) - 1 else ''
        if f.get('type') == 'list':
            item_keys = [item['key'] for item in f.get('item_schema', [])]
            item_obj = '{' + ', '.join(f'"{k}": ""' for k in item_keys) + '}'
            json_lines.append(f'  "{f["name"]}": [{item_obj}]{comma}')
        else:
            json_lines.append(f'  "{f["name"]}": ""{comma}')
    json_lines.append('}')

    if desc_lines:
        return 'Описание полей:\n' + '\n'.join(desc_lines) + '\n\nJSON-шаблон:\n' + '\n'.join(json_lines)
    return '\n'.join(json_lines)


# ---------------------------------------------------------------------------
# Вызов моделей
# ---------------------------------------------------------------------------

def _call_ollama(prompt: str, config: dict) -> str:
    import requests
    ollama = config['ollama']
    try:
        response = requests.post(
            f"{ollama.get('base_url', 'http://localhost:11434')}/api/generate",
            json={
                'model': ollama['model'],
                'prompt': prompt,
                'format': 'json',
                'stream': False,
                'options': {'temperature': 0},
            },
            timeout=ollama.get('timeout', 300),
        )
        response.raise_for_status()
        return response.json().get('response', '')
    except Exception as e:
        raise APICallError(str(e)) from e


def _call_api(prompt: str, config: dict) -> str:
    import os
    from openai import OpenAI

    api_cfg = config['api']
    client = OpenAI(
        api_key=os.environ.get('OPENAI_API_KEY', ''),
        base_url=api_cfg.get('base_url', 'https://api.openai.com/v1'),
    )

    try:
        is_openrouter = 'openrouter.ai' in api_cfg.get('base_url', '')
        extra = {"reasoning": {"exclude": True}} if is_openrouter else {}
        response = client.chat.completions.create(
            model=api_cfg['model'],
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0,
            extra_body=extra if extra else None,
        )
        choices = response.choices
        if not choices or choices[0] is None:
            return ''
        return choices[0].message.content or ''
    except Exception as e:
        raise APICallError(str(e)) from e


# ---------------------------------------------------------------------------
# Парсинг ответа
# ---------------------------------------------------------------------------

def _parse_response(raw: str, config: dict) -> dict:
    raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()

    try:
        return _validate(json.loads(raw), config)
    except (json.JSONDecodeError, AttributeError):
        pass

    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        try:
            return _validate(json.loads(match.group()), config)
        except json.JSONDecodeError:
            pass

    return _empty_result(config)


def _empty_result(config: dict) -> dict:
    result = {}
    for f in config.get('fields', []):
        result[f['name']] = [] if f.get('type') == 'list' else ''
    return result


def _validate(data: dict, config: dict) -> dict:
    result = {}
    for f in config.get('fields', []):
        name = f['name']
        if f.get('type') == 'list':
            result[name] = _validate_list(data.get(name), f)
        else:
            result[name] = str(data.get(name, '') or '').strip()
    return result


def _validate_list(value, field: dict) -> list:
    """Привести значение list-поля к списку объектов с подполями из item_schema.
    Подполе с numeric:true приводится к числу (int/float) или к '' если не парсится."""
    if not isinstance(value, list):
        return []
    schema = field.get('item_schema', [])
    cleaned = []
    for elem in value:
        if not isinstance(elem, dict):
            continue
        obj = {}
        for sub in schema:
            raw = elem.get(sub['key'], '')
            obj[sub['key']] = _to_number(raw) if sub.get('numeric') else _stringify(raw)
        if any(v != '' for v in obj.values()):
            cleaned.append(obj)
    return cleaned


def _stringify(value) -> str:
    """Привести значение к строке без лишних пробелов."""
    if value is None or isinstance(value, bool):
        return ''
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    return str(value).strip()


def _to_number(value):
    """Привести значение к числу. Возвращает int (если целое), float или '' если не парсится."""
    if isinstance(value, bool):
        return ''
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if not isinstance(value, str):
        return ''
    # очистка: убираем пробелы, ₽, руб., запятые → точки
    s = value.strip().replace('₽', '').replace('руб.', '').replace('руб', '')
    s = s.replace('\xa0', '').replace(' ', '').replace(',', '.')
    if not s:
        return ''
    try:
        n = float(s)
    except ValueError:
        return ''
    return int(n) if n.is_integer() else n


# ---------------------------------------------------------------------------
# Слияние результатов по чанкам
# ---------------------------------------------------------------------------

def _merge_results(results: list, config: dict) -> dict:
    """Для list-полей — конкатенация массивов из всех чанков.
    Для строковых полей — первое непустое значение."""
    merged = {}
    for f in config.get('fields', []):
        name = f['name']
        if f.get('type') == 'list':
            combined = []
            for r in results:
                combined.extend(r.get(name) or [])
            merged[name] = combined
        else:
            merged[name] = next((r[name] for r in results if r.get(name)), '')
    return merged
