"""Deep semantic parity audit - signature, request, fallback, transform, routing.

Usage: python scripts/deep_parity_audit.py
"""
import ast
import inspect
import json
import os
import sys
from typing import Any

os.chdir('/Users/zhanghuiyuan/Projects/a-stock-data/docker_api')
sys.path.insert(0, 'src')

# Load upstream inventory
with open('audit/generated/upstream_281fc69_inventory.json') as f:
    upstream_inv = json.load(f)

upstream_funcs = {f['name']: f for f in upstream_inv['functions']}


def compare_signatures(upstream_sig: str, local_func) -> dict:
    """Compare upstream signature string with local function."""
    try:
        local_sig = inspect.signature(local_func)
        # Parse upstream signature args from the unparse string
        # Extract arg names and defaults
        local_args = list(local_sig.parameters.keys())

        # Parse upstream args from signature string
        # e.g. "def tdx_client(market='std'):" -> ['market']
        import re
        m = re.search(r'def \w+\((.*?)\)', upstream_sig)
        if not m:
            return {'status': 'NEEDS_REVIEW', 'reason': 'Cannot parse upstream signature'}

        up_params = m.group(1).strip()
        if not up_params:
            up_args = []
        else:
            # Split by comma, handling defaults
            up_args = [p.strip().split('=')[0].split(':')[0].strip() for p in up_params.split(',')]

        # Remove 'self' if present
        up_args = [a for a in up_args if a != 'self']
        local_args = [a for a in local_args if a != 'self']

        # Check core args match
        if up_args == local_args:
            return {'status': 'PASS', 'upstream_args': up_args, 'local_args': local_args}

        # Check if local has subset (missing args)
        missing_in_local = set(up_args) - set(local_args)
        extra_in_local = set(local_args) - set(up_args)

        if missing_in_local:
            return {
                'status': 'FAIL',
                'reason': f'Missing args in local: {missing_in_local}',
                'upstream_args': up_args,
                'local_args': local_args,
            }

        if extra_in_local:
            # Check if extras are intentional (e.g., timeout, session)
            intentional = {'timeout', 'session', 'headers'}
            if extra_in_local.issubset(intentional):
                return {
                    'status': 'INTENTIONAL',
                    'reason': f'Extra args in local (intentional): {extra_in_local}',
                    'upstream_args': up_args,
                    'local_args': local_args,
                }
            return {
                'status': 'NEEDS_REVIEW',
                'reason': f'Extra args in local: {extra_in_local}',
                'upstream_args': up_args,
                'local_args': local_args,
            }

        return {'status': 'PASS', 'upstream_args': up_args, 'local_args': local_args}
    except Exception as e:
        return {'status': 'NEEDS_REVIEW', 'reason': str(e)}


def scan_local_functions():
    """Scan local upstream modules and build function map."""
    import glob

    local = {}
    for fpath in sorted(glob.glob('src/astock_api/upstream/*.py')):
        with open(fpath) as f:
            tree = ast.parse(f.read())
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local[node.name] = {
                    'module': fpath.replace('src/astock_api/upstream/', ''),
                    'node': node,
                }
    return local


def analyze_fallback_chain(node) -> list:
    """Analyze try/except fallback chains in a function."""
    chain = []
    for child in ast.walk(node):
        if isinstance(child, ast.Try):
            # Count except handlers
            handlers = len(child.handlers)
            chain.append({
                'type': 'try_except',
                'handlers': handlers,
            })
    return chain


def analyze_external_calls(node) -> list:
    """Find external request calls in function body."""
    calls = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute):
                # e.g., requests.get, session.post, client.bars
                if isinstance(func.value, ast.Name):
                    calls.append(f"{func.value.id}.{func.attr}")
                elif isinstance(func.value, ast.Attribute):
                    calls.append(f"{func.attr}")
            elif isinstance(func, ast.Name):
                if func.id in ('requests', 'urlopen', 'create_connection'):
                    calls.append(func.id)
    return list(set(calls))


def analyze_data_transforms(node) -> list:
    """Find data transformation operations."""
    transforms = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute):
                attr = func.attr
                if attr in ('rename', 'astype', 'to_numeric', 'sort_values',
                            'drop_duplicates', 'fillna', 'reset_index',
                            'set_index', 'rename_axis'):
                    transforms.append(attr)
            elif isinstance(func, ast.Name):
                if func.id in ('pd.to_numeric', 'float', 'int', 'str'):
                    transforms.append(func.id)
        # Check for division/multiplication constants
        if isinstance(child, ast.BinOp) and isinstance(child.op, (ast.Div, ast.Mult)):
            if isinstance(child.right, ast.Constant):
                val = child.right.value
                if val in (100, 1000, 10000, 0.01):
                    transforms.append(f"{'/' if isinstance(child.op, ast.Div) else '*'}{val}")
    return transforms


