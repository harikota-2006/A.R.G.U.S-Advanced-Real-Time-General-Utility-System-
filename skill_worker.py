"""Short-lived computation worker. AST policy is a guardrail, not an OS sandbox."""
import builtins
import json
import sys
from types import SimpleNamespace

# Capability facades: never expose real modules (statistics.sys/json.codecs can
# otherwise reach file and process APIs without any underscore attribute).
EXPORTS = {
    'math': 'acos acosh asin asinh atan atan2 atanh ceil comb copysign cos cosh degrees dist e erf erfc exp expm1 fabs factorial floor fmod frexp fsum gamma gcd hypot inf isclose isfinite isinf isnan isqrt lcm ldexp lgamma log log10 log1p log2 modf nan nextafter perm pi pow prod radians remainder sin sinh sqrt tan tanh tau trunc ulp',
    'statistics': 'mean fmean geometric_mean harmonic_mean median median_low median_high median_grouped mode multimode quantiles pstdev pvariance stdev variance covariance correlation linear_regression',
    're': 'compile escape findall finditer fullmatch match search split sub subn ASCII IGNORECASE MULTILINE DOTALL VERBOSE',
    'json': 'dumps loads', 'decimal': 'Decimal', 'fractions': 'Fraction',
    'itertools': 'accumulate chain combinations combinations_with_replacement compress count cycle dropwhile filterfalse groupby islice pairwise permutations product repeat starmap takewhile tee zip_longest',
    'collections': 'Counter defaultdict deque',
}

def main():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
    except ImportError:
        pass
    request = json.loads(sys.stdin.buffer.read(128001))
    allowed = {'math', 'statistics', 're', 'json', 'decimal', 'fractions', 'itertools', 'collections'}
    def checked_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name not in allowed or level:
            raise ImportError('Disallowed import')
        module = builtins.__import__(name, globals, locals, fromlist, level)
        return SimpleNamespace(**{key: getattr(module, key) for key in EXPORTS[name].split() if hasattr(module, key)})
    names = 'abs all any bool dict enumerate filter float int isinstance len list map max min next range repr reversed round set sorted str sum tuple zip Exception ValueError TypeError ZeroDivisionError'.split()
    scope = {'__builtins__': {name: getattr(builtins, name) for name in names}}
    scope['__builtins__']['__import__'] = checked_import
    exec(compile(request['code'], '<custom skill>', 'exec'), scope)
    result = json.dumps(scope['run'](request['params']), allow_nan=False)
    if len(result) > 1024 * 1024:
        raise ValueError('Result exceeds 1 MB')
    sys.stdout.write(result)

if __name__ == '__main__':
    main()
