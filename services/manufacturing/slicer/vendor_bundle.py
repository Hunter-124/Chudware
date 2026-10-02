"""Read installed vendor bundles without copying them into Chudware.

Inheritance is scoped to a bundle, left-to-right; flattened preset precedence
is printer, print, filament. Raw INI values (including literal \\n) survive.
Unknown compatibility expressions are reported in ``catalog.warnings`` and use
conservative same-vendor/model fallback rather than executing arbitrary code.
"""
from __future__ import annotations

import configparser
import csv
import difflib
import re
from dataclasses import dataclass
from pathlib import Path

_META = {"inherits", "renamed_from", "alias", "default_print_profile",
         "default_filament_profile", "default_sla_print_profile",
         "default_sla_material_profile", "bed_model", "bed_texture", "name",
         "printer_settings_id", "print_settings_id", "filament_settings_id"}


def _list(value: str) -> list[str]:
    return [x.strip().strip('"') for x in next(csv.reader([value], delimiter=';', escapechar='\\')) if x.strip()]


def _scalar(value: str):
    value = value.strip().strip('"')
    try:
        return float(value)
    except ValueError:
        return value


_TOKEN = re.compile(r'\s*(?:(and|or|not)\b|([A-Za-z_]\w*)|(-?\d+(?:\.\d*)?)|("(?:\\.|[^"\\])*")|(/(?:\\.|[^/\\])*/)|([=!]=|[<>]=?|=~|!~|[()\[\]]))')


class _Condition:
    def __init__(self, text: str, context: dict[str, str]):
        self.tokens = []
        pos = 0
        while pos < len(text.rstrip()):
            match = _TOKEN.match(text, pos)
            if not match:
                raise ValueError("unsupported condition syntax")
            self.tokens.append(next(x for x in match.groups() if x is not None))
            pos = match.end()
        self.i = 0
        self.context = context

    def take(self, expected=None):
        if self.i >= len(self.tokens):
            raise ValueError("incomplete condition")
        token = self.tokens[self.i]
        if expected is not None and token != expected:
            raise ValueError("unexpected condition token")
        self.i += 1
        return token

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def value(self):
        token = self.take()
        if token.startswith('"'):
            return token[1:-1].replace('\\"', '"')
        if token.startswith('/'):
            return token[1:-1]
        if re.fullmatch(r'-?\d+(?:\.\d*)?', token):
            return float(token)
        if token not in self.context:
            raise ValueError("unknown condition key " + token)
        raw = self.context[token]
        if self.peek() == '[':
            self.take('[')
            index = int(self.take())
            self.take(']')
            raw = re.split('[,;]', raw)[index]
        return _scalar(raw)

    def atom(self):
        if self.peek() == 'not':
            self.take()
            return not self.atom()
        if self.peek() == '(':
            self.take()
            result = self.expression()
            self.take(')')
            return result
        left = self.value()
        op = self.take()
        right = self.value()
        if op in ('=~', '!~'):
            hit = re.search(str(right), str(left)) is not None
            return hit if op == '=~' else not hit
        if op == '==': return left == right
        if op == '!=': return left != right
        if op == '<': return left < right
        if op == '>': return left > right
        if op == '<=': return left <= right
        if op == '>=': return left >= right
        raise ValueError("unsupported comparison")

    def conjunction(self):
        result = self.atom()
        while self.peek() == 'and':
            self.take()
            other = self.atom()
            result = result and other
        return result

    def expression(self):
        result = self.conjunction()
        while self.peek() == 'or':
            self.take()
            other = self.conjunction()
            result = result or other
        return result


def evaluate_condition(text: str, context: dict[str, str]) -> bool | None:
    """Return None for unsupported/missing-key conditions; never eval Python."""
    if not text.strip():
        return True
    try:
        parser = _Condition(text, context)
        result = parser.expression()
        if parser.peek() is not None:
            return None
        return bool(result)
    except (ValueError, TypeError, IndexError, re.error, RecursionError):
        return None


