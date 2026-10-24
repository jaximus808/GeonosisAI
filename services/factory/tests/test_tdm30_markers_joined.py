from rdf_factory.events import parse_waiting_for, says_done


def test_waiting_for_glued_to_previous_sentence():
    p = parse_waiting_for("Published the dataset. I'll stop.WAITING_FOR data_plan:iteration>=1")
    assert p["table"] == "data_plan" and p["field"] == "iteration" and p["op"] == ">="


def test_done_glued_to_previous_sentence():
    assert says_done("All checks passed.DONE")


def test_markers_inside_words_are_not_matched():
    assert parse_waiting_for("the NOTWAITING_FOR x:y==1 flag") is None
    assert not says_done("ABANDONED")
