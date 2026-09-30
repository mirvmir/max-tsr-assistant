"""Readability must preserve values, uncertainty and the action being confirmed."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tsr.adapters.max import render_max_view
from tsr.contracts import ActionSpec, Delivery, Fact, Money, ViewModel
from tsr.domain.conversation import input_view, money_text, page_text, pricing_lines
from tsr.operations.releases import load_release


@pytest.mark.parametrize('text', [
    ('Короткий абзац с пробелами.\n\n' * 140),
    ('Одна длинная строка со словами ' * 160),
    ('А' * 5000),
    '',
])
def test_paging_preserves_entire_confirmed_value(text):
    _, count = page_text(text, 0)
    pages = [page_text(text, index)[0] for index in range(count)]
    assert ''.join(pages) == text
    assert all(len(page) <= 1800 for page in pages)
    if '\n\n' in text:
        assert all(page.endswith('\n\n') for page in pages[:-1])
    with pytest.raises(ValueError):
        page_text(text, count)


def test_short_button_pairs_keep_order_handles_and_destructive_separation():
    labels = ('confirm', 'edit', 'Выбрать 1: очень длинное название коляски',
              'back', 'resume', 'materials', 'help', 'Мои кейсы',
              'Новый кейс с актуальным каталогом', 'delete')
    actions = tuple(ActionSpec(label_key=label, action_handle=uuid4().hex,
                    expires_at=datetime.now(timezone.utc)+timedelta(minutes=5)) for label in labels)
    view = ViewModel(view_id=uuid4(), kind='candidate', title_key='candidate.title', actions=actions)
    payload = render_max_view(view)
    assert [button.payload for row in payload.buttons for button in row] == [a.action_handle for a in actions]
    assert [len(row) for row in payload.buttons] == [2, 1, 2, 2, 2, 1]
    assert payload.buttons[-1][0].text == 'Удалить подбор'
    assert all(len(button.text) <= 20 for row in payload.buttons if len(row) == 2 for button in row)


def test_demo_questions_do_not_expose_release_metadata_or_change_profile():
    release = load_release(Path(__file__).parents[1], 'data/releases/real-1.1.0/manifest.json')
    before = release.profile.model_dump_json()
    case = SimpleNamespace(case_id=uuid4(), case_revision=0, deletion_epoch=0,
                           dialog_revision=0, mode='demo')
    for rule in release.profile.fields:
        view = input_view(case, rule)
        text = view.sections[0].parameters.root['text']
        assert len(text) < 180
        assert not any(word in text for word in ('draft', 'public_snapshot', 'экспертом'))
        assert any(action.action_key == 'unknown' for action in view.actions)
    assert release.profile.model_dump_json() == before


def test_compact_money_keeps_kopecks_and_unknown_is_not_zero():
    assert money_text(Money(minor=450000)) == '4 500 ₽'
    assert money_text(Money(minor=450001)) == '4 500,01 ₽'
    assert money_text(Money(minor=1)) == '0,01 ₽'
    assert money_text(Money(minor=0)) == '0 ₽'
    assert money_text(None) == 'Неизвестно'


def test_compact_pricing_keeps_conditional_certificate_and_unknown_delivery():
    from test_documents import frozen_manifest
    quote = frozen_manifest('purchase').content.pricing.model_copy(update={
        'price': Money(minor=1450000), 'certificate_limit': Money(minor=1000000),
        'coverage': Money(minor=1000000), 'gap': Money(minor=450000),
        'certificate_use': 'conditional', 'possible_own_total': None,
        'delivery': Delivery(mode='unknown', charge=Fact.unknown(), terms=Fact.unknown()),
    })
    text = '\n'.join(pricing_lines(quote, compact=True))
    assert 'Условная разница без доставки: 4 500 ₽' in text
    assert 'приём оплаты требует уточнения' in text
    assert 'Доставка: условия и стоимость неизвестны' in text
    assert not any('итог' in line.lower() for line in text.splitlines())
