"""Полнота и точность `outcome.APPLY_LABELS` — подписи, которые лента получает мостом.

Заведён аудитом 22.09.2026 (`docs/audits/2026-09-22-quality.md`, §3.2): до него подписи жили
только в `src/feed/main.js::APPLY_LABELS`, моста не было, и дрейф уже случился — сервер отдавал
статус `taken`, а метки для него в ленте не существовало, поэтому пользователь видел сырое
английское слово. Страж ловит ровно этот класс: новый член `ApplyOutcome`/`TransportStatus`
без подписи и подпись без члена.
"""
from hrwork.application.apply.outcome import APPLY_LABELS, ApplyOutcome, TransportStatus


def test_every_outcome_and_transport_status_has_a_label():
    codes = {o.code for o in ApplyOutcome} | {t.code for t in TransportStatus}
    assert set(APPLY_LABELS) == codes


def test_labels_are_non_empty_and_distinct_per_code():
    # две разные судьбы отклика не должны выглядеть для человека одинаково
    assert all(v.strip() for v in APPLY_LABELS.values())
    assert len(set(APPLY_LABELS.values())) == len(APPLY_LABELS)


def test_transport_codes_are_the_wire_strings_the_feed_already_knows():
    # литералы: их видит JS и по ним же отвечает server.py::_apply_post
    assert [t.code for t in TransportStatus] == [
        "queued", "busy", "no-session", "taken", "error"]
