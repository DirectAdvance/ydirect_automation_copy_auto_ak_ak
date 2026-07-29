"""Контракт сигнатур token/cookie-путей tp2/tp4.

Живой регресс (job 4bce0676297a, коммит f970f097): `run_create_set_text` собирает ОДИН
kwargs-набор `cookie_kwargs` и шлёт его в оба пути (`create_set_text.py:53-99`,
комментарий «token_kwargs == cookie_kwargs»), но в сигнатуре
`create_set_feed_builders._create_text_via_token` не было `keep_keywords`, хотя тело функции
его уже прокидывало в `_build_text_from_pack`. Итог: `TypeError: unexpected keyword argument
'keep_keywords'` за ~0 сек на каждом tp2/tp4-item token-пути.

Тест берёт РЕАЛЬНЫЙ набор ключей из AST вызывающего кода (а не зашитый список) и проверяет
его настоящими сигнатурами обеих функций через `inspect.signature().bind`, без моков.
"""
import ast
import inspect
import os

from direct.create import create_set_feed_builders
from direct.create import create_set_text
from direct.create import create_set_text_builders


def _caller_kwargs_keys() -> set[str]:
    """Ключи `cookie_kwargs = dict(...)` из run_create_set_text (create_set_text.py)."""
    src = os.path.join(os.path.dirname(os.path.abspath(create_set_text.__file__)), "create_set_text.py")
    tree = ast.parse(open(src, encoding="utf-8").read(), src)
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "cookie_kwargs" for t in node.targets):
            continue
        call = node.value
        assert isinstance(call.func, ast.Name) and call.func.id == "dict", "cookie_kwargs строится dict(...)"
        for kw in call.keywords:
            assert kw.arg, "в cookie_kwargs не должно быть **unpack — тест обязан видеть все ключи"
            keys.add(kw.arg)
    return keys


def test_caller_kwargs_are_discoverable():
    keys = _caller_kwargs_keys()
    assert keys, "не нашли cookie_kwargs в create_set_text.run_create_set_text"
    # опорные ключи режима автотаргета: именно keep_keywords уронил token-путь
    assert {"autotarget", "keep_keywords", "token", "only_gks"} <= keys


def test_token_and_cookie_paths_accept_exactly_caller_kwargs():
    keys = _caller_kwargs_keys()
    payload = {k: None for k in keys}
    for fn in (create_set_feed_builders._create_text_via_token,
               create_set_feed_builders._create_text_via_cookie):
        # bind падает TypeError ровно на том дефекте, что был в проде
        inspect.signature(fn).bind(**payload)


def test_text_paths_do_not_swallow_unknown_kwargs():
    """Починка через `**kwargs` запрещена: она спрячет и этот дефект, и будущие такие же."""
    for fn in (create_set_feed_builders._create_text_via_token,
               create_set_feed_builders._create_text_via_cookie):
        params = inspect.signature(fn).parameters.values()
        assert not [p for p in params if p.kind is inspect.Parameter.VAR_KEYWORD], (
            f"{fn.__name__} не должна принимать **kwargs — иначе опечатка в имени параметра"
            " будет молча проглочена")
        assert not [p for p in params if p.kind is inspect.Parameter.VAR_POSITIONAL]


def test_keep_keywords_reaches_group_builder():
    """keep_keywords имеет смысл на tp2/tp4: `_build_tp2_adgroups` по нему решает, лить ли
    реальные ключи в группу чистого автотаргета (create_set_text_builders.py:74,168)."""
    assert "keep_keywords" in inspect.signature(create_set_text_builders._build_text_from_pack).parameters
    assert "keep_keywords" in inspect.signature(create_set_text_builders._build_tp2_adgroups).parameters
    tok_src = inspect.getsource(create_set_feed_builders._create_text_via_token)
    assert "keep_keywords=bool(keep_keywords)" in tok_src, (
        "token-путь обязан прокидывать флаг в билдер групп, а не игнорировать его")
