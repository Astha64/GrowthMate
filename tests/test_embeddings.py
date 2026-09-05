"""
Embedding tests — lightweight deterministic vectors (LLD §7.2).
Deterministic, dependency-free; similar text should score higher than
dissimilar text.
"""

from app.embeddings import clear_embedding_cache, cosine, embed_text


def test_cosine_of_identical():
    a = embed_text("running shoes under budget")
    assert cosine(a, a) == 1.0


def test_cosine_similar_gt_dissimilar():
    q = embed_text("wireless earbuds bluetooth")
    same = embed_text("wireless bluetooth earbuds")
    diff = embed_text("leather wallet bifold")
    assert cosine(q, same) > cosine(q, diff)


def test_empty_text_zero_vector():
    assert embed_text("") == [0.0] * 512


def test_deterministic_vectors():
    a = embed_text("cotton crew t-shirt apparel")
    b = embed_text("cotton crew t-shirt apparel")
    assert a == b


def test_unit_length():
    v = embed_text("anything at all")  # noqa: F841
    norm = sum(x * x for x in v) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_cache_clear():
    embed_text("cached product")
    clear_embedding_cache()
    assert embed_text("cached product") is not None