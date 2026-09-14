"""Localized error catalogs (``translations/errors.<lang>.json``) + provenance gate.

i18n-shipping.md §5. The en canon lives in ``errors.py``
(``register_service_errors``); each target language ships as a flat
``translations/errors.<lang>.json`` catalog with a shared
``translations/.state.json`` provenance sidecar, and
:func:`check_translation_catalogs` gates coverage, staleness, params and
byte-stability.

Until this file existed, ``generate_error_keys`` printed
``[warning:unshipped] 'stapel_alerts' owns 6 declared code(s) but ships no
errors catalog in any language`` on every ``make contract``. That warning is
not cosmetic: it means a translated deployment renders this module's refusals
in English next to everything else's Russian, and the person reading a 403 at
3am has to notice that the mismatch is the library and not the bug.

Provenance, honest: the builtin ``stapel-translate`` corpus carries none of
these six keys (they are this module's own), so every value here is a
**machine translation** recorded in :data:`_MACHINE` and written with
``origin: llm`` — the gate's unreviewed (W) counter, which is what it is until
a native reader signs off. Two of the six carry params (``{max}``,
``{status}``) and the placeholder gate enforces them verbatim.

Adding a language is a three-line change: append the tag to
:data:`LANGUAGES`, add its ``_MACHINE_<TAG>`` table, and regenerate.

Regenerate after adding or changing an error key or a translation:

    STAPEL_REGEN_ERROR_I18N=1 python -m pytest tests/test_error_i18n.py::test_regen

then commit ``translations/errors.<lang>.json`` + ``translations/.state.json``
+ ``docs/errors.<lang>.md``. Without the env var the same module is the gate.
"""
import io
import os
from pathlib import Path

from django.core.management import call_command

from stapel_core.i18n import (
    check_translation_catalogs,
    source_texts,
    summarize,
    translate_catalog,
)
from stapel_core.i18n.catalogs import load_catalog_file

REPO = Path(__file__).resolve().parent.parent
TRANSLATIONS = REPO / "translations"
DOCS = REPO / "docs"
#: Languages this module ships error catalogs in. en is the canon (the
#: registry literals); every other tag needs a catalog + a docs page.
LANGUAGES = ["en", "ru", "es"]
#: The languages that need a catalog — everything but the source language.
TARGET_LANGUAGES = [lang for lang in LANGUAGES if lang != "en"]

#: stapel-translate builtin fixtures (the curated seed corpus). Overridable for
#: an out-of-tree checkout via STAPEL_TRANSLATE_FIXTURES. It carries none of
#: this module's keys today; the lookup stays so that a key which later enters
#: the shared corpus is seeded from it rather than machine-translated again.
_FIXTURES = Path(
    os.environ.get(
        "STAPEL_TRANSLATE_FIXTURES",
        REPO.parent / "stapel-translate" / "fixtures" / "builtin",
    )
)

_MACHINE_RU = {
    "error.400.alerts_invalid_report":
        "Тело отчёта имеет неверный формат",
    "error.400.alerts_batch_too_large":
        "Слишком много событий в одном отчёте (максимум {max})",
    "error.400.alerts_status_not_settable":
        "Статус {status} устанавливается хранилищем по фактам и не может быть "
        "назначен вручную",
    "error.401.alerts_service_key_required":
        "Требуется сервисный ключ или сессия сотрудника",
    "error.403.alerts_service_key_invalid":
        "Этот сервисный ключ недействителен",
    "error.404.alerts_issue_not_found":
        "Проблема не найдена",
}

_MACHINE_ES = {
    "error.400.alerts_invalid_report":
        "El contenido del informe tiene un formato no válido",
    "error.400.alerts_batch_too_large":
        "Demasiados eventos en un solo informe (máximo {max})",
    "error.400.alerts_status_not_settable":
        "El estado {status} lo establece el almacén a partir de las pruebas y "
        "no se puede asignar",
    "error.401.alerts_service_key_required":
        "Se requiere una clave de servicio o una sesión de personal",
    "error.403.alerts_service_key_invalid":
        "Esta clave de servicio no es válida",
    "error.404.alerts_issue_not_found":
        "Incidencia no encontrada",
}

#: language -> machine-translation table. Values land as ``origin: llm``.
_MACHINE = {"ru": _MACHINE_RU, "es": _MACHINE_ES}


class _DictTranslator:
    """Offline translator seam — returns fixed machine translations by key."""

    def __init__(self, table):
        self._table = table

    def translate(self, entries, source_language, target_language):
        return {k: self._table[k] for k in entries if k in self._table}


def _seed_from_fixtures(lang: str) -> dict[str, str]:
    """Flat ``{error.*: text}`` seed from the builtin fixtures for *lang*."""
    import json

    path = _FIXTURES / f"{lang}.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and k.startswith("error.")
        and isinstance(v, str) and v
    }


def _regen(lang: str):
    """Materialize one target-language catalog from corpus + machine map."""
    return translate_catalog(
        "errors", lang, TRANSLATIONS,
        source_texts=source_texts("errors"),
        seed=_seed_from_fixtures(lang),
        seed_label="stapel-builtin",
        llm=True,
        translator=_DictTranslator(_MACHINE.get(lang, {})),
    )


def test_regen():
    """Regenerate (env-gated) or assert every catalog is a no-op regen (drift)."""
    if os.environ.get("STAPEL_REGEN_ERROR_I18N"):
        for lang in TARGET_LANGUAGES:
            result = _regen(lang)
            assert not result.missing, f"{lang}: still missing: {result.missing}"
        for lang in LANGUAGES:
            call_command("generate_error_docs", "--lang", lang,
                         "--out", str(DOCS), "--translations", str(TRANSLATIONS),
                         stdout=io.StringIO())
        return

    for lang in TARGET_LANGUAGES:
        path = TRANSLATIONS / f"errors.{lang}.json"
        before = path.read_bytes()
        _regen(lang)
        assert path.read_bytes() == before, (
            f"errors.{lang}.json drifted — run "
            f"STAPEL_REGEN_ERROR_I18N=1 pytest tests/test_error_i18n.py::test_regen"
        )


