from __future__ import annotations

from dataclasses import replace

from services import consumable_service
from utils.tavern_view import public_offer_text


def test_recipient_is_prominent_and_both_mentions_stay_clickable():
    text = public_offer_text('<a href="tg://user?id=1">Mario</a>', '<a href="tg://user?id=2">Anna</a>', None)
    assert text.startswith('🎁 <b>Un\'offerta per <a href="tg://user?id=2">Anna</a></b>')
    assert 'Da <a href="tg://user?id=1">Mario</a>' in text
    assert "Solo il destinatario" in text


def test_consumed_card_names_the_consumer_and_escapes_catalog_and_ai():
    item = replace(consumable_service.get_item("cons_nuka_cola"), name="<b>cola</b>", emoji="<x>")
    text = public_offer_text("Mario", "Anna", item, consumed=True, consumer="Anna", reaction="<a>test</a> & altro")
    assert "Consumato da Anna" in text
    assert "&lt;b&gt;cola&lt;/b&gt;" in text and "&lt;x&gt;" in text
    assert "&lt;a&gt;test&lt;/a&gt; &amp; altro" in text
    assert "Solo il destinatario" not in text


def test_historical_gift_does_not_attribute_consumption_to_a_previous_owner():
    text = public_offer_text("Mario", "Anna", None, consumed=True)
    assert "Regalo già consumato" in text
    assert "Consumato da Anna" not in text
