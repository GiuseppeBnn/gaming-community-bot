from __future__ import annotations

import pytest

from services import raid_service
from services.structured_ai import StructuredAIError


class _Provider:
    async def generate_json(self, **kwargs):
        return {
            "boss_name": "Leviatano di Vetro",
            "intro": "Una creatura trasparente emerge dal lago e sfida tutta la compagnia riunita.",
            "victory_text": "Il Leviatano si frantuma e il lago torna finalmente calmo.",
            "defeat_text": "Il Leviatano torna nelle profondità, pronto a riemergere presto.",
            "phases": [{
                "title": f"Fase narrativa {index}",
                "scene": "La creatura prepara un attacco leggibile mentre il terreno cambia forma.",
                "telegraph": "Le scaglie luminose indicano il punto da osservare con attenzione.",
                "choices": {"a": "Assalto frontale", "d": "Difesa compatta", "i": "Trucco laterale"},
                "success_text": "La compagnia interpreta il segnale e ferisce il nemico.",
                "setback_text": "Il nemico resiste, ma la compagnia riesce comunque a indebolirlo.",
            } for index in range(1, 4)],
        }


class _BrokenProvider:
    async def generate_json(self, **kwargs):
        raise StructuredAIError("offline")


async def test_blueprint_is_validated_and_counter_is_owned_by_code():
    provider = _Provider()
    blueprint, fallback = await raid_service.build_blueprint(
        "mostro marino", provider, counters=("i", "a", "d"),
    )
    assert not fallback
    assert blueprint.boss_name == "Leviatano di Vetro"
    assert tuple(phase.counter for phase in blueprint.phases) == ("i", "a", "d")


async def test_invalid_or_unavailable_ai_uses_complete_fallback():
    blueprint, fallback = await raid_service.build_blueprint(
        "castello volante", _BrokenProvider(), counters=("a", "d", "i"),
    )
    assert fallback
    assert len(blueprint.phases) == 3
    assert "castello volante" in blueprint.intro


def test_damage_uses_fractions_not_group_size():
    assert raid_service._damage(1, 1) == (40, "decisive")
    assert raid_service._damage(60, 100) == (40, "decisive")
    assert raid_service._damage(1, 3) == (34, "success")
    assert raid_service._damage(33, 100) == (22, "setback")


@pytest.mark.parametrize(("rolls", "successes", "bonus", "twenties", "ones"), [
    ([11], 1, 3, 0, 0),
    ([10], 0, 0, 0, 0),
    ([11, 10], 1, 1, 0, 0),
    ([20, 11, 10], 2, 3, 1, 0),
    ([20, 1, 19, 2], 2, 1, 1, 1),
])
def test_party_check_is_proportional_and_bounded(
    rolls, successes, bonus, twenties, ones,
):
    result = raid_service._party_check(rolls)
    assert result.successes == successes
    assert result.bonus == bonus
    assert result.natural_20s == twenties
    assert result.natural_1s == ones
    assert result.bonus <= 3


@pytest.mark.parametrize("rolls", [[], [0], [21]])
def test_party_check_rejects_invalid_dice(rolls):
    with pytest.raises(ValueError, match="valid d20"):
        raid_service._party_check(rolls)


def test_blueprint_round_trip_and_corruption():
    source = raid_service.fallback_blueprint("tema", ("a", "d", "i"))
    assert raid_service.parse_blueprint(raid_service.blueprint_json(source)) == source
    with pytest.raises(RuntimeError, match="corrupt"):
        raid_service.parse_blueprint("{}")


def test_local_counter_sequence_is_a_permutation():
    assert set(raid_service._counter_sequence()) == set(raid_service.TACTICS)
    assert len(raid_service.fallback_blueprint("tema").phases) == 3


def test_fallback_clue_changes_with_the_locally_selected_counter():
    assault = raid_service.fallback_blueprint("tema", ("a", "d", "i"))
    trick = raid_service.fallback_blueprint("tema", ("i", "d", "a"))
    assert assault.phases[0].counter == "a"
    assert trick.phases[0].counter == "i"
    assert assault.phases[0].telegraph != trick.phases[0].telegraph


async def test_ai_receives_the_local_counter_as_a_trusted_mechanical_constraint():
    class CapturingProvider(_Provider):
        def __init__(self):
            self.kwargs = None

        async def generate_json(self, **kwargs):
            self.kwargs = kwargs
            return await super().generate_json(**kwargs)

    provider = CapturingProvider()
    await raid_service.build_blueprint(
        "tema utente", provider, counters=("d", "i", "a"),
    )
    prompt = provider.kwargs["user_prompt"]
    assert '"tema_non_attendibile": "tema utente"' in prompt
    assert '"fase_1_tattica_efficace": "d"' in prompt


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(phases=[]),
    lambda value: value["phases"].__setitem__(0, "bad"),
    lambda value: value["phases"][0].update(choices={"a": "x"}),
    lambda value: value["phases"][0].update(
        choices={"a": "Uguale", "d": "uguale", "i": "Diversa"},
    ),
    lambda value: value.update(boss_name=5),
    lambda value: value.update(boss_name="x"),
])
async def test_bad_ai_shapes_fall_back(mutation):
    value = await _Provider().generate_json()
    mutation(value)

    class Invalid:
        async def generate_json(self, **kwargs):
            return value

    blueprint, fallback = await raid_service.build_blueprint(
        "tema", Invalid(), counters=("a", "d", "i"),
    )
    assert fallback and len(blueprint.phases) == 3


@pytest.mark.parametrize("raw", [
    "[]",
    '{"phases": "bad"}',
    '{"phases": [{}, {}, {}]}',
    '{"phases": [{"counter": "x"}, {"counter": "a"}, {"counter": "d"}]}',
])
def test_corrupt_serialized_shapes_are_rejected(raw):
    with pytest.raises(RuntimeError, match="corrupt"):
        raid_service.parse_blueprint(raw)