def test_catalog_gate_green():
    """E: missing / stale / params-mismatch / not-byte-stable — all zero."""
    issues = check_translation_catalogs(
        "errors", TRANSLATIONS,
        source_texts=source_texts("errors"),
        languages=LANGUAGES,
    )
    errors, _warnings = summarize(issues)
    blocking = [i for i in issues if i.level == "error"]
    assert not blocking, "\n".join(f"[{i.code}] {i.message}" for i in blocking)
    assert errors == 0


def test_every_language_covers_every_key_this_module_owns():
    """Coverage is scoped to OWNERSHIP.

    Core ships its own catalogs and the loader merges the owner's, so a module
    that also translated core's keys would be maintaining a second, drifting
    copy — the gate calls that ``foreign`` and fails on it. What this module
    answers for is the six keys it owns, in every target language.
    """
    from stapel_core.i18n import owned_keys, owner_of_dir, source_owners

    source = owned_keys(
        source_texts("errors"),
        source_owners("errors"),
        owner_of_dir(TRANSLATIONS),
    )
    assert source, "no owned keys resolved — the ownership lookup is not seeing errors.py"
    for lang in TARGET_LANGUAGES:
        catalog = load_catalog_file(TRANSLATIONS / f"errors.{lang}.json")
        missing = [k for k in source if k not in catalog]
        assert not missing, (
            f"{lang} catalog missing {len(missing)} key(s): {missing[:8]}"
        )


def test_every_key_errors_py_declares_is_owned_and_translated():
    """The six keys, named, so a seventh cannot be added and left English.

    ``test_every_language_covers_every_key_this_module_owns`` asks the registry
    what this module owns; if ownership resolution ever broke it would ask
    about an empty set and pass. This asks the source of truth — the module's
    own constant — instead.
    """
    from stapel_alerts.errors import STAPEL_ALERTS_ERRORS

    for lang in TARGET_LANGUAGES:
        catalog = load_catalog_file(TRANSLATIONS / f"errors.{lang}.json")
        missing = sorted(set(STAPEL_ALERTS_ERRORS) - set(catalog))
        assert not missing, f"{lang} does not translate: {missing}"


def test_translations_preserve_placeholders():
    """Every localized text keeps exactly the canon's ``{param}`` slots.

    ``{max}`` and ``{status}`` are formatted into the response by core. A
    translation that dropped one would render a message with a hole in it; one
    that renamed it would raise KeyError inside the error path, which is the
    one place a second failure is least welcome.
    """
    from stapel_core.i18n.domains import params_of

    source = source_texts("errors")
    for lang in TARGET_LANGUAGES:
        catalog = load_catalog_file(TRANSLATIONS / f"errors.{lang}.json")
        for key, text in catalog.items():
            if key in source:
                assert set(params_of(text)) == set(params_of(source[key])), \
                    f"{lang}: {key}"


def test_the_two_parameterised_keys_really_carry_their_params():
    """Guard on the guard above: if ``params_of`` ever returned nothing for
    everything, the placeholder test would pass over empty sets."""
    from stapel_core.i18n.domains import params_of

    source = source_texts("errors")
    assert set(params_of(source["error.400.alerts_batch_too_large"])) == {"max"}
    assert set(params_of(source["error.400.alerts_status_not_settable"])) == {"status"}


def test_error_reference_matches_a_fresh_regeneration(tmp_path):
    """The committed reference is what the generator produces TODAY."""
    for lang in LANGUAGES:
        call_command("generate_error_docs", "--lang", lang, "--out", str(tmp_path),
                     "--translations", str(TRANSLATIONS), stdout=io.StringIO())
        assert (tmp_path / f"errors.{lang}.md").read_bytes() == \
            (DOCS / f"errors.{lang}.md").read_bytes(), (
                f"docs/errors.{lang}.md is stale — run "
                f"STAPEL_REGEN_ERROR_I18N=1 pytest tests/test_error_i18n.py::test_regen"
            )


def test_error_docs_exist_for_every_language():
    for lang in LANGUAGES:
        path = DOCS / f"errors.{lang}.md"
        assert path.is_file(), f"missing {path}"
    for lang in TARGET_LANGUAGES:
        assert "_(en)_" not in (DOCS / f"errors.{lang}.md").read_text(), (
            f"{lang} error reference has en-fallback rows — "
            f"the {lang} catalog is incomplete"
        )


def test_the_emitter_no_longer_warns_that_nothing_is_shipped():
    """The warning this whole file exists to silence, asserted directly.

    ``[warning:unshipped]`` is what ``make contract`` printed for every
    stapel-alerts release before 0.2. A green suite that did not check for it
    would leave the actual complaint in place while looking finished.
    """
    from django.utils.module_loading import autodiscover_modules

    from stapel_core.django.api.errors import build_error_registry
    from stapel_core.i18n import check_registry_catalog_pairing

    autodiscover_modules("errors")
    entries = build_error_registry()
    assert any(e.get("owner") == "stapel_alerts" for e in entries), (
        "the registry attributes nothing to stapel_alerts — this test would "
        "then be asserting that a package nobody declared has no problems"
    )

    issues = check_registry_catalog_pairing(entries)
    ours = [
        i for i in issues
        if "stapel_alerts" in i.message and i.code in ("unshipped", "untranslated")
    ]
    assert not ours, "\n".join(f"[{i.level}:{i.code}] {i.message}" for i in ours)
