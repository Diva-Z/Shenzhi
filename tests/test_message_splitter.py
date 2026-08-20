import common.message_splitter as ms
from common.message_splitter import split_text_bubbles


def _content_of(bubbles):
    return "".join(b.replace("\n\n", "") for b in bubbles)


# --- [MSG] marker behaviour -------------------------------------------------

def test_split_msg_then_short_sentence_fallback():
    text = "在想一篇稿子的事……[MSG]有个作者的措辞改了好几轮还是不太对，卡在那儿有点烦。\n\n你呢，今天怎么有空找我？"

    assert split_text_bubbles(text) == [
        "在想一篇稿子的事……",
        "有个作者的措辞改了好几轮还是不太对，卡在那儿有点烦。",
        "你呢，今天怎么有空找我？",
    ]


def test_explicit_msg_markers_never_folded_below_cap():
    # The model's explicit [MSG] split is a hard decision: even 7 parts must
    # survive as 7 bubbles although the default cap is 5.
    text = "[MSG]".join(["第一段话。", "第二段话。", "第三段话。", "第四段话。",
                         "第五段话。", "第六段话。", "第七段话。"])
    bubbles = split_text_bubbles(text)
    assert len(bubbles) == 7
    assert bubbles[0] == "第一段话。"
    assert bubbles[-1] == "第七段话。"


# --- plain chat text: one sentence per bubble -------------------------------

def test_split_plain_short_chinese_sentences():
    assert split_text_bubbles("嗯。说说看。") == ["嗯。", "说说看。"]


def test_one_sentence_per_bubble():
    assert split_text_bubbles("嗯。好呀。你说。") == ["嗯。", "好呀。", "你说。"]


def test_user_reported_four_sentences_become_four_bubbles():
    # Regression for the "long reply lands in one giant bubble" bug: this
    # exact text used to be delivered as a single bubble.
    text = "今天公司有点烦。其实也没发生什么大事，就是莫名有点累。你现在下班了吗？我突然想找你说会儿话。"
    assert split_text_bubbles(text) == [
        "今天公司有点烦。",
        "其实也没发生什么大事，就是莫名有点累。",
        "你现在下班了吗？",
        "我突然想找你说会儿话。",
    ]


def test_keep_single_clause_without_terminal_punctuation():
    assert split_text_bubbles("一米六三左右吧，怎么突然问这个") == ["一米六三左右吧，怎么突然问这个"]


def test_paragraph_breaks_split():
    text = "第一段先说这些。\n\n第二段换个话题。"
    bubbles = split_text_bubbles(text)
    assert bubbles == ["第一段先说这些。", "第二段换个话题。"]
    assert _content_of(bubbles) == text.replace("\n\n", "")


# --- long text must keep splitting (the old 220-char ban is gone) -----------

def test_long_reply_still_splits():
    sentence = "这是一句不算短的日常闲聊，用来凑长度。"
    text = sentence * 8  # ~160+ chars, used to be exempt from splitting
    bubbles = split_text_bubbles(text)
    assert len(bubbles) >= 2
    assert _content_of(bubbles) == text


def test_hard_budget_caps_each_bubble():
    # A single endless sentence (no terminal punctuation) must be hard-wrapped.
    text = "长" * 400
    bubbles = split_text_bubbles(text)
    assert len(bubbles) >= 3
    assert all(len(b) <= 160 for b in bubbles)
    assert _content_of(bubbles) == text


def test_folding_never_drops_content():
    sentences = [f"这是第{num}句闲聊内容，长度故意写得不太短。" for num in range(1, 8)]
    text = "".join(sentences)
    bubbles = split_text_bubbles(text)
    assert len(bubbles) <= 5
    assert _content_of(bubbles) == text


def test_explicit_max_parts_override():
    text = "第一句话。第二句话。第三句话。第四句话。"
    bubbles = split_text_bubbles(text, max_parts=2)
    assert len(bubbles) == 2
    assert _content_of(bubbles) == text


# --- protected content -------------------------------------------------------

def test_keep_url_and_markdown_together():
    assert split_text_bubbles("你看这个：https://example.com/a?x=1。别点错。") == [
        "你看这个：https://example.com/a?x=1。别点错。"
    ]
    assert split_text_bubbles("- 第一条。\n- 第二条。") == ["- 第一条。\n- 第二条。"]


def test_code_fence_kept_intact():
    text = "给你看下代码：\n```python\nprint('你好。再见。')\n```\n就这些。"
    bubbles = split_text_bubbles(text)
    assert any("```python" in b and "print" in b and "```" in b for b in bubbles)
    assert not any(b.startswith("print(") for b in bubbles)


# --- edge cases ---------------------------------------------------------------

def test_empty_text():
    assert split_text_bubbles("") == []
    assert split_text_bubbles("   \n  ") == []


def test_english_text_not_sentence_split():
    text = "Hello there. How are you today? I am fine."
    assert split_text_bubbles(text) == [text]


def test_disabled_returns_marker_parts(monkeypatch):
    monkeypatch.setattr(ms, "_bubble_conf", lambda: (5, 80, 160, False))
    text = "第一句和第二句都在一段里。[MSG]第二段也有两句。对吧？"
    assert split_text_bubbles(text) == [
        "第一句和第二句都在一段里。",
        "第二段也有两句。对吧？",
    ]
