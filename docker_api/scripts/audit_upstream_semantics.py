"""Parse SKILL.md and extract Python function inventory via AST.

Usage: python scripts/audit_upstream_semantics.py <SKILL.md path> <output.json>
"""
import ast
import json
import re
import sys


def extract_python_blocks(md_text: str):
    """Extract all ```python ... ``` blocks from markdown."""
    blocks = []
    # Match ```python\n...\n``` pattern
    pattern = re.compile(r'```python\s*\n(.*?)\n```', re.DOTALL)
    for m in pattern.finditer(md_text):
        code = m.group(1)
        blocks.append({
            'code': code,
            'start_line': md_text[:m.start()].count('\n') + 1,
            'end_line': md_text[:m.end()].count('\n') + 1,
        })
    return blocks


def extract_section_context(md_text: str, line_num: int):
    """Find the nearest markdown heading above a given line."""
    lines = md_text.split('\n')[:line_num]
    section = "unknown"
    for line in reversed(lines):
        m = re.match(r'^(#{1,4})\s+(.+)$', line.strip())
        if m:
            section = f"{'#' * len(m.group(1))} {m.group(2)}"
            break
    return section


def analyze_function(node, code_text):
    """Extract semantic info from a FunctionDef/AsyncFunctionDef AST node."""
    sig = ast.unparse(node) if hasattr(ast, 'unparse') else ''

    args = []
    defaults = {}
    for arg in node.args.args:
        args.append(arg.arg)
    # Map defaults (right-aligned)
    num_defaults = len(node.args.defaults)
    num_args = len(node.args.args)
    for i, d in enumerate(node.args.defaults):
        arg_idx = num_args - num_defaults + i
        if arg_idx < len(node.args.args):
            defaults[node.args.args[arg_idx].arg] = ast.unparse(d) if hasattr(ast, 'unparse') else ''

    # Find calls within function
    calls = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name):
                calls.append(child.func.id)
            elif isinstance(child.func, ast.Attribute):
                calls.append(f"{ast.unparse(child.func.value)}.{child.func.attr}" if hasattr(ast, 'unparse') else child.func.attr)

    # Find URLs/domains
    urls = []
    domains = []
    headers = {}
    for child in ast.walk(node):
        if isinstance(child, (ast.Str, ast.Constant)) and isinstance(getattr(child, 'value', ''), str):
            val = str(child.value) if hasattr(child, 'value') else str(child)
            if re.match(r'https?://', val):
                urls.append(val)
                m = re.match(r'https?://([^/:]+)', val)
                if m:
                    domains.append(m.group(1))

    # Find raises
    raises = []
    for child in ast.walk(node):
        if isinstance(child, ast.Raise) and child.exc:
            raises.append(ast.unparse(child.exc) if hasattr(ast, 'unparse') else '')

    return {
        'signature': sig,
        'args': args,
        'defaults': defaults,
        'calls': list(set(calls)),
        'urls': list(set(urls)),
        'domains': list(set(domains)),
        'raises': raises,
    }


def analyze_constants(tree):
    """Extract module-level constants (Name assignments at top level)."""
    constants = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    try:
                        constants[target.id] = ast.unparse(node.value) if hasattr(ast, 'unparse') else ''
                    except Exception:
                        constants[target.id] = '<complex>'
    return constants


def parse_skill_md(md_path: str) -> dict:
    """Parse SKILL.md and return structured inventory."""
    with open(md_path) as f:
        md_text = f.read()

    blocks = extract_python_blocks(md_text)
    functions = []
    all_constants = {}

    for block in blocks:
        section = extract_section_context(md_text, block['start_line'])
        try:
            tree = ast.parse(block['code'])
        except SyntaxError:
            continue

        # Constants at module level
        block_constants = analyze_constants(tree)
        all_constants.update(block_constants)

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                info = analyze_function(node, block['code'])
                functions.append({
                    'name': node.name,
                    'section': section,
                    'block_lines': f"{block['start_line']}-{block['end_line']}",
                    **info,
                })

    return {
        'source_file': md_path,
        'total_blocks': len(blocks),
        'functions': functions,
        'constants': all_constants,
    }


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <SKILL.md> <output.json>")
        sys.exit(1)

    result = parse_skill_md(sys.argv[1])
    with open(sys.argv[2], 'w') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"Found {len(result['functions'])} functions in {result['total_blocks']} code blocks")
    for func in result['functions']:
        print(f"  {func['name']} -> {func['section']}")