@dataclass
class ResolvedProfile:
    vendor: str
    printer: str
    print_: str
    filament: str
    config: dict[str, str]

    def bed_polygon(self) -> list[tuple[float, float]]:
        return [tuple(float(v) for v in point.split('x')) for point in self.config['bed_shape'].split(',')]

    def bed_center(self) -> tuple[float, float]:
        points = self.bed_polygon()
        return tuple((min(p[i] for p in points) + max(p[i] for p in points)) / 2 for i in (0, 1))

    def max_height(self) -> float:
        return float(self.config['max_print_height'])

    def nozzle_mm(self) -> float:
        return float(self.config['nozzle_diameter'].split(',')[0])


class ProfileCatalog:
    def __init__(self, profiles_dir: Path):
        self.warnings: list[str] = []
        self._sections: dict[tuple[str, str, str], dict[str, str]] = {}
        self._cache: dict[tuple[str, str, str], dict[str, str]] = {}
        for path in sorted(Path(profiles_dir).glob('*.ini')):
            parser = configparser.ConfigParser(interpolation=None, strict=False, inline_comment_prefixes=None)
            parser.optionxform = str
            parser.read(path, encoding='utf-8-sig')
            vendor = parser.get('vendor', 'name', fallback=path.stem)
            for section in parser.sections():
                if ':' in section:
                    kind, name = section.split(':', 1)
                    self._sections[vendor, kind, name] = dict(parser[section])

    def vendors(self) -> list[str]:
        return sorted({k[0] for k in self._sections})

    def _resolve(self, key, stack=()):
        if key in self._cache:
            return self._cache[key].copy()
        if key in stack or len(stack) > 100:
            raise ValueError('Preset inheritance cycle: ' + ' -> '.join(k[2] for k in (*stack, key)))
        own = self._sections[key]
        result = {}
        for parent in _list(own.get('inherits', '')):
            result.update(self._resolve((key[0], key[1], parent), (*stack, key)))
        result.update(own)
        self._cache[key] = result
        return result.copy()

    def _key(self, kind, name):
        keys = [k for k in self._sections if k[1:] == (kind, name)]
        if len(keys) != 1:
            raise ValueError(f'Unknown or ambiguous {kind} preset: {name}')
        return keys[0]

    def _normalize(self, text):
        text = re.sub(r'\([^)]*nozzle[^)]*\)', '', text.lower())
        words = re.sub(r'[^a-z0-9]+', ' ', text).split()
        vendors = set(re.sub(r'[^a-z0-9]+', ' ', ' '.join(self.vendors()).lower()).split())
        return ' '.join(w for w in words if w not in vendors)

    def find_printers(self, query: str, nozzle_mm: float | None = None, limit=10) -> list[dict]:
        q = self._normalize(query)
        rows = []
        for key in sorted(self._sections):
            if key[1] != 'printer' or key[2].startswith('*'):
                continue
            cfg = self._resolve(key)
            if cfg.get('printer_technology', 'FFF') != 'FFF':
                continue
            nozzle = float(cfg.get('nozzle_diameter', '0.4').split(',')[0])
            if nozzle_mm is not None and abs(nozzle - nozzle_mm) > 1e-6:
                continue
            name = self._normalize(key[2])
            tokens = set(q.split())
            if q and not tokens.issubset(set(name.split())):
                continue
            score = 100.0 if q == name else 80.0 - 3 * len(set(name.split()) - tokens)
            score += difflib.SequenceMatcher(None, q, name).ratio()
            rows.append(dict(vendor=key[0], name=key[2], score=round(score, 3), nozzle_mm=nozzle,
                             bed_shape=cfg.get('bed_shape'), max_print_height=float(cfg.get('max_print_height', '0'))))
        return sorted(rows, key=lambda r: (-r['score'], abs(r['nozzle_mm'] - 0.4), r['name']))[:limit]

    def _compatible(self, key, printer_key, print_=None):
        cfg = self._resolve(key)
        printer = self._resolve(printer_key)
        explicit = _list(cfg.get('compatible_printers', ''))
        if explicit and printer_key[2] not in explicit:
            aliases = _list(printer.get('renamed_from', ''))
            if not set(aliases).intersection(explicit):
                return False
        contexts = [('compatible_printers_condition', printer)]
        if print_ is not None:
            contexts.append(('compatible_prints_condition', {**printer, **self._resolve(self._key('print', print_))}))
            prints = _list(cfg.get('compatible_prints', ''))
            if prints and print_ not in prints:
                return False
        for name, context in contexts:
            condition = cfg.get(name, '')
            match = evaluate_condition(condition, context)
            if match is False:
                return False
            if match is None:
                warning = f'{key[2]}: unknown {name}: {condition}'
                if warning not in self.warnings:
                    self.warnings.append(warning)
                if key[0] != printer_key[0]:
                    return False
                # Honor recognizable model literals even when the rest is unsupported.
                models = re.findall(r'printer_model\s*==\s*"([^"]+)"', condition)
                if models and str(_scalar(printer.get('printer_model', ''))) not in models:
                    return False
        return True

    def _compatible_names(self, kind, printer, print_=None):
        pk = self._key('printer', printer)
        return sorted(k[2] for k in self._sections if k[1] == kind and not k[2].startswith('*')
                      and k[0] == pk[0] and self._compatible(k, pk, print_))

    def compatible_prints(self, printer: str) -> list[str]:
        return self._compatible_names('print', printer)

    def compatible_filaments(self, printer: str, print_: str | None = None) -> list[str]:
        return self._compatible_names('filament', printer, print_)

    def default_print(self, printer: str, layer_height_mm: float | None = None) -> str:
        names = self.compatible_prints(printer)
        if not names:
            raise ValueError('No compatible print presets for ' + printer)
        cfg = self._resolve(self._key('printer', printer))
        preferred = _list(cfg.get('default_print_profile', ''))
        if layer_height_mm is None:
            for name in preferred:
                if name in names:
                    return name
            layer_height_mm = 0.2
        def score(name):
            height = float(self._resolve(self._key('print', name)).get('layer_height', '0.2'))
            quality = bool(re.search('NORMAL|OPTIMAL|QUALITY', name, re.I))
            return (round(abs(height - layer_height_mm), 8), not quality, name)
        return min(names, key=score)

    def default_filament(self, printer: str, material: str = 'PLA') -> str:
        cfg = self._resolve(self._key('printer', printer))
        names = [n for n in self.compatible_filaments(printer) if
                 str(_scalar(self._resolve(self._key('filament', n)).get('filament_type', ''))).upper() == material.upper()]
        for name in _list(cfg.get('default_filament_profile', '')):
            if name in names:
                return name
        if not names:
            raise ValueError(f'No compatible {material} filament for {printer}')
        return min(names, key=lambda n: ('generic' not in n.lower(), n))

    def resolve(self, printer: str, print_: str | None = None, filament: str | None = None) -> ResolvedProfile:
        pk = self._key('printer', printer)
        print_ = print_ or self.default_print(printer)
        filament = filament or self.default_filament(printer)
        if print_ not in self.compatible_prints(printer) or filament not in self.compatible_filaments(printer, print_):
            raise ValueError('Incompatible printer/print/filament selection')
        config = self._resolve(pk)
        config.update(self._resolve(self._key('print', print_)))
        config.update(self._resolve(self._key('filament', filament)))
        return ResolvedProfile(pk[0], printer, print_, filament, config)


def write_ini(config: dict[str, str], path: Path) -> None:
    """Write only configuration keys; do not unescape vendor G-code strings."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for key, value in sorted(config.items()):
        if key in _META or key.startswith('compatible_'):
            continue
        if not re.fullmatch('[a-zA-Z_][a-zA-Z_0-9]*', key) or '\n' in str(value) or '\r' in str(value):
            raise ValueError(f'Invalid INI key/value: {key}')
        rows.append(f'{key} = {value}\n')
    path.write_text(''.join(rows), encoding='utf-8')
