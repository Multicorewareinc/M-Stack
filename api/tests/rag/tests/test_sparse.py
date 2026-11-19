"""Exact-word search as sparse vectors: how text is split into words, and
what each chunk's and question's vector holds."""

from __future__ import annotations

from sparse import K1, average_length, document_vector, query_vector, term_id, tokenize


def test_an_identifier_is_kept_whole_and_as_its_parts():
    tokens = tokenize("event_backbone_url")
    assert tokens[0] == "event_backbone_url"
    assert {"event", "backbone", "url"} <= set(tokens)


def test_camel_case_names_are_split():
    assert {"model", "gateway", "option"} <= set(tokenize("ModelGatewayOptions"))


def test_dotted_names_are_split_at_the_dots():
    assert {"gateway", "event", "enriched"} <= set(tokenize("gateway.events.enriched"))


def test_case_does_not_matter_and_plurals_match_their_singular():
    assert tokenize("Replicas REPLICAS") == tokenize("replica replica")
    assert tokenize("class") == ["class"]


def test_filler_words_are_dropped_but_meaningful_short_ones_kept():
    assert tokenize("how do I set the limit") == ["set", "limit"]
    assert "s3" in tokenize("an s3 bucket")


def test_a_word_always_maps_to_the_same_32_bit_index():
    assert term_id("replica") == term_id("replica")
    assert 0 <= term_id("replica") < 2**32


def test_a_question_vector_weighs_each_distinct_word_once():
    vector = query_vector("replicas replicas gateway")
    assert len(vector["indices"]) == 2 and vector["values"] == [1.0, 1.0]
    assert vector["indices"] == sorted(vector["indices"])


def test_a_question_with_no_searchable_words_has_an_empty_vector():
    assert query_vector("how do I?") == {"indices": [], "values": []}


def test_repeating_a_word_counts_for_more_but_saturates():
    once = document_vector(["cache"], 1.0)["values"][0]
    twice = document_vector(["cache", "cache"], 2.0)["values"][0]
    many = document_vector(["cache"] * 50, 50.0)["values"][0]
    assert once < twice < many < K1 + 1


def test_a_long_chunk_weighs_a_word_less_than_a_short_one():
    short = document_vector(["cache", "url"], 10.0)
    long = document_vector(["cache", "url"] + [f"w{i}" for i in range(30)], 10.0)
    cache = term_id("cache")
    assert (
        dict(zip(short["indices"], short["values"], strict=True))[cache]
        > dict(zip(long["indices"], long["values"], strict=True))[cache]
    )


def test_average_length_of_nothing_is_zero():
    assert average_length([]) == 0.0
    assert average_length([["a", "b"], ["c", "d", "e", "f"]]) == 3.0
