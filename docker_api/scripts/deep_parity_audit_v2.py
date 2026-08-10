"""Deep semantic parity audit — pure AST, no runtime import needed.

Usage: python scripts/deep_parity_audit_v2.py
"""
import ast
import json
import os
import re

os.chdir('/Users/zhanghuiyuan/Projects/a-stock-data/docker_api')

# Load upstream inventory
with open('audit/generated/upstream_281fc69_inventory.json') as f:
    upstream_inv = json.load(f)

upstream_funcs = {f['name']: f for f in upstream_inv['functions']}


def extract_signature_from_ast(node):
    """Extract arg names and defaults from FunctionDef AST node."""
    args = []
    defaults = {}
    for arg in node.args.args:
        args.append(arg.arg)
    num_defaults = len(node.args.defaults)
    num_args = len(node.args.args)
    for i, d in enumerate(node.args.defaults):
        arg_idx = num_args - num_defaults + i
        if arg_idx < len(node.args.args):
            defaults[node.args.args[arg_idx].arg] = ast.unparse(d)

    return args, defaults


def parse_upstream_signature(sig_str):
    """Parse upstream signature string to extract args and defaults."""
    m = re.search(r'def \w+\((.*?)\)', sig_str)
    if not m:
        return [], {}

    params_str = m.group(1).strip()
    if not params_str:
        return [], {}

    # Simple parser for args
    args = []
    defaults = {}
    # Split by comma, but handle nested parens
    parts = []
    depth = 0
    current = ''
    for ch in params_str:
        if ch in '([{':
            depth += 1
            current += ch
        elif ch in ')]}':
            depth -= 1
            current += ch
        elif ch == ',' and depth == 0:
            parts.append(current.strip())
            current = ''
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())

    for part in parts:
        if '=' in part:
            name, default = part.split('=', 1)
            # Strip type annotation from name
            name = name.strip().split(':')[0].strip()
            if name:
                args.append(name)
                defaults[name] = default.strip()
        else:
            # Remove type annotation
            name = part.split(':')[0].strip()
            if name and name != 'self':
                args.append(name)

    # Handle **kwargs / *args
    for part in parts:
        if '**' in part:
            name = part.strip().replace('*', '').split(':')[0].strip()
            if name and name not in args:
                args.append(name)

    return [a for a in args if a != 'self'], defaults


def scan_local_modules():
    """Scan local upstream modules via AST."""
    import glob

    local = {}
    for fpath in sorted(glob.glob('src/astock_api/upstream/*.py')):
        with open(fpath) as f:
            tree = ast.parse(f.read())
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args, defaults = extract_signature_from_ast(node)
                local[node.name] = {
                    'module': fpath.replace('src/astock_api/upstream/', ''),
                    'args': args,
                    'defaults': defaults,
                }
    return local


def analyze_fallback_chain(node):
    """Count try/except blocks in function."""
    count = 0
    for child in ast.walk(node):
        if isinstance(child, ast.Try):
            count += 1
    return count


def analyze_external_calls(node):
    """Find external request calls."""
    calls = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute):
                calls.append(func.attr)
    return list(set(calls))


def analyze_data_transforms(node):
    """Find data transformation operations."""
    transforms = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute) and func.attr in (
                'rename', 'astype', 'to_numeric', 'sort_values',
                'drop_duplicates', 'fillna', 'reset_index'
            ):
                transforms.append(func.attr)
        if isinstance(child, ast.BinOp) and isinstance(child.op, (ast.Div, ast.Mult)):
            if isinstance(child.right, ast.Constant):
                val = child.right.value
                if val in (100, 1000, 10000, 0.01):
                    op = '/' if isinstance(child.op, ast.Div) else '*'
                    transforms.append(f"{op}{val}")
    return list(set(transforms))