def main():
    local_modules = scan_local_functions()

    # Import local modules for runtime inspection
    import importlib
    imported = {}
    for mod_name in ['tencent', 'eastmoney', 'tdx', 'cninfo', 'sina',
                     'cls', 'other', 'ths', 'baidu']:
        try:
            mod = importlib.import_module(f'astock_api.upstream.{mod_name}')
            imported[mod_name] = mod
        except Exception:
            pass

    # Also import common
    try:
        imported['common'] = importlib.import_module('astock_api.upstream.common')
    except Exception:
        pass

    # === SIGNATURE PARITY ===
    sig_results = []
    for name, ufunc in sorted(upstream_funcs.items()):
        # Find local function
        local_func = None
        for mod_name, mod in imported.items():
            if hasattr(mod, name):
                local_func = getattr(mod, name)
                break

        if local_func is None:
            sig_results.append({
                'function': name,
                'status': 'MISSING',
                'reason': 'Not found in local modules',
            })
            continue

        result = compare_signatures(ufunc['signature'], local_func)
        sig_results.append({
            'function': name,
            **result,
        })

    # === FALLBACK PARITY ===
    fallback_results = []
    for name, ufunc in sorted(upstream_funcs.items()):
        # Find local AST node
        local_node = None
        for mod_info in local_modules.values():
            if mod_info['node'].name == name:
                local_node = mod_info['node']
                break

        if local_node is None:
            continue

        up_chain = analyze_fallback_chain(
            ast.parse(ufunc.get('signature', '')) if ufunc.get('signature') else ast.Module(body=[], type_ignores=[])
        )
        local_chain = analyze_fallback_chain(local_node)

        fallback_results.append({
            'function': name,
            'upstream_fallbacks': len(up_chain),
            'local_fallbacks': len(local_chain),
        })

    # === EXTERNAL CALLS PARITY ===
    request_results = []
    for name, ufunc in sorted(upstream_funcs.items()):
        local_node = None
        for mod_info in local_modules.values():
            if mod_info['node'].name == name:
                local_node = mod_info['node']
                break

        if local_node is None:
            continue

        up_calls = ufunc.get('calls', [])
        local_calls = analyze_external_calls(local_node)

        request_results.append({
            'function': name,
            'upstream_calls': up_calls[:5],  # First 5
            'local_calls': local_calls[:5],
        })

    # === DATA TRANSFORM PARITY ===
    transform_results = []
    for name, ufunc in sorted(upstream_funcs.items()):
        local_node = None
        for mod_info in local_modules.values():
            if mod_info['node'].name == name:
                local_node = mod_info['node']
                break

        if local_node is None:
            continue

        transforms = analyze_data_transforms(local_node)
        if transforms:
            transform_results.append({
                'function': name,
                'transforms': transforms,
            })

    # === ROUTING PARITY (common module) ===
    routing_results = []
    test_codes = ['600519', '000001', '300750', '688981', '510300',
                  'sh000001', 'sz399001', '832975']

    if 'common' in imported:
        common = imported['common']
        for code in test_codes:
            try:
                prefix = common.get_prefix(code)
                routing_results.append({
                    'code': code,
                    'prefix': prefix,
                })
            except Exception as e:
                routing_results.append({
                    'code': code,
                    'prefix': f'ERROR: {e}',
                })

    # === STATISTICS ===
    sig_pass = sum(1 for r in sig_results if r['status'] == 'PASS')
    sig_intentional = sum(1 for r in sig_results if r['status'] == 'INTENTIONAL')
    sig_fail = sum(1 for r in sig_results if r['status'] == 'FAIL')
    sig_review = sum(1 for r in sig_results if r['status'] == 'NEEDS_REVIEW')
    sig_missing = sum(1 for r in sig_results if r['status'] == 'MISSING')

    # Write results
    report = {
        'signature_parity': sig_results,
        'fallback_parity': fallback_results,
        'request_parity': request_results,
        'transform_parity': transform_results,
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

    # Print failures
    fails = [r for r in sig_results if r['status'] in ('FAIL', 'NEEDS_REVIEW')]
    if fails:
        print("\n=== SIGNATURE ISSUES ===")
        for r in fails:
            print(f"  {r['function']}: {r.get('reason', r.get('status'))}")

    print(f"\n=== FALLBACK ANALYSIS ===")
    for r in fallback_results:
        if r['local_fallbacks'] > 0 or r['function'] in ('tdx_client',):
            print(f"  {r['function']}: upstream={r['upstream_fallbacks']} local={r['local_fallbacks']}")

    print(f"\n=== ROUTING ===")
    for r in routing_results:
        print(f"  {r['code']} -> {r['prefix']}")

    print(f"\n=== TRANSFORMS ===")
    for r in transform_results[:10]:
        print(f"  {r['function']}: {r['transforms']}")


if __name__ == '__main__':
    main()
