from speakeasy.prompt import builder


def test_voice_is_told_to_hand_off_instead_of_refusing():
    text = builder.truthfulness(builder.Names())
    assert "never decline a request yourself" in text
    assert "purchases" in text and "never say you are not allowed" in text
