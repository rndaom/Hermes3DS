import json
import types

from h3proto.clarify_text import coerce

SKIPPED = object()


def entry(*choices, multi=False):
    return types.SimpleNamespace(choices=list(choices), multi_select=multi)


def test_a_number_or_a_label_picks_that_choice():
    e = entry("Staging (Recommended)", "Prod")
    assert coerce(e, "2", SKIPPED) == "Prod"
    assert coerce(e, " staging ", SKIPPED) == "Staging (Recommended)"
    assert coerce(e, "PROD", SKIPPED) == "Prod"


def test_text_that_names_no_choice_is_left_alone():
    e = entry("Staging", "Prod")
    assert coerce(e, "3", SKIPPED) is None
    assert coerce(e, "the one we used yesterday", SKIPPED) is None
    assert coerce(entry(), "anything", SKIPPED) is None


def test_multi_select_answers_become_a_json_array_of_labels():
    e = entry("A", "B", "C", multi=True)
    assert json.loads(coerce(e, "1,3", SKIPPED)) == ["A", "C"]
    assert json.loads(coerce(e, "3 1 3", SKIPPED)) == ["C", "A"]
    assert json.loads(coerce(e, "b, c", SKIPPED)) == ["B", "C"]
    assert coerce(e, "1,9", SKIPPED) is None  # one bad token rejects the whole answer


def test_skip_skips():
    assert coerce(entry("A", "B"), "Skip", SKIPPED) is SKIPPED