def main():
    local_modules = scan_local_modules()

    # === SIGNATURE PARITY ===
    sig_results = []
    for name, ufunc in sorted(upstream_funcs.items()):
        if name not in local_modules:
            sig_results.append({
                'function': name,
                'status': 'MISSING',
                'reason': 'Not found in local modules',
            })
            continue

        up_args, up_defaults = parse_upstream_signature(ufunc['signature'])
        local_info = local_modules[name]
        local_args = local_info['args']
        local_defaults = local_info['defaults']

        # Compare core args (ignore type annotations)
        up_core = [a for a in up_args if not a.startswith('_')]
        local_core = [a for a in local_args if not a.startswith('_')]

        missing_in_local = set(up_core) - set(local_core)
        extra_in_local = set(local_core) - set(up_core)

        if not missing_in_local and not extra_in_local:
            sig_results.append({
                'function': name,
                'status': 'PASS',
                'upstream_args': up_core,
                'local_args': local_core,
            })
        elif missing_in_local:
            sig_results.append({
                'function': name,
                'status': 'FAIL',
                'reason': f'Missing args: {missing_in_local}',
                'upstream_args': up_core,
                'local_args': local_core,
            })
        elif extra_in_local:
            intentional = {'timeout', 'session', 'headers'}
            if extra_in_local.issubset(intentional):
                sig_results.append({
                    'function': name,
                    'status': 'INTENTIONAL',
                    'reason': f'Extra args (intentional): {extra_in_local}',
                })
            else:
                sig_results.append({
                    'function': name,
                    'status': 'NEEDS_REVIEW',
                    'reason': f'Extra args: {extra_in_local}',
                })

    # === FALLBACK PARITY ===
    fallback_results = []
    for name, ufunc in sorted(upstream_funcs.items()):
        if name not in local_modules:
            continue

        # Get upstream fallback count from inventory calls
        up_calls = ufunc.get('calls', [])
        local_info = local_modules[name]

        # Re-parse local AST for fallback count
        fpath = f"src/astock_api/upstream/{local_info['module']}"
        with open(fpath) as f:
            tree = ast.parse(f.read())

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                local_fallbacks = analyze_fallback_chain(node)
                # Upstream fallback count from calls containing 'except' or try patterns
                up_fallbacks = len([c for c in up_calls if 'except' in str(c).lower()])
                fallback_results.append({
                    'function': name,
                    'upstream_fallbacks': up_fallbacks,
                    'local_fallbacks': local_fallbacks,
                })

    # === ROUTING PARITY (common module) ===
    routing_results = []
    test_codes = ['600519', '000001', '300750', '688981', '510300',
                  'sh000001', 'sz399001', '832975']

    # Parse get_prefix from common.py
    with open('src/astock_api/upstream/common.py') as f:
        tree = ast.parse(f.read())

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'get_prefix':
            # Extract the logic by reading source lines
            pass

    # We'll test routing by importing in a controlled way
    import sys as py_sys
    py_sys.path.insert(0, 'src')

    # Import only common (no mootdx needed for get_prefix)
    try:
        from astock_api.upstream.common import get_prefix, norm_ticker
        for code in test_codes:
            try:
                prefix = get_prefix(code)
                routing_results.append({'code': code, 'prefix': prefix})
            except Exception as e:
                routing_results.append({'code': code, 'prefix': f'ERROR: {e}'})
    except Exception as e:
        routing_results.append({'code': 'N/A', 'prefix': f'Import error: {e}'})

    # === STATISTICS ===
    sig_pass = sum(1 for r in sig_results if r['status'] == 'PASS')
    sig_intentional = sum(1 for r in sig_results if r['status'] == 'INTENTIONAL')
    sig_fail = sum(1 for r in sig_results if r['status'] == 'FAIL')
    sig_review = sum(1 for r in sig_results if r['status'] == 'NEEDS_REVIEW')
    sig_missing = sum(1 for r in sig_results if r['status'] == 'MISSING')

    report = {
        'signature_parity': sig_results,
        'fallback_parity': fallback_results,
        'routing_parity': routing_results,
        'statistics': {
            'signature_pass': sig_pass,
            'signature_intentional': sig_intentional,
            'signature_fail': sig_fail,
            'signature_review': sig_review,
            'signature_missing': sig_missing,
        },
    }

    with open('audit/deep_parity_results.json', 'w') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print("=== SIGNATURE PARITY ===")
    print(f"PASS: {sig_pass}")
    print(f"INTENTIONAL: {sig_intentional}")
    print(f"FAIL: {sig_fail}")
    print(f"REVIEW: {sig_review}")
    print(f"MISSING: {sig_missing}")

    fails = [r for r in sig_results if r['status'] == 'FAIL']
    if fails:
        print("\n=== SIGNATURE FAILURES ===")
        for r in fails:
            print(f"  {r['function']}: {r.get('reason', '')}")

    review = [r for r in sig_results if r['status'] == 'NEEDS_REVIEW']
    if review:
        print("\n=== NEEDS REVIEW ===")
        for r in review:
            print(f"  {r['function']}: {r.get('reason', '')}")

    missing = [r for r in sig_results if r['status'] == 'MISSING']
    if missing:
        print(f"\n=== MISSING ({len(missing)}) ===")
        for r in missing:
            print(f"  {r['function']}")

    print(f"\n=== FALLBACK ANALYSIS ===")
    for r in fallback_results:
        if r['local_fallbacks'] > 0 or r['function'] in ('tdx_client',):
            print(f"  {r['function']}: upstream={r['upstream_fallbacks']} local={r['local_fallbacks']}")

    print(f"\n=== ROUTING ===")
    for r in routing_results:
        print(f"  {r['code']} -> {r['prefix']}")


if __name__ == '__main__':
    main()
