

def test_a_follow_up_sent_into_a_running_task_shows_only_what_was_written():
    """Hermes wraps guidance into a running turn; the app showed the whole wrapper (and the shared
    picture's saved path) as the user's bubble."""
    from speakeasy.history import user_words
    wrapped = ("[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position]\n"
               "Sam typed to this task: the header looks off. Sam shared the picture Photo.jpg (saved at "
               "/tmp/shared/Photo.jpg). Open the images with vision_analyze. Use them to understand what they mean. "
               "Fold it into the work you are doing; do not start over.\n[/OUT-OF-BAND USER MESSAGE]")
    assert user_words(wrapped) == ("user", "the header looks off.")
    spoken = "Sam just added to this task by voice: and the footer too Fold it into the work you are doing; do not start over."
    assert user_words(spoken) == ("user", "and the footer too")


def test_a_typed_follow_up_with_a_picture_is_not_called_voice():
    from speakeasy.prompt import builder as P
    names = P.Names(user_name="Sam")
    typed = P.shared_steer_text(names, "the header looks off", "x shared a picture.", typed=True)
    assert "by voice" not in typed and "typed to this task" in typed
    assert "by voice" in P.shared_steer_text(names, "the header looks off", "x shared a picture.")
