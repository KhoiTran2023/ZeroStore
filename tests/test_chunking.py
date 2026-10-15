from zerostore.librarian.chunking import MAX_CHARS, chunk_text


def test_short_text_is_one_chunk():
    chunks = chunk_text("The Pro plan costs $29 per month.")
    assert [c.text for c in chunks] == ["The Pro plan costs $29 per month."]
    assert chunks[0].heading == ""


def test_headings_travel_with_their_text():
    text = "# Pricing\n\nIntro.\n\n## Pro plan\n\nIt costs $29.\n\n## Team plan\n\nIt costs $99.\n"
    chunks = chunk_text(text)
    by_heading = {c.heading: c.text for c in chunks}
    assert by_heading["Pricing > Pro plan"] == "It costs $29."
    assert by_heading["Pricing > Team plan"] == "It costs $99."
    assert chunks[1].with_heading() == "Pricing > Pro plan\nIt costs $29."


def test_heading_trail_resets_at_same_level():
    text = "# A\n\n## B\n\nx\n\n# C\n\ny\n"
    assert [c.heading for c in chunk_text(text)] == ["A > B", "C"]


def test_code_blocks_are_not_headings():
    text = "# Setup\n\n```bash\n# install it\npip install zerostore\n```\n"
    chunks = chunk_text(text)
    assert len(chunks) == 1 and "# install it" in chunks[0].text


def test_long_text_is_split_within_the_limit_without_losing_words():
    paragraph = " ".join(f"Sentence number {i} says something." for i in range(400))
    chunks = chunk_text(paragraph)
    assert len(chunks) > 1
    assert all(len(c.text) <= MAX_CHARS for c in chunks)
    assert " ".join(c.text for c in chunks).split() == paragraph.split()


def test_one_giant_word_run_is_still_split():
    blob = "x" * 5000
    chunks = chunk_text(blob)
    assert all(len(c.text) <= MAX_CHARS for c in chunks)
    assert "".join(c.text for c in chunks) == blob


def test_empty_text():
    assert chunk_text("   \n\n ") == []
