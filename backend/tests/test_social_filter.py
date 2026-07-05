"""Unit tests for the NSFW/spam social filter (pure)."""

import social_filter as sf


def _post(text="", labels=None, self_labels=None):
    p = {"record": {"text": text}}
    if labels is not None:
        p["labels"] = [{"val": v} for v in labels]
    if self_labels is not None:
        p["record"]["labels"] = {"values": [{"val": v} for v in self_labels]}
    return p


def test_moderation_label_is_nsfw():
    assert sf.is_nsfw_post(_post("anything", labels=["porn"]))


def test_self_label_is_nsfw():
    assert sf.is_nsfw_post(_post("check my page", self_labels=["nudity"]))


def test_keyword_backstop():
    # adult-spam tagged with a finance hashtag still gets caught by the blocklist
    assert sf.is_nsfw_post(_post("$BTC to the moon 🚀 onlyfans link in bio"))


def test_clean_finance_post_passes():
    assert not sf.is_nsfw_post(_post("$NVDA earnings beat, calls printing #stocks"))


def test_blocked_text_word_boundary():
    assert sf.is_blocked_text("free nudes here")
    assert not sf.is_blocked_text("nuanced analysis of the market")  # 'nu...' not 'nude'


def test_post_labels_collects_both_sources():
    p = _post("x", labels=["sexual"], self_labels=["nudity"])
    assert sf.post_labels(p) == {"sexual", "nudity"}
