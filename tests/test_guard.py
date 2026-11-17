import pytest

from callagent import guard


@pytest.mark.parametrize(
    "text",
    [
        "Stop calling me.",
        "Take me off your list, please.",
        "Put me on your do not call list.",
        "Don't call this number again.",
        "Please remove my number from your system.",
        "Lose my number.",
        "No more calls, thanks.",
        "I'm on the Do-Not-Call registry, you know.",
        "never call me",
        "don't ever call me again",
    ],
)
def test_requests_to_stop_are_heard(text):
    assert guard.asks_to_stop(text)


@pytest.mark.parametrize(
    "text",
    [
        "I'm at work, don't call me back today. Try tomorrow morning.",
        "Can you call me back after six?",
        "I'm not interested, thanks.",
        "Please stop, you're going too fast.",
        "I never got a call back from the last lender.",
        "Take your time.",
    ],
)
def test_ordinary_replies_are_not_opt_outs(text):
    assert not guard.asks_to_stop(text)


def test_the_patterns_do_not_catch_every_wording():
    """Unusual wordings are left to the model, which has a tool for it. Both paths are tested."""
    assert not guard.asks_to_stop("I never want to hear from you people again.")


@pytest.mark.parametrize(
    "text",
    [
        "Is this a real person?",
        "Are you a robot?",
        "Am I talking to a machine?",
        "wait, is this AI",
    ],
)
def test_asking_whether_it_is_a_person(text):
    assert guard.asks_if_human(text)


def test_not_every_question_is_about_being_a_person():
    assert not guard.asks_if_human("Are you open on Saturdays?")
    assert not guard.asks_if_human("Is this about my refinance?")


def test_the_opening_line_carries_both_disclosures(playbook):
    assert guard.has_disclosures(playbook.opening_line("Jordan"))
    assert not guard.has_disclosures("Hi, this is Ava from Harbor Home Lending.")
    assert not guard.has_disclosures("Hi, this is Ava, an AI assistant.")


@pytest.mark.parametrize(
    ("sentence", "problem"),
    [
        ("You'd get about 6.25% on that.", "quotes a rate"),
        ("Rates are around six percent this week.", "quotes a rate"),
        ("Call it five and a half percent.", "quotes a rate"),
        ("With that credit, approval is guaranteed.", "promises approval"),
        ("You'll definitely be approved.", "promises approval"),
        ("You're pre-approved already.", "promises approval"),
        ("This offer expires tonight.", "pressures the contact"),
        ("I have your social as 512-44-7166.", "repeats a nine-digit number"),
    ],
)
def test_sentences_the_agent_must_never_say(sentence, problem):
    assert guard.violations(sentence) == [problem]


@pytest.mark.parametrize(
    "sentence",
    [
        "I can't quote a rate on this call.",
        "I can't guarantee approval, and nobody can before a review.",
        "I can't say whether a loan would be approved.",
        "Most loans close in about 30 to 45 days.",
        "Your home is worth about $400,000, is that right?",
        "Would Thursday at 10:00 AM work?",
        "You can reach us at 555-0100.",
        "There's no rush at all.",
    ],
)
def test_sentences_that_are_fine(sentence, playbook):
    assert guard.violations(sentence) == []


def test_every_approved_answer_passes_its_own_guard(playbook):
    for text in [*playbook.answers.values(), playbook.fallback, playbook.voicemail_line()]:
        assert guard.withhold(text, playbook.fallback) == (text, [])


def test_only_the_offending_sentence_is_withheld(playbook):
    text = "Thanks, Jordan. You'd get about 6.25% on that. What is the home worth?"
    spoken, problems = guard.withhold(text, playbook.fallback)
    assert problems == ["quotes a rate"]
    assert "6.25" not in spoken
    assert spoken.startswith("Thanks, Jordan. What is the home worth?")
    assert spoken.endswith(playbook.fallback)


def test_stage_directions_are_never_spoken(playbook):
    spoken, problems = guard.withhold("Thank you. Goodbye. [end_call with reason voicemail]", "x")
    assert spoken == "Thank you. Goodbye." and problems == ["writes a stage direction"]
    assert guard.withhold("*pauses* Hello there.", "x")[0] == "Hello there."
