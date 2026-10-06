from handlers.music import _clean_youtube_title


def test_clean_youtube_title_removes_lyrics_noise():
    assert _clean_youtube_title("Song Name (Official Lyrics)") == "Song Name"
    assert _clean_youtube_title("Song Name [Lirik Video]") == "Song Name"
    assert _clean_youtube_title("Song Name - Lyrics") == "Song Name"


def test_clean_youtube_title_keeps_normal_title():
    assert _clean_youtube_title("Song Name - Artist") == "Song Name - Artist"
